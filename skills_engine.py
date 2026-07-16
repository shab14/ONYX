"""
skills_engine.py — ONYX Skills Engine v2
Remplace skill_forge.py + skills_dynamic.py par un vrai système de skills
multi-étapes, inspiré de OpenJarvis (skills/manager.py, executor.py, types.py,
security.py) mais réécrit simple pour le stack ONYX :
  - pas d'EventBus, pas de config TOML hiérarchique, pas de signature Ed25519
  - manifests stockés en JSON dans AppData/ONYX/skills/*.json
  - chaque step appelle soit une _ACTIONS existante (actions.py), soit du texte
    libre renvoyé au LLM
  - garde-fous sur les capacités dangereuses (shell, fichiers, réseau)

Différence avec l'ancien skills_dynamic.py (un seul mot-clé → une fonction) :
ici un skill = une SÉQUENCE d'étapes avec contexte partagé entre elles
(le résultat de l'étape 1 peut être injecté dans l'étape 2 via {placeholder}).

Usage :
    from skills_engine import skills_engine

    # Création d'un skill (typiquement via "apprends à ...")
    skills_engine.create_skill(
        name="briefing_matin",
        description="Météo + top actu + mes rappels du jour",
        steps=[
            {"action": "meteo", "params_template": {"ville": "Paris"}, "output_key": "meteo"},
            {"action": "recherche_web", "params_template": {"query": "actualités du jour"}, "output_key": "actu"},
            {"action": "lister_rappels", "params_template": {}, "output_key": "rappels"},
        ],
    )

    # Exécution
    result = skills_engine.run("briefing_matin")

    # Routing depuis main.py (remplace route_dynamic de l'ancien système)
    reply = skills_engine.route(commande_utilisateur)
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# ── Stockage : AppData/Roaming/ONYX/skills/*.json ────────────────────────────
if os.name == "nt":
    _APPDATA = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    _SKILLS_DIR = _APPDATA / "ONYX" / "skills"
else:
    _SKILLS_DIR = Path.home() / ".local" / "share" / "onyx" / "skills"

# ── Capacités dangereuses (alerte avant exécution, jamais bloqué silencieusement) ─
# Noms alignés sur les vraies clés de _ACTIONS dans actions.py
DANGEROUS_ACTIONS: frozenset[str] = frozenset({
    "supprimer_fichier", "kill_process", "shutdown", "restart",
    "deplacer_fichier", "fermer_app",
})


@dataclass(slots=True)
class SkillStep:
    action: str                        # nom du type dans _ACTIONS (actions.py), ex "meteo", "recherche_web"
    params_template: dict = field(default_factory=dict)  # ex {"ville": "{ville}", "query": "actu du jour"}
    output_key: str = ""               # où stocker le résultat pour les steps suivants

    def to_dict(self) -> dict:
        return {"action": self.action, "params_template": self.params_template, "output_key": self.output_key}

    @classmethod
    def from_dict(cls, d: dict) -> "SkillStep":
        return cls(
            action=d["action"],
            params_template=d.get("params_template", d.get("args_template", {}) or {}),
            output_key=d.get("output_key", ""),
        )


@dataclass(slots=True)
class SkillManifest:
    name: str
    description: str = ""
    keywords: list[str] = field(default_factory=list)  # déclencheurs pour route()
    steps: list[SkillStep] = field(default_factory=list)
    created_at: str = ""
    run_count: int = 0
    last_run: str = ""

    def required_capabilities(self) -> list[str]:
        """Liste les actions dangereuses utilisées par ce skill (pour confirmation)."""
        return [s.action for s in self.steps if s.action in DANGEROUS_ACTIONS]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "keywords": self.keywords,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "run_count": self.run_count,
            "last_run": self.last_run,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SkillManifest":
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            keywords=d.get("keywords", []),
            steps=[SkillStep.from_dict(s) for s in d.get("steps", [])],
            created_at=d.get("created_at", ""),
            run_count=d.get("run_count", 0),
            last_run=d.get("last_run", ""),
        )


def _render_value(value: Any, ctx: dict[str, Any]) -> Any:
    """Remplace {clé} par ctx[clé] dans une string ; récursif sur les dicts/listes."""
    if isinstance(value, str):
        def _sub(m: re.Match) -> str:
            key = m.group(1)
            return str(ctx.get(key, m.group(0)))
        return re.sub(r"\{(\w+)\}", _sub, value)
    if isinstance(value, dict):
        return {k: _render_value(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_value(v, ctx) for v in value]
    return value


class SkillsEngine:
    """
    Gère le cycle de vie complet : découverte (charge tous les .json du
    dossier), création, exécution séquentielle, et routing par mots-clés
    (remplace l'ancien route_dynamic()).
    """

    def __init__(self, skills_dir: Path = _SKILLS_DIR) -> None:
        self._dir = skills_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._skills: dict[str, SkillManifest] = {}
        self._action_dispatcher: Optional[Callable[[str, str], str]] = None
        self._confirm_dangerous: Optional[Callable[[str, list[str]], bool]] = None
        self._load_all()

    # ── Branchement avec actions.py / GUI ──────────────────────────────────

    def set_action_dispatcher(self, fn: Callable[[dict], str]) -> None:
        """
        fn({"type": action_name, "params": {...}}) -> résultat texte.
        C'est exactement la signature de executer_action() dans actions.py —
        on lui passe le même format de dict, pas une string brute, pour
        rester 100% compatible avec _ACTIONS sans wrapper supplémentaire.
        """
        self._action_dispatcher = fn

    def set_confirm_callback(self, fn: Callable[[str, list[str]], bool]) -> None:
        """
        fn(skill_name, dangerous_actions) -> True si l'utilisateur confirme.
        Si non défini, les skills dangereux sont exécutés SANS confirmation
        (à éviter en prod — gui.py doit fournir un vrai popup).
        """
        self._confirm_dangerous = fn

    # ── Persistance ──────────────────────────────────────────────────────

    def _load_all(self) -> None:
        self._skills.clear()
        for f in self._dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                manifest = SkillManifest.from_dict(data)
                self._skills[manifest.name] = manifest
            except Exception as exc:
                log.warning("[Skills] échec chargement %s : %s", f.name, exc)
        log.info("[Skills] %d skill(s) chargé(s)", len(self._skills))

    def _save(self, manifest: SkillManifest) -> None:
        path = self._dir / f"{manifest.name}.json"
        path.write_text(json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    # ── API publique ─────────────────────────────────────────────────────

    def create_skill(
        self,
        name: str,
        description: str = "",
        steps: Optional[list[dict]] = None,
        keywords: Optional[list[str]] = None,
    ) -> SkillManifest:
        manifest = SkillManifest(
            name=name,
            description=description,
            keywords=keywords or [name.lower()],
            steps=[SkillStep.from_dict(s) for s in (steps or [])],
            created_at=datetime.now().isoformat(),
        )
        self._skills[name] = manifest
        self._save(manifest)
        log.info("[Skills] créé : %s (%d étapes)", name, len(manifest.steps))
        return manifest

    def delete_skill(self, name: str) -> bool:
        if name not in self._skills:
            return False
        del self._skills[name]
        path = self._dir / f"{name}.json"
        if path.exists():
            path.unlink()
        return True

    def list_skills(self) -> list[SkillManifest]:
        return list(self._skills.values())

    def get(self, name: str) -> Optional[SkillManifest]:
        return self._skills.get(name)

    def run(self, name: str, initial_context: Optional[dict] = None) -> str:
        """Exécute un skill étape par étape, contexte partagé entre les steps."""
        manifest = self._skills.get(name)
        if manifest is None:
            return f"Skill inconnu : {name}"

        dangerous = manifest.required_capabilities()
        if dangerous and self._confirm_dangerous:
            if not self._confirm_dangerous(name, dangerous):
                return f"Annulé (actions sensibles : {', '.join(dangerous)})."

        if self._action_dispatcher is None:
            return "Skills engine non branché à actions.py (set_action_dispatcher manquant)."

        ctx: dict[str, Any] = dict(initial_context or {})
        outputs: list[str] = []

        for i, step in enumerate(manifest.steps):
            rendered_params = _render_value(step.params_template, ctx)
            try:
                result = self._action_dispatcher({"type": step.action, "params": rendered_params})
            except Exception as exc:
                err = f"Étape {i+1} ({step.action}) échouée : {exc}"
                log.error("[Skills] %s", err)
                outputs.append(err)
                break

            outputs.append(str(result))
            if step.output_key:
                ctx[step.output_key] = result

        manifest.run_count += 1
        manifest.last_run = datetime.now().isoformat()
        self._save(manifest)

        return "\n".join(outputs)

    def route(self, command: str) -> Optional[str]:
        """
        Cherche un skill dont un mot-clé matche la commande. Retourne None
        si aucun match (laisse main.py continuer son routing normal).
        Remplace route_dynamic() de l'ancien skills_dynamic.py.
        """
        cmd = command.lower().strip()
        for manifest in self._skills.values():
            for kw in manifest.keywords:
                if kw.lower() in cmd:
                    return self.run(manifest.name)
        return None

    def list_skills_human(self) -> str:
        if not self._skills:
            return "Aucun skill appris pour l'instant."
        lines = []
        for m in self._skills.values():
            tag = " ⚠️" if m.required_capabilities() else ""
            lines.append(f"• {m.name}{tag} — {m.description or 'pas de description'} ({m.run_count}x exécuté)")
        return "\n".join(lines)


# ── Instance globale ──────────────────────────────────────────────────────
skills_engine = SkillsEngine()


# ── Helper pour "apprends à ..." : skill simple à une seule étape LLM ──────

def quick_learn(name: str, description: str, single_action: str, params_template: Optional[dict] = None) -> str:
    """
    Raccourci pour créer un skill à une seule étape (cas le plus fréquent
    depuis le shortcut "🧠 Apprends"). Pour des skills multi-étapes, utiliser
    create_skill() directement avec une liste de steps.
    """
    skills_engine.create_skill(
        name=name,
        description=description,
        steps=[{"action": single_action, "params_template": params_template or {}, "output_key": "résultat"}],
        keywords=[name.lower()],
    )
    return f"Skill '{name}' appris ✓ (déclencheur : '{name.lower()}')"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    def _dummy_dispatcher(action: dict) -> str:
        return f"[simulé] {action['type']}({action['params']})"

    skills_engine.set_action_dispatcher(_dummy_dispatcher)
    skills_engine.create_skill(
        "test_briefing",
        description="démo multi-étapes",
        steps=[
            {"action": "meteo", "params_template": {"ville": "Paris"}, "output_key": "m"},
            {"action": "recherche_web", "params_template": {"query": "actu du jour"}, "output_key": "a"},
        ],
    )
    print(skills_engine.run("test_briefing"))
    print(skills_engine.list_skills_human())

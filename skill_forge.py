"""
skill_forge.py — ONYX Self-Evolving Skills Engine v2
Workflow : réflexion → confirmation user → génération → AST-check
           → sandbox subprocess (exec isolé) → auto-eval → injection

v2 (inspiré OpenHands) :
- Sandbox subprocess : le code généré tourne dans un process séparé avec timeout.
  ONYX ne peut PLUS crasher si un skill plante.
- Auto-eval : après génération, le skill est appelé avec un test_input.
  Doit retourner une str non-vide sans exception, sinon refusé.
- Structure enrichie : test_input + test_result par skill.

Fichiers touchés :
  skills_dynamic.py  ← reconstruit automatiquement (jamais edité à la main)
  skills/_index.json ← métadonnées de chaque skill
  skills/<id>.json   ← code + meta + test par skill
  shortcuts.py       ← nouvelle entrée (SHORTCUTS list)
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

import requests

# ── Chemins ───────────────────────────────────────────────────────────────────
_BASE       = Path(__file__).parent
SKILLS_DIR  = _BASE / "skills"
SKILLS_DYN  = _BASE / "skills_dynamic.py"
SKILLS_IDX  = SKILLS_DIR / "_index.json"
SHORTCUTS_F = _BASE / "shortcuts.py"

SKILLS_DIR.mkdir(exist_ok=True)

# ── Catégories existantes ─────────────────────────────────────────────────────
_EXISTING_CATS = {
    "Vision", "Système", "Web", "YouTube", "Média",
    "Infos", "Météo", "Messages", "Rappels", "Mémoire",
    "Power", "Fichiers", "Processus", "Debug", "Skills",
}

# ── Blacklist sécurité AST ────────────────────────────────────────────────────
_BANNED_IMPORTS = {
    "subprocess", "pty", "ctypes", "cffi", "winreg",
    "msvcrt", "importlib", "_thread", "socket",
}
_BANNED_CALLS = {
    "exec", "eval", "compile", "__import__",
    "os.system", "os.popen", "shutil.rmtree",
}

# Sandbox limits
_EVAL_TIMEOUT = 10  # secondes max pour l'auto-eval d'un skill

# ── Prompts Ollama ────────────────────────────────────────────────────────────
_REFLECT_PROMPT = """\
Tu es ONYX, assistant IA local. L'utilisateur veut t'apprendre un nouveau skill.

DEMANDE : {request}

CONTEXTE :
- Python 3.14, Windows 11, Intel Core Ultra 7 155H, PAS de GPU dédié
- Libs disponibles : requests, psutil, pyautogui, pyttsx3, BeautifulSoup4, ddgs,
  send2trash, plyer, qrcode, cv2, pytesseract, pycaw, screen_brightness_control,
  webbrowser, pathlib, shutil, json, re, os, time, datetime, threading, zipfile
- INTERDITS : subprocess, winreg, ctypes, socket, exec/eval, os.system

Analyse TOUT en détail :
1. FAISABILITÉ sur ce hardware/OS avec ces libs exactes ?
2. LIBS nécessaires : déjà dispo ? à installer ? (pip install xxx)
3. RISQUES : fichiers modifiés, processus lancés, effets de bord ?
4. INTÉGRATION : comment la commande sera tapée par l'utilisateur ?
5. KEYWORDS (3-6 phrases exactes qui déclencheront le skill)
6. RACCOURCI GUI : catégorie existante parmi {cats} ou NOUVELLE catégorie ?
   Label court du bouton (max 20 chars) ? Couleur (cyan/blue/green/orange/purple/red/grey) ?
7. TEST : une commande d'exemple SANS effet de bord pour tester le skill
   (ex: si le skill ouvre une app, le test peut être un dry-run qui retourne juste un statut)
8. PLAN implémentation : structure Python de la fonction (pas le code complet)

Termine obligatoirement par :
VERDICT: FAISABLE
ou
VERDICT: NON_FAISABLE — <raison courte>

Réponds en français. Sois honnête sur les limites.\
"""

_CODEGEN_PROMPT = """\
Tu es ONYX. Génère le code Python pour ce skill.

DEMANDE : {request}
ANALYSE : {reflection}

RÈGLES ABSOLUES :
- Fonction nommée exactement `skill_{sid}(args: str = "") -> str`
- Retourne TOUJOURS une str non-vide (résultat ou message d'erreur lisible)
- Lazy imports EN HAUT de la fonction (pas au niveau module)
- INTERDIT : subprocess, exec, eval, os.system, os.popen, winreg, ctypes, socket, __import__
- try/except autour de tout, retourne message clair si échec
- Docstring courte en français
- Zéro print(), zéro side-effect global
- Le code doit pouvoir s'exécuter sans planter même si args est vide

RÉPONDS UNIQUEMENT avec le code Python brut.
Commence DIRECTEMENT par `def skill_{sid}(`\
"""


# ── Ollama call ───────────────────────────────────────────────────────────────

def _ollama(prompt: str, model: str = "deepseek-r1:7b") -> str:
    try:
        r = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=180,
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception as exc:
        return f"[ERREUR OLLAMA] {exc}"


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


# ── Validation AST ────────────────────────────────────────────────────────────

def _validate_ast(code: str) -> tuple[bool, str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"Syntaxe invalide : {exc}"

    has_fn = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("skill_"):
            has_fn = True

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [a.name.split(".")[0] for a in node.names]
                if isinstance(node, ast.Import)
                else ([node.module.split(".")[0]] if node.module else [])
            )
            for n in names:
                if n in _BANNED_IMPORTS:
                    return False, f"Import interdit : {n}"

        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in _BANNED_CALLS:
                return False, f"Appel interdit : {fn.id}()"
            if isinstance(fn, ast.Attribute):
                full = f"{getattr(fn.value, 'id', '')}.{fn.attr}"
                if full in _BANNED_CALLS:
                    return False, f"Appel interdit : {full}()"

    if not has_fn:
        return False, "Aucune fonction skill_* trouvée"
    return True, "OK"


# ── Sandbox subprocess + auto-eval ────────────────────────────────────────────

_SANDBOX_RUNNER = '''\
# Sandbox runner — exécute le skill dans un process isolé
import json
import sys

_CODE = {code!r}
_SID = {sid!r}
_TEST_INPUT = {test_input!r}

ns = {{}}
try:
    exec(compile(_CODE, "skill_" + _SID, "exec"), ns)
except Exception as exc:
    print(json.dumps({{"ok": False, "stage": "exec", "error": str(exc)}}))
    sys.exit(0)

fn = ns.get("skill_" + _SID)
if fn is None:
    print(json.dumps({{"ok": False, "stage": "lookup", "error": "fonction introuvable"}}))
    sys.exit(0)

try:
    result = fn(_TEST_INPUT)
except Exception as exc:
    print(json.dumps({{"ok": False, "stage": "call", "error": str(exc)}}))
    sys.exit(0)

if not isinstance(result, str):
    print(json.dumps({{"ok": False, "stage": "type", "error": "retour non-str: " + type(result).__name__}}))
    sys.exit(0)

if not result.strip():
    print(json.dumps({{"ok": False, "stage": "empty", "error": "retour vide"}}))
    sys.exit(0)

print(json.dumps({{"ok": True, "result": result[:300]}}))
'''


def _sandbox_eval(code: str, sid: str, test_input: str) -> tuple[bool, str]:
    """
    Exécute le skill dans un subprocess isolé avec un test_input.
    Vérifie : exec OK + appel OK + retour str non-vide.
    ONYX ne peut pas crasher — tout se passe dans le process enfant.
    """
    runner = _SANDBOX_RUNNER.format(code=code, sid=sid, test_input=test_input)

    tmp = None
    out = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(runner)
            tmp = f.name

        proc = subprocess.run(
            [sys.executable, tmp],
            capture_output=True,
            text=True,
            timeout=_EVAL_TIMEOUT,
        )

        out = (proc.stdout or "").strip()
        if not out:
            err = (proc.stderr or "")[:300]
            return False, f"Aucune sortie (stderr: {err})"

        last_line = out.splitlines()[-1]
        verdict = json.loads(last_line)

        if verdict.get("ok"):
            return True, verdict.get("result", "OK")
        stage = verdict.get("stage", "?")
        error = verdict.get("error", "?")
        return False, f"[{stage}] {error}"

    except subprocess.TimeoutExpired:
        return False, f"Timeout sandbox (>{_EVAL_TIMEOUT}s) — boucle infinie ou blocage ?"
    except json.JSONDecodeError:
        return False, f"Sortie sandbox illisible : {out[:200]}"
    except Exception as exc:
        return False, f"Erreur sandbox : {exc}\n{traceback.format_exc()}"
    finally:
        if tmp:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass


# ── Index ─────────────────────────────────────────────────────────────────────

def _load_index() -> dict:
    if SKILLS_IDX.exists():
        try:
            return json.loads(SKILLS_IDX.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_index(idx: dict) -> None:
    SKILLS_IDX.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Rebuild skills_dynamic.py ─────────────────────────────────────────────────

def _rebuild_dynamic() -> None:
    idx = _load_index()
    lines = [
        "# skills_dynamic.py — AUTO-GÉNÉRÉ par skill_forge.py — NE PAS ÉDITER",
        f"# {datetime.now().isoformat()}",
        "",
        "# ── Fonctions des skills ─────────────────────────────────────────────────────",
        "",
    ]

    map_lines = [
        "",
        "# ── Routing map ─────────────────────────────────────────────────────────────",
        "DYNAMIC_SKILLS: dict = {",
    ]

    for sid, meta in idx.items():
        path = SKILLS_DIR / f"{sid}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        lines.append(data["code"])
        lines.append("")
        kw_repr = json.dumps(meta["keywords"], ensure_ascii=False)
        map_lines += [
            f'    "{sid}": {{',
            f'        "fn": skill_{sid},',
            f'        "keywords": {kw_repr},',
            f'        "label": {json.dumps(meta["label"])},',
            f'        "category": {json.dumps(meta["category"])},',
            "    },",
        ]

    map_lines += [
        "}",
        "",
        "",
        "def route_dynamic(command: str) -> str | None:",
        '    """Cherche un skill dynamique. Retourne None si aucun match."""',
        "    cmd = command.lower().strip()",
        "    for skill in DYNAMIC_SKILLS.values():",
        '        for kw in skill["keywords"]:',
        "            if kw.lower() in cmd:",
        "                try:",
        '                    return skill["fn"](command)',
        "                except Exception as exc:",
        '                    return f"Skill a planté : {exc}"',
        "    return None",
        "",
    ]
    lines.extend(map_lines)
    SKILLS_DYN.write_text("\n".join(lines), encoding="utf-8")


# ── Patch shortcuts.py ────────────────────────────────────────────────────────

def _patch_shortcuts(label: str, prefill: str, color: str, cat: str, is_new: bool) -> tuple[bool, str]:
    """Ajoute le raccourci dans SHORTCUTS list de shortcuts.py."""
    try:
        content = SHORTCUTS_F.read_text(encoding="utf-8")
        new_entry = f'    ("{label}", "{prefill}", "{color}", "{cat}"),'

        if not is_new:
            last_cat_idx = -1
            for m in re.finditer(re.escape(f'"{cat}"'), content):
                last_cat_idx = m.end()
            if last_cat_idx != -1:
                eol = content.find("\n", last_cat_idx)
                content = content[:eol + 1] + new_entry + "\n" + content[eol + 1:]
                SHORTCUTS_F.write_text(content, encoding="utf-8")
                return True, "shortcuts.py patché"

        insert_block = f"\n    # ── {cat} ──\n{new_entry}"
        insert_pos = content.rfind("]")
        if insert_pos == -1:
            return False, "Fin de liste SHORTCUTS introuvable"
        content = content[:insert_pos] + insert_block + "\n" + content[insert_pos:]
        SHORTCUTS_F.write_text(content, encoding="utf-8")
        return True, "shortcuts.py patché"
    except Exception as exc:
        return False, f"Erreur patch shortcuts : {exc}"


# ── Parsing réponse LLM ───────────────────────────────────────────────────────

def _make_sid(request: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", request.lower())[:28].strip("_")
    ts = datetime.now().strftime("%m%d%H%M")
    return f"{slug}_{ts}"


def _parse_keywords(text: str, request: str) -> list[str]:
    for line in text.splitlines():
        ll = line.lower()
        if any(k in ll for k in ("keyword", "trigger", "déclench", "phrase")):
            found = re.findall(r'"([^"]{3,40})"|\'([^\']{3,40})\'|`([^`]{3,40})`', line)
            flat = [g for t in found for g in t if g]
            if flat:
                return flat[:6]
            if ":" in line:
                parts = line.split(":", 1)[1]
                kws = [p.strip().strip("\"',-") for p in re.split(r"[,;]", parts) if p.strip()]
                if kws:
                    return kws[:6]
    words = [w for w in re.findall(r'\b[a-zàéèùç]{4,}\b', request.lower()) if w not in {
        "pour", "dans", "avec", "une", "les", "des", "que", "est", "apprends"
    }]
    return words[:4] if words else ["skill_onyx"]


def _parse_category(text: str) -> tuple[str, bool]:
    existing_lower = {c.lower(): c for c in _EXISTING_CATS}
    for line in text.splitlines():
        ll = line.lower()
        if any(k in ll for k in ("catégorie", "category", "raccourci", "bouton")):
            for clow, creal in existing_lower.items():
                if clow in ll:
                    return creal, False
            m = re.search(r'"([A-ZÀÉÈÙA-Za-z][^"]{1,18})"', line)
            if m:
                return m.group(1).capitalize(), True
    return "Système", False


def _parse_label(text: str, request: str) -> str:
    for line in text.splitlines():
        ll = line.lower()
        if any(k in ll for k in ("label", "bouton", "button")):
            m = re.search(r'"([^"]{2,20})"', line)
            if m:
                return m.group(1)
    words = request.split()[:3]
    return " ".join(w.capitalize() for w in words)[:20]


def _parse_color(text: str) -> str:
    colors = ("cyan", "blue", "green", "orange", "purple", "red", "grey")
    for line in text.splitlines():
        ll = line.lower()
        for c in colors:
            if c in ll and any(k in ll for k in ("couleur", "color", "bouton", "label")):
                return c
    return "blue"


def _parse_test_input(text: str) -> str:
    """Extrait la commande de test proposée par le LLM (point 7)."""
    for line in text.splitlines():
        ll = line.lower()
        if any(k in ll for k in ("test", "exemple", "dry-run", "dry run")):
            m = re.search(r'"([^"]{2,60})"|`([^`]{2,60})`', line)
            if m:
                return (m.group(1) or m.group(2)).strip()
    return "test"


# ── SkillForge principal ──────────────────────────────────────────────────────

class SkillForge:
    """Orchestre le cycle complet d'apprentissage d'un skill."""

    def __init__(self, model: str = "deepseek-r1:7b") -> None:
        self.model   = model
        self._pending: dict | None = None

    # Phase 1 : réflexion
    def reflect(self, request: str) -> str:
        prompt = _REFLECT_PROMPT.format(
            request=request,
            cats=", ".join(sorted(_EXISTING_CATS)),
        )
        raw = _strip_think(_ollama(prompt, self.model))

        feasible = "VERDICT: FAISABLE" in raw and "NON_FAISABLE" not in raw

        sid         = _make_sid(request)
        keywords    = _parse_keywords(raw, request)
        cat, is_new = _parse_category(raw)
        label       = _parse_label(raw, request)
        color       = _parse_color(raw)
        test_input  = _parse_test_input(raw)
        prefill     = request.lower().strip()

        self._pending = {
            "request":     request,
            "reflection":  raw,
            "feasible":    feasible,
            "sid":         sid,
            "keywords":    keywords,
            "category":    cat,
            "is_new_cat":  is_new,
            "label":       label,
            "color":       color,
            "test_input":  test_input,
            "prefill":     prefill,
        }

        if not feasible:
            self._pending = None
            verdict = next((l for l in raw.splitlines() if "VERDICT" in l), "Non faisable.")
            return raw + f"\n\n❌ {verdict}\n\nSkill non appris."

        return (
            raw
            + "\n\n---\n"
            "✅ **Plan prêt.** Dis **ok** pour que j'apprenne ce skill, ou ignore pour annuler.\n"
            f"_(Test auto qui sera lancé : `{test_input}`)_"
        )

    # Phase 2 : génération + sandbox + eval + injection
    def forge(self) -> str:
        if not self._pending:
            return "Aucun skill en attente. Demande-moi d'abord d'apprendre quelque chose."

        p   = self._pending
        sid = p["sid"]

        # 1. Génération code
        code_prompt = _CODEGEN_PROMPT.format(
            request=p["request"], reflection=p["reflection"], sid=sid,
        )
        raw_code = _strip_think(_ollama(code_prompt, self.model))
        raw_code = re.sub(r"```python\n?|```\n?", "", raw_code).strip()

        # 2. Validation AST (statique)
        ok, reason = _validate_ast(raw_code)
        if not ok:
            self._pending = None
            return f"❌ Validation AST échouée : {reason}\nSkill non ajouté. Reformule et réessaie."

        # 3. Sandbox subprocess + auto-eval (dynamique, isolé)
        ok, reason = _sandbox_eval(raw_code, sid, p["test_input"])
        if not ok:
            self._pending = None
            return (
                f"❌ Test sandbox échoué : {reason}\n"
                "Skill non ajouté. Le code généré ne fonctionne pas correctement.\n"
                "Reformule ta demande ou réessaie."
            )

        eval_preview = reason[:150]

        # 4. Sauvegarde JSON enrichie
        skill_data = {
            "id":          sid,
            "request":     p["request"],
            "code":        raw_code,
            "keywords":    p["keywords"],
            "category":    p["category"],
            "label":       p["label"],
            "color":       p["color"],
            "test_input":  p["test_input"],
            "test_result": eval_preview,
            "prefill":     p["prefill"],
            "created_at":  datetime.now().isoformat(),
        }
        (SKILLS_DIR / f"{sid}.json").write_text(
            json.dumps(skill_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 5. Mise à jour index
        idx = _load_index()
        idx[sid] = {
            "label":      p["label"],
            "keywords":   p["keywords"],
            "category":   p["category"],
            "is_new_cat": p["is_new_cat"],
        }
        _save_index(idx)

        # 6. Rebuild skills_dynamic.py
        _rebuild_dynamic()

        # 7. Patch shortcuts.py
        sc_ok, sc_msg = _patch_shortcuts(
            p["label"], p["prefill"], p["color"], p["category"], p["is_new_cat"]
        )

        self._pending = None

        sc_status = "✅" if sc_ok else f"⚠️ ({sc_msg})"
        cat_note  = " (nouvelle catégorie créée)" if p["is_new_cat"] else ""

        return (
            f"✅ **Skill appris : {p['label']}**\n"
            f"- ID : `{sid}`\n"
            f"- Keywords : {', '.join(p['keywords'])}\n"
            f"- Catégorie : {p['category']}{cat_note}\n"
            f"- Test sandbox : ✅ passé → {eval_preview}\n"
            f"- Raccourci (shortcuts.py) : {sc_status}\n"
            f"- Disponible immédiatement — tape un des keywords pour tester."
        )

    def cancel(self) -> str:
        self._pending = None
        return "Skill annulé. Rien n'a été modifié."

    def list_skills(self) -> str:
        idx = _load_index()
        if not idx:
            return "Aucun skill dynamique appris."
        lines = ["**Skills dynamiques :**\n"]
        for sid, meta in idx.items():
            lines.append(
                f"- **{meta['label']}** (`{sid}`) "
                f"— {meta['category']} "
                f"— keywords : {', '.join(meta['keywords'])}"
            )
        return "\n".join(lines)

    def delete_skill(self, sid: str) -> str:
        idx = _load_index()
        if sid not in idx:
            return f"Skill `{sid}` introuvable."
        del idx[sid]
        _save_index(idx)
        f = SKILLS_DIR / f"{sid}.json"
        if f.exists():
            f.unlink()
        _rebuild_dynamic()
        return f"✅ Skill `{sid}` supprimé."

    @property
    def has_pending(self) -> bool:
        return self._pending is not None


# ── Singleton ─────────────────────────────────────────────────────────────────
_forge: SkillForge | None = None


def get_forge(model: str = "deepseek-r1:7b") -> SkillForge:
    global _forge
    if _forge is None:
        _forge = SkillForge(model=model)
    return _forge

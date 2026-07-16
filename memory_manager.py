"""
memory_manager.py — ONYX Memory v2
v1 (tri chronologique only) + v2 ajoute un pruning intelligent inspiré du
pattern OpenJarvis learning_orchestrator (trace -> learn -> eval loop) mais
réduit à l'essentiel : pas de LoRA, pas de GPU — juste un appel Ollama
périodique qui juge la mémoire et décide quoi garder/fusionner/jeter.

Nouveau en v2 :
- smart_prune() : appelle le LLM pour évaluer la mémoire (doublons,
  infos obsolètes, contradictions) plutôt que de jeter par ancienneté brute
- merge_duplicates() : détecte les clés synonymes ("ville"/"localisation")
  et les fusionne avant que ça pollue le prompt
- Tout le reste (load/save/remember/forget/load_memory_prompt) est inchangé
  pour rester 100% compatible avec le code existant (gui.py, server.py, etc.)

Usage (inchangé) :
    from memory_manager import save_memory, load_memory_prompt, remember, forget
    ctx = load_memory_prompt()
    prompt = ctx + SYSTEM_PROMPT

Nouveau (optionnel, à appeler périodiquement, ex: 1x/semaine via scheduler.py) :
    from memory_manager import smart_prune
    rapport = smart_prune()
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)

# ── Stockage : AppData/Roaming/ONYX/memory.json ──────────────────────────────
try:
    import os
    _APPDATA  = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    _MEM_DIR  = _APPDATA / "ONYX"
except Exception:
    _MEM_DIR  = Path.home() / ".onyx"

MEMORY_PATH      = _MEM_DIR / "memory.json"
_lock            = Lock()          # protège load/save individuels
_update_lock     = Lock()          # rend load→modify→save ATOMIQUE (anti perte d'updates)
MAX_VALUE_LEN    = 380
MEMORY_MAX_CHARS = 2_200  # garde le contexte injecté léger


# ── Structure vide ────────────────────────────────────────────────────────────

def _empty() -> dict:
    return {
        "identite":    {},   # nom, âge, ville, job…
        "preferences": {},   # dark mode, langue préférée…
        "projets":     {},   # ONYX v9, etc.
        "contacts":    {},   # personnes mentionnées
        "notes":       {},   # tout le reste
    }


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_memory() -> dict:
    """Charge la mémoire depuis le disque. Retourne un dict vide si absent/corrompu."""
    if not MEMORY_PATH.exists():
        return _empty()
    with _lock:
        try:
            data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _empty()
            # Assure que toutes les clés existent
            base = _empty()
            for k in base:
                data.setdefault(k, {})
            return data
        except Exception as exc:
            log.warning("[Memory] Chargement échoué : %s", exc)
            return _empty()


def _trim(memory: dict) -> dict:
    """Supprime les entrées les plus vieilles si on dépasse MEMORY_MAX_CHARS."""
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory
    # Collecte toutes les entrées avec leur date
    entries: list[tuple[str, str, dict]] = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "valeur" in entry:
                entries.append((cat, key, entry))
    entries.sort(key=lambda t: t[2].get("mis_a_jour", "0000-00-00"))
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
        log.debug("[Memory] Tronqué : %s/%s", cat, key)
    return memory


def _save(memory: dict) -> None:
    memory = _trim(memory)
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ── Mise à jour récursive ─────────────────────────────────────────────────────

def _truncate(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LEN:
        return val[:MAX_VALUE_LEN].rstrip() + "…"
    return val


def _apply_update(target: dict, updates: dict) -> bool:
    changed = False
    today   = datetime.now().strftime("%Y-%m-%d")
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "valeur" not in value:
            target.setdefault(key, {})
            if _apply_update(target[key], value):
                changed = True
        else:
            new_val  = _truncate(str(value["valeur"] if isinstance(value, dict) else value))
            entry    = {"valeur": new_val, "mis_a_jour": today}
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("valeur") != new_val:
                target[key] = entry
                changed = True
    return changed


def update_memory(updates: dict) -> dict:
    """
    Met à jour la mémoire avec un dict structuré.
    Exemple :
        update_memory({"identite": {"nom": "Shab"}})
        update_memory({"projets":  {"onyx": "assistant local Python"}})
    """
    if not isinstance(updates, dict) or not updates:
        return load_memory()
    # Atomique : sans ce lock, deux updates concurrents (GUI + serveur mobile)
    # font load→modify→save croisés et l'un écrase l'autre.
    with _update_lock:
        memory = load_memory()
        if _apply_update(memory, updates):
            _save(memory)
            log.info("[Memory] Sauvegardé : %s", list(updates.keys()))
    return memory


# ── API publique simple ───────────────────────────────────────────────────────

_VALID_CATS = {"identite", "preferences", "projets", "contacts", "notes"}


def remember(key: str, value: str, categorie: str = "notes") -> str:
    """
    Mémorise une info.
    Exemple : remember("nom", "Shab", "identite")
    """
    if categorie not in _VALID_CATS:
        categorie = "notes"
    update_memory({categorie: {key: value}})
    return f"Mémorisé : {categorie}/{key} = {value}"


def forget(key: str, categorie: str = "notes") -> str:
    """Oublie une clé."""
    with _update_lock:
        memory = load_memory()
        cat    = memory.get(categorie, {})
        if key in cat:
            del cat[key]
            memory[categorie] = cat
            _save(memory)
            return f"Oublié : {categorie}/{key}"
    return f"Introuvable : {categorie}/{key}"


def load_memory_prompt() -> str:
    """
    Retourne un bloc texte à injecter en tête du SYSTEM_PROMPT.
    Vide si aucune mémoire. Léger (<2200 chars).
    """
    memory = load_memory()
    lines: list[str] = []

    # Identité
    id_fields = ["nom", "age", "ville", "job", "langue"]
    identite  = memory.get("identite", {})
    for f in id_fields:
        entry = identite.get(f)
        if entry:
            val = entry.get("valeur") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"{f.capitalize()} : {val}")
    for k, entry in identite.items():
        if k in id_fields:
            continue
        val = entry.get("valeur") if isinstance(entry, dict) else entry
        if val:
            lines.append(f"{k.replace('_', ' ').capitalize()} : {val}")

    # Préférences
    prefs = memory.get("preferences", {})
    if prefs:
        lines.append("Préférences :")
        for k, entry in list(prefs.items())[:10]:
            val = entry.get("valeur") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {k.replace('_', ' ').capitalize()} : {val}")

    # Projets
    projets = memory.get("projets", {})
    if projets:
        lines.append("Projets actifs :")
        for k, entry in list(projets.items())[:6]:
            val = entry.get("valeur") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {k.replace('_', ' ').capitalize()} : {val}")

    # Contacts
    contacts = memory.get("contacts", {})
    if contacts:
        lines.append("Contacts :")
        for k, entry in list(contacts.items())[:8]:
            val = entry.get("valeur") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {k.replace('_', ' ').capitalize()} : {val}")

    # Notes
    notes = memory.get("notes", {})
    if notes:
        lines.append("Notes :")
        for k, entry in list(notes.items())[:6]:
            val = entry.get("valeur") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {k} : {val}")

    if not lines:
        return ""

    header = "[CE QUE TU SAIS SUR L'UTILISATEUR — utilise naturellement, ne récite pas]\n"
    result = header + "\n".join(lines) + "\n\n"
    return result[:2200]


# ══════════════════════════════════════════════════════════════════════════
# v2 — Pruning intelligent (inspiré OpenJarvis learning_orchestrator,
# réduit à un seul appel LLM léger, pas de fine-tuning / pas de GPU requis)
# ══════════════════════════════════════════════════════════════════════════

_PRUNE_PROMPT = (
    "Tu vas nettoyer une mémoire JSON d'assistant personnel. Voici son contenu actuel :\n\n"
    "{memoire_json}\n\n"
    "Tâche : identifie UNIQUEMENT les problèmes suivants s'ils existent :\n"
    "1. Doublons / clés synonymes qui devraient être fusionnées (ex: 'ville' et 'localisation')\n"
    "2. Informations clairement obsolètes ou contradictoires entre elles\n"
    "3. Notes vagues/inutiles qui n'apportent rien (ex: 'note: ok')\n\n"
    "Réponds UNIQUEMENT en JSON avec ce format strict, sans aucun texte autour :\n"
    '{{"a_supprimer": [["categorie", "cle"], ...], "raison": "explication courte en français"}}\n\n'
    "Si rien à nettoyer, réponds {{\"a_supprimer\": [], \"raison\": \"mémoire propre\"}}."
)


def smart_prune(dry_run: bool = False) -> str:
    """
    Demande au LLM local (Ollama) d'auditer la mémoire et de proposer des
    suppressions (doublons, infos obsolètes, notes inutiles). Contrairement
    à _trim() qui élague par ancienneté pure, ceci juge le CONTENU.

    dry_run=True : retourne le rapport sans rien supprimer.
    Pensé pour être appelé périodiquement via scheduler.py
    (ex: "tous les jours à 4h fais le ménage dans ta mémoire").

    Coût : 1 appel LLM, quelques centaines de tokens. Pas de GPU requis,
    pas d'entraînement — contrairement au pipeline LoRA d'OpenJarvis dont
    ce module reprend l'idée (boucle d'auto-amélioration) en version
    compatible avec un Lenovo sans carte graphique dédiée.
    """
    memory = load_memory()
    mem_json = json.dumps(memory, ensure_ascii=False, indent=2)

    try:
        import requests
        from config import MODEL_NAME, OLLAMA_URL, LLM_TIMEOUT
    except ImportError:
        return "smart_prune nécessite config.py (MODEL_NAME, OLLAMA_URL) — non disponible en standalone."

    prompt = _PRUNE_PROMPT.format(memoire_json=mem_json)
    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL_NAME,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            timeout=LLM_TIMEOUT,
        )
        r.raise_for_status()
        raw = r.json()["message"]["content"]
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    except Exception as exc:
        log.error("[Memory] smart_prune LLM échoué : %s", exc)
        return f"Pruning échoué (LLM indisponible) : {exc}"

    # Extraction JSON tolérante (le LLM peut entourer de ```json ou de texte)
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return f"Réponse LLM non-JSON, pruning annulé. Brut : {raw[:200]}"

    try:
        decision = json.loads(match.group(0))
    except json.JSONDecodeError:
        return f"JSON invalide, pruning annulé. Brut : {raw[:200]}"

    a_supprimer = decision.get("a_supprimer", [])
    raison      = decision.get("raison", "")

    if not a_supprimer:
        return f"Mémoire propre, rien à supprimer. ({raison})"

    if dry_run:
        cibles = ", ".join(f"{cat}/{key}" for cat, key in a_supprimer)
        return f"[dry-run] Suppressions proposées : {cibles}\nRaison : {raison}"

    removed = []
    with _update_lock:
        memory = load_memory()
        for cat, key in a_supprimer:
            if cat in memory and key in memory[cat]:
                del memory[cat][key]
                removed.append(f"{cat}/{key}")
        if removed:
            _save(memory)

    log.info("[Memory] smart_prune a supprimé : %s", removed)
    return f"Nettoyage terminé : {len(removed)} entrée(s) supprimée(s) ({', '.join(removed)}).\nRaison : {raison}"


# ── Fusion de doublons par alias connus (rapide, sans LLM) ──────────────────

_KNOWN_ALIASES: dict[str, list[str]] = {
    "ville": ["localisation", "lieu", "city"],
    "job":   ["travail", "metier", "profession", "emploi"],
    "nom":   ["prenom", "name"],
}


def merge_duplicates() -> str:
    """
    Fusion rapide sans LLM : si une clé connue (ex: 'ville') ET un de ses
    alias (ex: 'localisation') existent tous les deux dans la même
    catégorie, garde la plus récente et supprime l'alias.
    Complète smart_prune() pour les cas évidents, sans coût LLM.
    """
    merged: list[str] = []
    with _update_lock:
        memory = load_memory()
        for cat, items in memory.items():
            if not isinstance(items, dict):
                continue
            for canonical, aliases in _KNOWN_ALIASES.items():
                if canonical not in items:
                    continue
                for alias in aliases:
                    if alias in items:
                        canon_date = items[canonical].get("mis_a_jour", "") if isinstance(items[canonical], dict) else ""
                        alias_date = items[alias].get("mis_a_jour", "") if isinstance(items[alias], dict) else ""
                        if alias_date > canon_date:
                            items[canonical] = items[alias]
                        del items[alias]
                        merged.append(f"{cat}/{alias} → {cat}/{canonical}")
        if merged:
            _save(memory)

    if not merged:
        return "Aucun doublon connu détecté."
    return f"Fusionné : {', '.join(merged)}"


# ── CLI utilitaire ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if not args:
        print("Usage:")
        print("  python memory_manager.py show")
        print("  python memory_manager.py set <categorie> <clé> <valeur>")
        print("  python memory_manager.py forget <categorie> <clé>")
        print("  python memory_manager.py prune [--dry-run]")
        print("  python memory_manager.py merge")
        sys.exit(0)

    cmd = args[0]
    if cmd == "show":
        prompt = load_memory_prompt()
        print(prompt if prompt else "(mémoire vide)")
    elif cmd == "set" and len(args) >= 4:
        cat, key, val = args[1], args[2], " ".join(args[3:])
        print(remember(key, val, cat))
    elif cmd == "forget" and len(args) >= 3:
        cat, key = args[1], args[2]
        print(forget(key, cat))
    elif cmd == "prune":
        dry = "--dry-run" in args
        print(smart_prune(dry_run=dry))
    elif cmd == "merge":
        print(merge_duplicates())
    else:
        print("Commande inconnue.")

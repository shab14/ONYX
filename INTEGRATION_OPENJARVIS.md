# INTEGRATION_OPENJARVIS.md — comment brancher les 3 nouveaux fichiers

3 nouveaux fichiers à copier dans `C:\Users\shabd\ONYX\` :
  - scheduler.py       (NOUVEAU — tâches récurrentes)
  - skills_engine.py   (NOUVEAU — skills multi-étapes, coexiste avec skill_forge.py)
  - memory_manager.py  (REMPLACE l'existant — 100% compatible + smart_prune/merge_duplicates en plus)

Rien d'autre n'est remplacé automatiquement : les patchs ci-dessous sont à
copier-coller à la main dans actions.py / main.py / shortcuts.py / config.py
pour les activer. Chaque bloc indique où l'insérer.

────────────────────────────────────────────────────────────────────────────
## 1. config.py — ajouter en bas du fichier

```python
# ── Skills Engine v2 (multi-étapes) + Scheduler (tâches récurrentes) ────────
SKILLS_ENGINE_ENABLED  = True
SCHEDULER_ENABLED      = True
SCHEDULER_POLL_SECONDS = 30   # fréquence de vérification des tâches dues
```

────────────────────────────────────────────────────────────────────────────
## 2. actions.py — ajouter à la fin du fichier (après _ACTIONS et executer_action)

```python
# ── Skills Engine v2 — branchement (multi-étapes, indépendant de skill_forge) ─
def _init_skills_engine() -> None:
    """Branche skills_engine.py sur le dispatcher _ACTIONS existant."""
    try:
        from skills_engine import skills_engine
        skills_engine.set_action_dispatcher(executer_action)
        # Confirmation pour actions dangereuses : popup GUI géré séparément
        # via skills_engine.set_confirm_callback(...) dans gui.py au démarrage.
    except ImportError:
        pass

_init_skills_engine()
```

────────────────────────────────────────────────────────────────────────────
## 3. main.py — modifier le bloc d'import des skills (vers la ligne 71)

REMPLACER :
```python
try:
    from skill_forge import get_forge
    from skills_dynamic import route_dynamic
    _FORGE_OK = True
except ImportError:
    _FORGE_OK = False
    def route_dynamic(_cmd: str) -> None: return None
```

PAR :
```python
try:
    from skill_forge import get_forge
    from skills_dynamic import route_dynamic
    _FORGE_OK = True
except ImportError:
    _FORGE_OK = False
    def route_dynamic(_cmd: str) -> None: return None

# ── Skills Engine v2 (multi-étapes) — coexiste avec skill_forge (un seul mot-clé) ─
try:
    from skills_engine import skills_engine, quick_learn
    _SKILLS_V2_OK = True
except ImportError:
    _SKILLS_V2_OK = False
    def quick_learn(*a, **k): return "skills_engine indisponible"

# ── Scheduler (tâches récurrentes) ───────────────────────────────────────────
try:
    from scheduler import scheduler, parse_and_schedule, list_tasks_human
    _SCHEDULER_OK = True
except ImportError:
    _SCHEDULER_OK = False
    def parse_and_schedule(_t: str) -> str: return "scheduler indisponible"
    def list_tasks_human() -> str: return "scheduler indisponible"
```

PUIS, dans le bloc `router()` là où `route_dynamic(txt)` est appelé (~ligne 1168),
AJOUTER juste avant :

```python
        # Routing vers skills v2 (multi-étapes) avant l'ancien système mono-step
        if _SKILLS_V2_OK:
            v2 = skills_engine.route(txt)
            if v2 is not None:
                return v2

        # Planification de tâches récurrentes : "tous les jours à 8h ...", etc.
        if _SCHEDULER_OK and any(
            kw in lower for kw in ("tous les jours à", "toutes les", "chaque lundi",
                                     "chaque mardi", "chaque mercredi", "chaque jeudi",
                                     "chaque vendredi", "chaque samedi", "chaque dimanche")
        ):
            return parse_and_schedule(txt)

        if _SCHEDULER_OK and lower in ("mes tâches planifiées", "mes taches planifiees", "mes tâches"):
            return list_tasks_human()

        # Routing vers skills dynamiques existants
        dyn = route_dynamic(txt)
        if dyn is not None:
            return dyn
```

ENFIN, démarrer le scheduler une fois `chat_llm` défini (à la fin du fichier,
juste avant la fin — ou appelé explicitement depuis gui.py/server.py au
démarrage, voir bloc 5 ci-dessous) :

```python
def start_scheduler() -> None:
    """À appeler une fois au démarrage de l'app (gui.py ou server.py)."""
    if _SCHEDULER_OK:
        scheduler.start(executor=lambda prompt: chat_llm(prompt))
```

────────────────────────────────────────────────────────────────────────────
## 4. shortcuts.py — ajouter ces lignes dans la liste SHORTCUTS (catégorie Skills/Rappels)

```python
    ("⏰ Planifier",           "tous les jours à ",                   "orange", "Rappels"),
    ("📅 Mes tâches planif.",  "mes tâches planifiées",                "blue",   "Rappels"),
```

(Catégorie "Skills" existe déjà avec "🧠 Apprends" / "📋 Mes skills" — ceux-là
routent déjà vers skill_forge ; skills_engine v2 répond aux mêmes mots-clés
de listing si tu veux migrer complètement plus tard, mais pour l'instant les
deux systèmes coexistent sans conflit.)

────────────────────────────────────────────────────────────────────────────
## 5. gui.py / server.py — démarrer le scheduler au lancement

Dans `gui.py`, dans `__init__` de la fenêtre principale (après les autres
initialisations, avant `mainloop()`) :

```python
try:
    from main import start_scheduler
    start_scheduler()
except Exception as exc:
    log.warning("[GUI] scheduler non démarré : %s", exc)
```

Dans `server.py`, dans le hook FastAPI `@app.on_event("startup")` (ou
équivalent) :

```python
@app.on_event("startup")
async def _startup_scheduler():
    try:
        from main import start_scheduler
        start_scheduler()
    except Exception:
        log.warning("Scheduler non démarré côté serveur mobile")
```

⚠️ Important : ne démarrer le scheduler qu'UNE FOIS au total (soit gui.py,
soit server.py selon lequel est lancé en premier) — pas les deux en même
temps si tu lances les deux process, sinon les tâches dues s'exécuteraient
deux fois. Solution simple si tu lances toujours gui.py + server.py
ensemble : ne mets le `start_scheduler()` QUE dans gui.py.

────────────────────────────────────────────────────────────────────────────
## 6. Confirmation des actions dangereuses dans skills_engine (gui.py)

Pour éviter qu'un skill créé via "apprends à éteindre l'ordi si CPU > 90%"
exécute un shutdown sans confirmation, brancher un vrai popup dans gui.py :

```python
def _confirm_dangerous_skill(skill_name: str, dangerous_actions: list[str]) -> bool:
    import tkinter.messagebox as mb
    actions_str = ", ".join(dangerous_actions)
    return mb.askyesno(
        "Confirmation requise",
        f"Le skill « {skill_name} » va exécuter : {actions_str}\nContinuer ?",
    )

try:
    from skills_engine import skills_engine
    skills_engine.set_confirm_callback(_confirm_dangerous_skill)
except ImportError:
    pass
```

────────────────────────────────────────────────────────────────────────────
## Ce qui N'A PAS été porté depuis OpenJarvis (volontairement)

- LoRA fine-tuning / GRPO training → nécessite GPU, hors scope Lenovo 155H
- Channels multi-plateforme (Slack/Discord/Teams/WhatsApp Baileys) →
  ONYX a déjà WhatsApp/Telegram/Discord/Signal basique dans envoyer_message();
  le système de channels complet d'OpenJarvis est pour du multi-tenant cloud
- MCP server/client → ajoute une couche protocole; à reconsidérer si ONYX
  doit un jour parler à d'autres agents externes
- Mining / Pearl (crypto-training distribué) → totalement hors sujet
- Evals framework (GAIA, SWE-bench, etc.) → benchmarking multi-agent,
  pas pertinent pour un assistant perso mono-utilisateur
- Sandbox Docker/WASM → ONYX tourne déjà en natif Windows, pas de sandboxing
  de ce niveau nécessaire pour du code perso

Tout ce qui restait (skills manifests, scheduler récurrent, pattern
trace→learn→eval allégé pour la mémoire) est maintenant dans ONYX.

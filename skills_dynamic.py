# skills_dynamic.py — AUTO-GÉNÉRÉ par skill_forge.py — NE PAS ÉDITER
# init

# ── Fonctions des skills ──────────────────────────────────────────────────────


# ── Routing map ───────────────────────────────────────────────────────────────
DYNAMIC_SKILLS: dict = {}


def route_dynamic(command: str) -> str | None:
    """Cherche un skill dynamique. Retourne None si aucun match."""
    cmd = command.lower().strip()
    for skill in DYNAMIC_SKILLS.values():
        for kw in skill["keywords"]:
            if kw.lower() in cmd:
                return skill["fn"](command)
    return None

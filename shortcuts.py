"""
shortcuts.py — ONYX · source UNIQUE des raccourcis (v11)
Avant : la liste était dupliquée dans gui.py ET server.py → dérive
(le tel avait moins de raccourcis que le PC). Désormais les deux importent d'ici.

Format : (label, prefill, color_key, categorie)
- color_key ∈ {cyan, blue, green, orange, purple, red, grey}
- prefill terminé par un espace = commande à compléter (l'utilisateur tape la suite)
  sinon = commande complète. Sur mobile comme sur PC : PRE-REMPLIT (jamais d'envoi
  direct → évite les arrêts PC accidentels).
"""
from __future__ import annotations

# Catégories dans l'ordre d'affichage
SHORTCUTS: list[tuple[str, str, str, str]] = [
    # ── Vision (la star) ──
    ("👁 Aide moi",            "aide moi",                            "cyan",   "Vision"),
    ("🔴 C'est quoi l'erreur", "aide moi, c'est quoi cette erreur ?", "cyan",   "Vision"),
    ("📝 Explique moi",        "aide moi, explique moi ",             "cyan",   "Vision"),

    # ── Système ──
    ("🚀 Ouvrir app",          "ouvre ",                              "blue",   "Système"),
    ("📄 Créer fichier",       "crée un fichier ",                    "green",  "Système"),
    ("📂 Ouvrir fichier",      "ouvre le fichier ",                   "orange", "Système"),
    ("→ Claude",               "demande à claude de ",                "purple", "Système"),

    # ── Web ──
    ("🔍 Chercher",            "cherche ",                            "blue",   "Web"),
    ("🌐 Ouvrir site",         "va sur ",                             "green",  "Web"),
    ("📄 Lire page",           "lis la page ",                        "orange", "Web"),

    # ── YouTube ──
    ("🎬 Jouer",               "joue ",                               "red",    "YouTube"),
    ("📺 Résumer vidéo",       "résume cette vidéo ",                 "purple", "YouTube"),

    # ── Média ──
    ("📸 Screenshot",          "screenshot",                          "purple", "Média"),
    ("🔊 Vol +",               "monte le volume",                     "green",  "Média"),
    ("🔉 Vol -",               "baisse le volume",                    "orange", "Média"),
    ("🔇 Mute",                "coupe le son",                        "grey",   "Média"),
    ("⏯️ Play/Pause",          "pause musique",                       "green",  "Média"),
    ("⏭️ Suivant",             "piste suivante",                      "green",  "Média"),
    ("⏮️ Précédent",           "piste précédente",                    "green",  "Média"),

    # ── Infos / Écran ──
    ("🖥️ Infos PC",            "infos pc",                            "blue",   "Infos"),
    ("💡 Lumin. +",            "monte la luminosité",                 "orange", "Infos"),
    ("🌑 Lumin. -",            "baisse la luminosité",                "grey",   "Infos"),

    # ── Météo ──
    ("🌦️ Météo",               "météo ",                              "cyan",   "Météo"),

    # ── Messages ──
    ("💬 Message",             "envoie à ",                           "green",  "Messages"),

    # ── Rappels ──
    ("⏰ Rappel",              "rappelle-moi dans ",                  "orange", "Rappels"),
    ("📋 Mes rappels",         "mes rappels",                         "blue",   "Rappels"),

    # ── Mémoire ──
    ("🧠 Mémorise",            "mémorise que ",                       "purple", "Mémoire"),

    # ── Skills dynamiques ──
    ("🧠 Apprends",            "apprends à ",                         "cyan",   "Skills"),
    ("📋 Mes skills",          "liste mes skills",                    "blue",   "Skills"),

    # ── Power ──
    ("⏻ Éteindre",            "éteins l'ordi",                       "red",    "Power"),
    ("🔄 Redémarrer",          "redémarre l'ordi",                    "orange", "Power"),
    ("😴 Veille",              "mets en veille",                      "grey",   "Power"),
    ("🔒 Verrouiller",         "verrouille l'ordi",                   "purple", "Power"),

    # ── Fichiers ──
    ("🗑️ Supprimer",           "supprime le fichier ",                "red",    "Fichiers"),
    ("✏️ Renommer",            "renomme ",                            "blue",   "Fichiers"),
    ("📋 Copier",              "copie ",                              "green",  "Fichiers"),
    ("📦 Déplacer",            "déplace ",                            "orange", "Fichiers"),
    ("📂 Lister",              "liste les fichiers dans ",            "blue",   "Fichiers"),
    ("🗜️ Zipper",              "zippe ",                              "purple", "Fichiers"),
    ("📨 Dézipper",            "dézippe ",                            "green",  "Fichiers"),
    ("📁 Ouvrir dossier",      "ouvre le dossier ",                   "orange", "Fichiers"),
    ("🔎 Trouver",             "cherche le fichier ",                 "cyan",   "Fichiers"),
    ("⚖️ Taille",              "taille de ",                          "grey",   "Fichiers"),

    # ── Processus ──
    ("🪟 Apps ouvertes",       "quelles apps sont ouvertes",          "blue",   "Processus"),
    ("❌ Fermer app",          "ferme ",                              "orange", "Processus"),
    ("💀 Kill",                "kill ",                               "red",    "Processus"),
    ("🔥 Top CPU",             "top cpu",                             "orange", "Processus"),
    ("🧠 Top RAM",             "top ram",                             "purple", "Processus"),

    # ── Debug ──
    ("📋 Logs",                "montre les logs",                     "grey",   "Debug"),
    ("⏺️ Rec ▶",               "enregistre l'écran",                  "red",    "Debug"),
    ("⏹️ Rec ■",               "arrête l'enregistrement",             "green",  "Debug"),
]

# color_key → hex (desktop CustomTkinter)
COLOR_HEX: dict[str, str] = {
    "cyan":   "#22d3ee",
    "blue":   "#60a5fa",
    "green":  "#4ade80",
    "orange": "#fb923c",
    "purple": "#a78bfa",
    "red":    "#f87171",
    "grey":   "#888888",
}


def by_category() -> dict[str, list[tuple[str, str, str]]]:
    """Regroupe en {categorie: [(label, prefill, color_key), …]} (ordre préservé)."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    for label, fill, color, cat in SHORTCUTS:
        out.setdefault(cat, []).append((label, fill, color))
    return out

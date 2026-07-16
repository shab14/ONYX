"""
main.py — ONYX v9 backend · importé par gui.py et server.py
NOUVEAUTÉS v9 :
- memory_manager : contexte utilisateur injecté dans SYSTEM_PROMPT
- youtube_jouer / youtube_resumer : nouvelles commandes
- meteo : météo par ville
- envoyer_message : WhatsApp/Telegram/Discord/Signal
- recherche_web : DDG API + résumé Ollama (remplace scraping HTML)
- router : nouveaux blocs KW_YOUTUBE / KW_METEO / KW_MSG

CONSERVÉ de v8 :
- _call_ollama : gère JSONDecodeError si Ollama renvoie HTML d'erreur
- ConversationState thread-safe (Lock)
- chat_llm accepte un state optionnel → server.py peut avoir son propre contexte
- _parse_with_llm : log l'exception au lieu de swallow total
- config : LOG_FILE séparé de SCREENSHOTS_DIR (dans AppData/Roaming/ONYX)
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from config import (
    CONV_MAX_MESSAGES, LLM_TIMEOUT, LOG_FILE, MODEL_NAME, OLLAMA_URL,
    PARSE_TIMEOUT, SYSTEM_PROMPT, USER_HOME,
)

try:
    from actions import executer_action
    ACTIONS_OK = True
except ImportError:
    ACTIONS_OK = False
    def executer_action(_a): return "Module actions non chargé."

try:
    from claude_browser import demander_a_claude  # noqa: F401
    CLAUDE_OK = True
except ImportError:
    CLAUDE_OK = False

try:
    from vision import aide_moi, statut_vision
    VISION_OK = True
except ImportError:
    VISION_OK = False
    def aide_moi(q="", fichier=""): return "Module vision non chargé."
    def statut_vision(): return "Vision ✗"

try:
    from reminders import creer_rappel, lister_rappels, annuler_rappel, load_from_disk, start_scheduler
    _REMINDERS_OK = True
except ImportError:
    _REMINDERS_OK = False

try:
    from memory_manager import load_memory_prompt, update_memory, remember as mem_remember
    _MEMORY_OK = True
except ImportError:
    _MEMORY_OK = False
    def load_memory_prompt(): return ""
    def update_memory(u): return {}
    def mem_remember(k, v, c="notes"): return ""

# ── Skill Forge (self-evolving skills) ───────────────────────────────────────
try:
    from skill_forge import get_forge
    from skills_dynamic import route_dynamic
    _FORGE_OK = True
except ImportError:
    _FORGE_OK = False
    def route_dynamic(_cmd: str) -> None: return None

log = logging.getLogger(__name__)


def _setup_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_onyx_configured", False):
        return
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(console)

    try:
        from logging.handlers import RotatingFileHandler
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fileh = RotatingFileHandler(
            LOG_FILE, maxBytes=512_000, backupCount=2, encoding="utf-8",
        )
        fileh.setLevel(logging.INFO)
        fileh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(fileh)
    except OSError:
        pass

    root._onyx_configured = True


_setup_logging()

# ── Rappels ───────────────────────────────────────────────────────────────────
if _REMINDERS_OK:
    try:
        n = load_from_disk()
        start_scheduler()
        if n:
            log.info("[Main] %d rappel(s) rechargé(s)", n)
    except Exception as _exc:
        log.warning("[Main] Reminders init échoué : %s", _exc)


# ── Keywords ──────────────────────────────────────────────────────────────────

KW_VISION = frozenset({
    "aide moi", "aide-moi",
    "lis mon écran", "lis mon ecran",
    "regarde mon écran", "regarde mon ecran",
    "analyse mon écran", "analyse mon ecran",
    "que vois-tu", "que vois tu",
    "explique ce que tu vois",
    "c'est quoi cette erreur",
})

KW_CLAUDE = frozenset({
    "demande à claude", "dis à claude", "envoie à claude",
    "demande a claude", "dis a claude", "envoie a claude",
    "va sur claude",
})

KW_APP_PREFIX = ("ouvre ", "lance ", "démarre ", "demarre ")

KW_CREATE_FILE = frozenset({
    "crée un fichier", "cree un fichier",
    "crée le fichier", "cree le fichier",
    "nouveau fichier", "crée moi un fichier", "cree moi un fichier",
})

KW_OPEN_FILE = frozenset({
    "ouvre le fichier", "ouvre mon fichier",
    "ouvre le doc", "ouvre le document",
    "ouvre la video", "ouvre la vidéo",
    "ouvre le mp3", "ouvre le pdf", "ouvre l'image",
})

KW_DELETE_FILE = frozenset({
    "supprime le fichier", "supprime le dossier", "supprime ",
    "efface le fichier", "efface ", "mets à la corbeille ",
    "met à la corbeille ", "jette le fichier ",
})
KW_OPEN_FOLDER = frozenset({
    "ouvre le dossier ", "ouvre le répertoire ", "ouvre le repertoire ",
    "ouvre le folder ", "montre le dossier ",
})
KW_LIST_DIR = frozenset({
    "liste les fichiers", "liste le dossier", "liste le contenu",
    "contenu du dossier", "qu'y a-t-il dans", "qu'y a t il dans",
    "montre le contenu de",
})
KW_ZIP = frozenset({
    "zippe ", "zip ", "compresse ", "archive le dossier ", "archive ",
})
KW_UNZIP = frozenset({
    "dézippe ", "dezippe ", "décompresse ", "decompresse ", "unzip ", "extrais ",
})
KW_SEARCH_FILE = frozenset({
    "cherche le fichier ", "trouve le fichier ", "cherche les fichiers ",
    "trouve les fichiers ", "où est le fichier ", "ou est le fichier ",
    "recherche le fichier ",
})
KW_SIZE = frozenset({
    "taille de ", "taille du fichier ", "taille du dossier ",
    "poids de ", "combien pèse ", "combien pese ", "quelle taille fait ",
})
KW_RENAME = ("renomme ", "rename ")
KW_COPY   = ("copie ", "copier ", "duplique ")
KW_MOVE   = ("déplace ", "deplace ", "bouge ")

KW_LIST_APPS = frozenset({
    "quelles apps sont ouvertes", "liste les apps", "apps ouvertes",
    "fenêtres ouvertes", "fenetres ouvertes", "liste les fenêtres",
    "liste les fenetres", "quelles fenêtres", "qu'est-ce qui est ouvert",
})
KW_CLOSE_APP = ("ferme ", "ferme l'app ", "ferme la fenêtre ", "ferme la fenetre ")
KW_KILL      = ("kill ", "tue le process ", "tue ", "force la fermeture de ", "force quitte ")
KW_TOP_CPU   = frozenset({
    "top cpu", "processus cpu", "qui bouffe le cpu", "qui mange le cpu",
    "top 5 cpu", "utilisation cpu détaillée", "quel process utilise le cpu",
})
KW_TOP_RAM = frozenset({
    "top ram", "processus ram", "qui bouffe la ram", "qui mange la ram",
    "top 5 ram", "utilisation ram détaillée", "quel process utilise la ram",
    "top mémoire", "top memoire",
})

KW_LOGS = frozenset({
    "montre les logs", "affiche les logs", "les logs", "logs onyx",
    "voir les logs", "derniers logs", "log onyx",
})

KW_REMINDER_PREFIX = (
    "rappelle-moi ", "rappelle moi ", "rappel ",
    "dans ", "souviens-moi ", "n'oublie pas ",
)
KW_LIST_REMINDERS = frozenset({
    "mes rappels", "liste les rappels", "rappels actifs",
    "quels rappels", "affiche les rappels",
})
KW_CANCEL_REMINDER_PREFIX = (
    "annule le rappel ", "annuler le rappel ",
    "supprime le rappel ", "efface le rappel ",
)
KW_REC_START = frozenset({
    "enregistre l'écran", "enregistre l'ecran", "démarre l'enregistrement",
    "demarre l'enregistrement", "commence à enregistrer", "commence a enregistrer",
    "lance l'enregistrement", "record écran", "record ecran", "filme l'écran",
    "filme l'ecran",
})
KW_REC_STOP = frozenset({
    "arrête l'enregistrement", "arrete l'enregistrement", "stop l'enregistrement",
    "stoppe l'enregistrement", "fin de l'enregistrement", "termine l'enregistrement",
    "arrête de filmer", "arrete de filmer",
})

KW_SCREENSHOT = frozenset({
    "screenshot", "capture d'écran", "capture ecran",
    "prends une capture", "fais un screenshot",
    "prends un screenshot", "capture l'écran",
})

KW_VOL_UP = frozenset({
    "monte le volume", "monte le son",
    "augmente le volume", "augmente le son",
    "plus fort", "volume up", "volume +",
})
KW_VOL_DOWN = frozenset({
    "baisse le volume", "baisse le son",
    "diminue le volume", "diminue le son",
    "moins fort", "volume down", "volume -",
})
KW_VOL_MUTE = frozenset({
    "coupe le son", "coupe le volume",
    "mute le son", "mets en sourdine",
    "unmute", "remettre le son",
})

KW_SHUTDOWN = frozenset({
    "éteins l'ordi", "eteins l'ordi",
    "éteins le pc", "eteins le pc",
    "éteins l'ordinateur", "eteins l'ordinateur",
    "shutdown maintenant",
})
KW_RESTART = frozenset({
    "redémarre l'ordi", "redemarre l'ordi",
    "redémarre le pc", "redemarre le pc",
    "redémarre maintenant", "redemarre maintenant",
    "reboot maintenant",
})
KW_SLEEP = frozenset({
    "mets en veille", "mode veille",
    "mets l'ordi en veille",
    "suspendre l'ordi", "hiberner l'ordi",
})
KW_LOCK = frozenset({
    "verrouille l'ordi", "verrouille le pc",
    "verrouille la session", "verrouiller la session",
    "bloque l'écran", "bloque la session",
    "lock pc", "lock l'ordi",
})

KW_INFOS = frozenset({
    "infos système", "infos systeme", "infos pc",
    "état du pc", "etat du pc",
    "stats système", "stats pc",
    "utilisation cpu", "utilisation ram",
    "niveau batterie",
})

KW_MEDIA_PLAY = frozenset({
    "play pause", "play/pause",
    "mets en pause la musique", "mets pause",
    "reprends la musique", "reprends la lecture",
    "pause musique", "musique pause",
})
KW_MEDIA_NEXT = frozenset({
    "piste suivante", "chanson suivante", "musique suivante",
    "morceau suivant", "track suivant",
    "skip musique", "skip chanson",
})
KW_MEDIA_PREV = frozenset({
    "piste précédente", "piste precedente",
    "chanson précédente", "chanson precedente",
    "musique précédente", "musique precedente",
    "morceau précédent", "track précédent",
})
KW_MEDIA_STOP = frozenset({
    "arrête la musique", "arrete la musique",
    "coupe la musique", "stop la musique",
    "arrêter la lecture", "arreter la lecture",
})

KW_LUMIN_UP = frozenset({
    "augmente la luminosité", "augmente la luminosite",
    "monte la luminosité", "monte la luminosite",
    "écran plus lumineux", "luminosité +",
})
KW_LUMIN_DOWN = frozenset({
    "baisse la luminosité", "baisse la luminosite",
    "diminue la luminosité", "diminue la luminosite",
    "écran moins lumineux", "luminosité -",
})

KW_WEB_SEARCH_PREFIX = (
    "cherche ", "recherche ",
    "cherche moi ", "recherche moi ",
    "googler ", "trouve sur internet ",
    "cherche sur internet ", "recherche sur internet ",
    "cherche sur le web ",
)

KW_OPEN_URL_PREFIX = (
    "ouvre le site ", "va sur ",
    "navigue vers ", "ouvre https://", "ouvre http://",
    "ouvre le lien ", "ouvre l'url ",
)

KW_SCRAPE_PREFIX = (
    "lis la page ", "scrape ", "scrape la page ",
    "récupère le contenu de ", "recupere le contenu de ",
    "lis le contenu de ", "lis le site ",
)

# ── NOUVEAU v9 ────────────────────────────────────────────────────────────────

KW_YOUTUBE_PREFIX = (
    "joue ", "joue moi ", "lance la vidéo ", "lance la video ",
    "mets ", "mets moi ", "cherche sur youtube ",
    "youtube ", "ouvre youtube ",
)
KW_YOUTUBE_RESUME_PREFIX = (
    "résume cette vidéo ", "resume cette video ",
    "résume la vidéo ", "resume la video ",
    "transcris ", "summarize ",
)

KW_METEO_PREFIX = (
    "météo ", "meteo ", "météo à ", "meteo a ",
    "météo de ", "meteo de ", "quel temps fait-il ",
    "quel temps à ", "quel temps a ",
)

KW_MSG_PREFIX = (
    "envoie un message à ", "envoie un message a ",
    "envoie à ", "envoie a ",
    "envoie un msg à ", "envoie un msg a ",
    "envoie message à ", "envoie message a ",
    "message à ", "message a ",
    "dis à ", "dis a ",
)

# ── Memory : "mémorise que…" / "souviens-toi que…"
KW_MEMORY_PREFIX = (
    "mémorise que ", "memorise que ",
    "souviens-toi que ", "souviens toi que ",
    "retiens que ", "note que ",
    "n'oublie pas que ", "noublie pas que ",
)
KW_MEMORY_FORGET_PREFIX = (
    "oublie ", "efface de ta mémoire ", "efface de ta memoire ",
    "supprime de ta mémoire ", "supprime de ta memoire ",
)

# ── Skill Forge keywords ──────────────────────────────────────────────────────
KW_SKILL_LEARN = (
    "apprends à ", "apprends-moi à ", "apprends toi à ",
    "ajoute une action ", "ajoute un skill ", "nouveau skill ",
    "crée une action ", "crée un skill ", "enseigne-toi ",
    "tu peux apprendre à ", "je veux que tu saches ",
    "apprends a ", "apprends moi a ",
)
KW_SKILL_LIST = frozenset({
    "liste mes skills", "mes skills", "skills appris",
    "quels skills", "liste les skills", "skills dynamiques",
})
KW_SKILL_DELETE_PREFIX = (
    "supprime le skill ", "efface le skill ", "oublie le skill ",
    "delete skill ",
)


# ── Conversation state (thread-safe) ─────────────────────────────────────────

@dataclass
class ConversationState:
    messages:     list[dict]       = field(default_factory=list)
    max_messages: int               = CONV_MAX_MESSAGES
    _lock:        threading.Lock   = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self._rebuild_system()

    def _rebuild_system(self) -> None:
        """Reconstruit le system prompt avec le contexte mémoire."""
        mem_ctx = load_memory_prompt() if _MEMORY_OK else ""
        system  = mem_ctx + SYSTEM_PROMPT
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0]["content"] = system
        else:
            self.messages.insert(0, {"role": "system", "content": system})

    def add_user(self, content: str) -> None:
        with self._lock:
            self.messages.append({"role": "user", "content": content})
            self._trim()

    def add_assistant(self, content: str) -> None:
        with self._lock:
            self.messages.append({"role": "assistant", "content": content})
            self._trim()

    def refresh_memory(self) -> None:
        """Appelé après update_memory() pour mettre à jour le system prompt."""
        with self._lock:
            self._rebuild_system()

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.messages)

    def _trim(self) -> None:
        if len(self.messages) <= self.max_messages:
            return
        system_msg = self.messages[0]
        tail       = self.messages[-(self.max_messages - 1):]
        self.messages = [system_msg] + tail


_conv = ConversationState()


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _call_ollama(messages: list[dict], timeout: int = LLM_TIMEOUT) -> str:
    resp = requests.post(
        OLLAMA_URL,
        json={"model": MODEL_NAME, "messages": messages, "stream": False},
        timeout=timeout,
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"Ollama a retourné une réponse non-JSON (HTTP {resp.status_code}). "
            f"Début : {resp.text[:200]!r}"
        ) from exc
    return data["message"]["content"]


# Marqueurs d'incertitude → confiance basse
_LOW_CONF_RE = re.compile(
    r"\[incertain\]|je ne sais pas|j'ignore|pas certain|pas sûr|peut-être|"
    r"il se peut|je crois|probablement|aucune idée",
    re.IGNORECASE,
)


def estimer_confiance(reply: str) -> int:
    """Heuristique 0-100 de confiance d'une réponse LLM (marqueurs d'incertitude)."""
    if _LOW_CONF_RE.search(reply):
        return 55
    if "?" in reply and len(reply) < 60:
        return 75
    return 90


def chat_llm(message: str, state: Optional[ConversationState] = None) -> str:
    s = state if state is not None else _conv
    s.add_user(message)
    t0 = time.monotonic()
    try:
        raw   = _call_ollama(s.snapshot())
        reply = _strip_think(raw)
        s.add_assistant(reply)
        dt    = time.monotonic() - t0
        conf  = estimer_confiance(reply)
        # Préfixe visuel si confiance basse — l'utilisateur sait qu'il faut vérifier
        display = reply.replace("[incertain]", "").strip()
        if conf < 70:
            display = f"⚠️ (confiance ~{conf}% — à vérifier) {display}"
        log.info("[LLM] %.1fs, confiance≈%d%%", dt, conf)
        return f"{display}\u200b[[lat:{dt:.1f}s]]"  # latence encodée pour la GUI
    except requests.exceptions.Timeout:
        return "Timeout — réessaie."
    except requests.exceptions.ConnectionError:
        return "Ollama hors ligne — lance `ollama serve`."
    except Exception as exc:
        log.exception("LLM error")
        return f"Erreur LLM : {exc}"


_LAT_RE = re.compile(r"\u200b\[\[lat:([\d.]+)s\]\]$")


def extraire_latence(reply: str) -> tuple[str, Optional[float]]:
    """Sépare le texte affichable du marqueur de latence encodé par chat_llm."""
    m = _LAT_RE.search(reply)
    if m:
        return reply[:m.start()], float(m.group(1))
    return reply, None


def reset_conversation() -> None:
    global _conv
    _conv = ConversationState()


# ── Path helpers ──────────────────────────────────────────────────────────────

def _folder_rules() -> str:
    d    = USER_HOME / "Desktop"
    docs = USER_HOME / "Documents"
    dl   = USER_HOME / "Downloads"
    return (
        f'- "bureau"/"desktop" → {d}\n'
        f'- "documents" → {docs}\n'
        f'- "downloads"/"téléchargements" → {dl}\n'
        f'- "dossier X sur le bureau" → {d / "X"}\n'
        f"- sinon → {d}"
    )


def _parse_with_llm(system: str, user_input: str) -> Optional[dict]:
    try:
        raw     = _call_ollama(
            [{"role": "system", "content": system},
             {"role": "user",   "content": user_input}],
            timeout=PARSE_TIMEOUT,
        )
        cleaned = _strip_think(raw)
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            log.warning("_parse_with_llm: aucun JSON : %r", cleaned[:200])
            return None
        return json.loads(m.group())
    except json.JSONDecodeError as exc:
        log.warning("_parse_with_llm: JSON invalide : %s", exc)
    except Exception as exc:
        log.warning("_parse_with_llm: échec : %s", exc)
    return None


_FOLDER_ALIASES: list[tuple[str, Path]] = [
    (r"\b(?:sur\s+le\s+)?bureau\b",                  USER_HOME / "Desktop"),
    (r"\b(?:sur\s+le\s+)?desktop\b",                 USER_HOME / "Desktop"),
    (r"\b(?:dans\s+(?:les?\s+)?)?mes\s+documents\b", USER_HOME / "Documents"),
    (r"\b(?:dans\s+(?:les?\s+)?)?documents?\b",      USER_HOME / "Documents"),
    (r"\b(?:dans\s+(?:les?\s+)?)?downloads?\b",      USER_HOME / "Downloads"),
    (r"\bt[eé]l[eé]chargements?\b",                  USER_HOME / "Downloads"),
]

_EXT_ALIASES: dict[str, str] = {
    "python": ".py",  "py":   ".py",
    "texte":  ".txt", "txt":  ".txt",  "text": ".txt",
    "word":   ".docx","docx": ".docx",
    "markdown":".md", "md":   ".md",
    "html":   ".html","css":  ".css",
    "json":   ".json","csv":  ".csv",
    "yaml":   ".yaml","toml": ".toml",
    "batch":  ".bat", "shell": ".sh",
}

_CONTENT_PATTERN = re.compile(
    r"""
    \b(?:
        avec\s+(?:(?:le|son)\s+)?(?:contenu\s*:?|texte\s*:?|comme\s+contenu) |
        avec\s+dedans                                                           |
        contenant                                                               |
        contenu\s*:                                                             |
        qui\s+contient                                                          |
        dont\s+le\s+contenu\s+est                                              |
        (?:avec|et)\s+[eé]cris\s+dedans                                        |
        (?:avec|et)\s+[eé]cris\s*:?                                            |
        avec\s+(?!(?:le\s+)?(?:bureau|desktop|documents?|downloads?|t[eé]l[eé]chargements?)\b)
    )\s*:?\s*
    """,
    re.IGNORECASE | re.VERBOSE,
)

_NOISE_WORDS = frozenset({
    "un", "une", "le", "la", "les", "du", "de", "des",
    "dans", "sur", "pour", "nommé", "nomme", "appelé", "appele",
    "fichier", "document", "dossier",
})


def _parse_file_creation_regex(user_input: str) -> Optional[dict]:
    txt   = user_input.strip()
    lower = txt.lower()
    for kw in sorted(KW_CREATE_FILE, key=len, reverse=True):
        if kw in lower:
            idx = lower.find(kw) + len(kw)
            txt   = txt[idx:].lstrip(" ,;:–-")
            lower = txt.lower()
            break

    contenu = ""
    m = _CONTENT_PATTERN.search(txt)
    if m:
        contenu = txt[m.end():].strip()
        txt   = txt[:m.start()].strip()
        lower = txt.lower()

    dossier: Path = USER_HOME / "Desktop"
    for pat, path in _FOLDER_ALIASES:
        mo = re.search(pat, lower)
        if mo:
            dossier = path
            txt   = (txt[:mo.start()] + " " + txt[mo.end():]).strip()
            lower = txt.lower()
            txt   = re.sub(r"\b(?:dans|sur)\s*$", "", txt, flags=re.IGNORECASE).strip()
            lower = txt.lower()
            break

    nom: Optional[str] = None

    mo = re.search(r"""["']([\w\-. éèàùçâêîôûäëïöü]+)["']""", txt)
    if mo:
        nom = mo.group(1).strip()

    if not nom:
        mo = re.search(r"\b([\w\-éèàùç]+\.[a-zA-Z0-9]{1,6})\b", txt)
        if mo:
            nom = mo.group(1)

    if not nom:
        mo = re.search(
            r"\b(?:nomm[eé]|appel[eé]|intitul[eé])\s+([\w\-éèàùç.]+)",
            txt, re.IGNORECASE,
        )
        if mo:
            nom = mo.group(1)

    if not nom:
        words = [w for w in txt.split() if w.lower() not in _NOISE_WORDS and len(w) > 1]
        if words:
            nom = words[0]

    if not nom:
        return None

    if "." not in nom:
        nom += ".txt"

    return {"nom": nom, "dossier": str(dossier), "contenu": contenu}


def _parse_file_creation(user_input: str) -> Optional[dict]:
    result = _parse_file_creation_regex(user_input)
    if result:
        return result
    system = (
        "Extrais les infos de création de fichier depuis la commande utilisateur.\n"
        "Retourne UNIQUEMENT un JSON valide avec les clés : nom, dossier, contenu\n"
        f"Règles dossier :\n{_folder_rules()}\n"
        "Exemple : {\"nom\": \"notes.txt\", \"dossier\": \"C:/Users/user/Desktop\", \"contenu\": \"\"}"
    )
    return _parse_with_llm(system, user_input)


def _parse_file_open(user_input: str) -> Optional[dict]:
    system = (
        "Extrais les infos d'ouverture de fichier depuis la commande utilisateur.\n"
        "Retourne UNIQUEMENT un JSON valide avec les clés : nom, dossier\n"
        f"Règles dossier :\n{_folder_rules()}\n"
        "Exemple : {\"nom\": \"rapport.pdf\", \"dossier\": \"C:/Users/user/Desktop\"}"
    )
    return _parse_with_llm(system, user_input)


def _resolve_path(arg: str) -> str:
    arg   = arg.strip().strip("\"'")
    lower = arg.lower()
    for pat, path in _FOLDER_ALIASES:
        if re.search(pat, lower):
            return str(path)
    return arg


def _extract_after_prefix(txt: str, prefixes: tuple[str, ...]) -> str:
    lower = txt.lower()
    for p in sorted(prefixes, key=len, reverse=True):
        if lower.startswith(p):
            return txt[len(p):].strip()
    return ""


def _prefix_or_bare(lower: str, kws: frozenset) -> bool:
    return any(lower.startswith(k) for k in kws)


def _parse_two_paths(
    txt: str,
    prefixes: tuple[str, ...],
    separators: tuple[str, ...],
) -> Optional[tuple[str, str]]:
    lower = txt.lower()
    rest  = ""
    for p in sorted(prefixes, key=len, reverse=True):
        if lower.startswith(p):
            rest = txt[len(p):]
            break
    if not rest:
        return None
    for sep in separators:
        if sep in rest.lower():
            idx = rest.lower().find(sep)
            src = rest[:idx].strip()
            dst = rest[idx + len(sep):].strip()
            if src and dst:
                return src, dst
    return None


# ── Parsing message (contact + plateforme) ────────────────────────────────────

_PLATEFORME_MOTS: dict[str, str] = {
    "whatsapp": "whatsapp", "wp": "whatsapp", "wapp": "whatsapp",
    "telegram": "telegram", "tg": "telegram",
    "discord":  "discord",
    "signal":   "signal",
}

def _parse_message_cmd(txt: str) -> Optional[tuple[str, str, str]]:
    """
    Parse "envoie [un message] à/a <contact> [sur <plateforme>] [:]  <texte>"
    Retourne (contact, message, plateforme) ou None.
    """
    # Retire le préfixe
    rest = _extract_after_prefix(txt, KW_MSG_PREFIX)
    if not rest:
        return None

    # Détecte la plateforme si mentionnée
    plateforme = "whatsapp"
    for mot, plat in _PLATEFORME_MOTS.items():
        pat = re.compile(
            r"\s+(?:sur|via|par)\s+" + re.escape(mot) + r"\b",
            re.IGNORECASE,
        )
        m = pat.search(rest)
        if m:
            plateforme = plat
            rest = (rest[:m.start()] + rest[m.end():]).strip()
            break

    # Séparateur contact / message
    # Formats : "à Maman : texte" | "à Maman texte" | "à Maman, texte"
    m = re.match(
        r'^([^:,]{1,40}?)(?:\s*[,:]\s*|\s+(?=\S{5,}))(.+)$',
        rest, re.DOTALL,
    )
    if not m:
        return None

    contact = m.group(1).strip().strip('"\'')
    message = m.group(2).strip().strip('"\'')

    if not contact or not message:
        return None

    return contact, message, plateforme


# ── Router ────────────────────────────────────────────────────────────────────

# ── Découpage multi-étapes ────────────────────────────────────────────────────
# "ouvre chrome puis cherche meteo" → ["ouvre chrome", "cherche meteo"]
_STEP_SEP = re.compile(
    r"\s+(?:puis|ensuite|et ensuite|après|apres|et après|et apres|"
    r"et aussi|et|then)\s+",
    re.IGNORECASE,
)

# Connecteurs à NE PAS découper (le "et" fait partie d'un nom/contenu)
_NO_SPLIT_HINTS = ("avec dedans", "écris", "ecris", "contenu", ":")


def _split_steps(txt: str) -> list[str]:
    """Découpe une commande composée en étapes. Liste à 1 élément si pas de séparateur."""
    low = txt.lower()
    # Si la phrase contient du contenu libre (création fichier…), ne pas découper sur "et"
    if any(h in low for h in _NO_SPLIT_HINTS):
        return [txt]
    parts = [p.strip(" ,.;") for p in _STEP_SEP.split(txt) if p.strip(" ,.;")]
    return parts if len(parts) > 1 else [txt]


def router(txt: str) -> Optional[str]:
    """
    Router public. Détecte et exécute les commandes multi-étapes
    ("ouvre X puis cherche Y") en séquence. Retourne None si AUCUNE
    étape n'est une commande (→ le LLM prend le relais).
    """
    if not txt or not txt.strip():
        return None

    steps = _split_steps(txt)
    if len(steps) == 1:
        return _route_single(txt)

    # Multi-étapes : exécute chaque morceau, agrège les résultats
    results: list[str] = []
    any_handled = False
    for i, step in enumerate(steps, 1):
        res = _route_single(step)
        if res is None:
            # Étape non-commande → on l'envoie au LLM pour ne rien perdre
            res = chat_llm(step)
        else:
            any_handled = True
        results.append(f"{i}. {step} → {res}")

    if not any_handled:
        return None  # aucune vraie commande → laisse le LLM gérer la phrase entière
    return "Étapes exécutées :\n" + "\n".join(results)


def _route_single(txt: str) -> Optional[str]:
    if not txt or not txt.strip():
        return None

    lower = txt.lower().strip()

    # ── VISION ──
    if any(lower.startswith(k) for k in KW_VISION) or lower in KW_VISION:
        if not VISION_OK:
            return "Module vision non chargé."
        question = ""
        for kw in sorted(KW_VISION, key=len, reverse=True):
            if lower.startswith(kw):
                question = txt[len(kw):].strip(" ,;:?")
                break
        # Drag&drop : "aide moi, analyse ce fichier : "chemin"" → OCR fichier
        m = re.search(r'analyse ce fichier\s*:?\s*["\']?([^"\']+)["\']?', question, re.IGNORECASE)
        if m:
            chemin = m.group(1).strip().strip("\"'")
            return aide_moi("", fichier=chemin)
        return aide_moi(question)

    # ── CLAUDE BROWSER ──
    if any(lower.startswith(k) for k in KW_CLAUDE) or lower in KW_CLAUDE:
        if not CLAUDE_OK:
            return "Module claude_browser non chargé."
        prompt = ""
        for kw in sorted(KW_CLAUDE, key=len, reverse=True):
            if lower.startswith(kw):
                prompt = txt[len(kw):].strip(" ,;:?")
                break
        if not prompt:
            return "Que veux-tu que je demande à Claude ?"
        try:
            from claude_browser import demander_a_claude, wait_for_window
            if wait_for_window(timeout=1.0) is None:
                log.info("[Claude] Fenêtre absente → ouvrir_app('claude')")
                executer_action({"type": "ouvrir_app", "params": {"nom": "claude"}})
                time.sleep(3.0)
            demander_a_claude(prompt)
            return "Message envoyé à Claude ✓"
        except Exception as exc:
            log.exception("Claude routing")
            return f"Erreur Claude : {exc}"

    # ── SCREENSHOT ──
    if lower in KW_SCREENSHOT or any(lower.startswith(k) for k in KW_SCREENSHOT):
        return executer_action({"type": "screenshot", "params": {}})

    # ── VOLUME ──
    if lower in KW_VOL_UP   or any(k in lower for k in KW_VOL_UP):   return executer_action({"type": "volume_up",   "params": {}})
    if lower in KW_VOL_DOWN or any(k in lower for k in KW_VOL_DOWN): return executer_action({"type": "volume_down", "params": {}})
    if lower in KW_VOL_MUTE or any(k in lower for k in KW_VOL_MUTE): return executer_action({"type": "volume_mute", "params": {}})

    # ── POWER ──
    if lower in KW_SHUTDOWN or any(k in lower for k in KW_SHUTDOWN): return executer_action({"type": "shutdown", "params": {}})
    if lower in KW_RESTART  or any(k in lower for k in KW_RESTART):  return executer_action({"type": "restart",  "params": {}})
    if lower in KW_SLEEP    or any(k in lower for k in KW_SLEEP):    return executer_action({"type": "sleep",    "params": {}})
    if lower in KW_LOCK     or any(k in lower for k in KW_LOCK):     return executer_action({"type": "lock",     "params": {}})

    # ── CHERCHER FICHIER (avant recherche web) ──
    arg = _extract_after_prefix(txt, tuple(KW_SEARCH_FILE))
    if arg:
        return executer_action({"type": "chercher_fichier", "params": {"motif": arg}})
    if _prefix_or_bare(lower, KW_SEARCH_FILE):
        return "Chercher quel fichier ?"

    # ── RECHERCHE WEB ──
    query = _extract_after_prefix(txt, KW_WEB_SEARCH_PREFIX)
    if query:
        return _recherche_web(query)
    if any(lower.startswith(p) for p in KW_WEB_SEARCH_PREFIX):
        return "Qu'est-ce que je dois chercher ?"

    # ── URL / SCRAPE ──
    url = _extract_after_prefix(txt, KW_OPEN_URL_PREFIX)
    if url:
        return executer_action({"type": "ouvrir_url", "params": {"url": url}})
    if any(lower.startswith(p) for p in KW_OPEN_URL_PREFIX):
        return "Quelle URL ?"

    url = _extract_after_prefix(txt, KW_SCRAPE_PREFIX)
    if url:
        return executer_action({"type": "scraper_page", "params": {"url": url}})
    if any(lower.startswith(p) for p in KW_SCRAPE_PREFIX):
        return "Quelle page ?"

    # ── MÉDIAS / LUMINOSITÉ (AVANT YouTube : "mets pause" matcherait "mets ") ──
    if any(k in lower for k in KW_MEDIA_PLAY):  return executer_action({"type": "media_play_pause", "params": {}})
    if any(k in lower for k in KW_MEDIA_NEXT):  return executer_action({"type": "media_next",       "params": {}})
    if any(k in lower for k in KW_MEDIA_PREV):  return executer_action({"type": "media_prev",       "params": {}})
    if any(k in lower for k in KW_MEDIA_STOP):  return executer_action({"type": "media_stop",       "params": {}})
    if any(k in lower for k in KW_LUMIN_UP):    return executer_action({"type": "luminosite_up",    "params": {}})
    if any(k in lower for k in KW_LUMIN_DOWN):  return executer_action({"type": "luminosite_down",  "params": {}})

    # ── YOUTUBE RÉSUMÉ (avant youtube_jouer pour éviter faux match) ──
    url_video = _extract_after_prefix(txt, KW_YOUTUBE_RESUME_PREFIX)
    if url_video:
        return executer_action({"type": "youtube_resumer", "params": {"url": url_video}})
    if any(lower.startswith(p) for p in KW_YOUTUBE_RESUME_PREFIX):
        return "URL de quelle vidéo YouTube ?"

    # ── YOUTUBE JOUER ──
    yt_query = _extract_after_prefix(txt, KW_YOUTUBE_PREFIX)
    if yt_query:
        return executer_action({"type": "youtube_jouer", "params": {"query": yt_query}})
    if any(lower.startswith(p) for p in KW_YOUTUBE_PREFIX):
        return "Quoi jouer sur YouTube ?"

    # ── MÉTÉO ──
    meteo_arg = _extract_after_prefix(txt, KW_METEO_PREFIX)
    if meteo_arg:
        # Détecte "demain" / "aujourd'hui" / "ce week-end" en fin de chaîne
        quand = "aujourd'hui"
        for mot, val in [("demain", "demain"), ("ce soir", "ce soir"),
                          ("ce week-end", "ce week-end"), ("weekend", "ce week-end")]:
            if mot in meteo_arg.lower():
                quand      = val
                meteo_arg  = re.sub(re.escape(mot), "", meteo_arg, flags=re.IGNORECASE).strip(" ,")
                break
        ville = meteo_arg.strip()
        return executer_action({"type": "meteo", "params": {"ville": ville, "quand": quand}})
    if any(lower.startswith(p) for p in KW_METEO_PREFIX):
        return "Météo de quelle ville ?"

    # ── ENVOYER MESSAGE ──
    if any(lower.startswith(p) for p in KW_MSG_PREFIX):
        parsed = _parse_message_cmd(lower)
        if parsed:
            contact, message, plateforme = parsed
            # Récupère la casse originale pour le message
            _, message_orig, _ = _parse_message_cmd(txt) or (None, message, None)
            return executer_action({
                "type": "envoyer_message",
                "params": {
                    "contact":    contact,
                    "message":    message_orig or message,
                    "plateforme": plateforme,
                },
            })
        return "Format : envoie à <contact> : <message>"

    # ── MÉMOIRE ──
    if _MEMORY_OK:
        mem_info = _extract_after_prefix(txt, KW_MEMORY_PREFIX)
        if mem_info:
            update_memory({"notes": {f"note_{int(time.time())}": mem_info}})
            _conv.refresh_memory()
            return f"Mémorisé ✓"
        oublie_info = _extract_after_prefix(txt, KW_MEMORY_FORGET_PREFIX)
        if oublie_info:
            from memory_manager import forget
            res = forget(oublie_info.strip(), "notes")
            _conv.refresh_memory()
            return res

    # ── PROCESSUS ──
    if lower in KW_LIST_APPS or any(k in lower for k in KW_LIST_APPS):
        return executer_action({"type": "lister_apps", "params": {}})
    if lower in KW_TOP_CPU or any(k in lower for k in KW_TOP_CPU):
        return executer_action({"type": "top_cpu", "params": {}})
    if lower in KW_TOP_RAM or any(k in lower for k in KW_TOP_RAM):
        return executer_action({"type": "top_ram", "params": {}})
    nom = _extract_after_prefix(txt, KW_KILL)
    if nom:
        return executer_action({"type": "kill_process", "params": {"nom": nom}})
    if _prefix_or_bare(lower, KW_KILL):
        return "Quel process tuer ?"
    nom = _extract_after_prefix(txt, KW_CLOSE_APP)
    if nom:
        return executer_action({"type": "fermer_app", "params": {"nom": nom}})
    if _prefix_or_bare(lower, KW_CLOSE_APP):
        return "Quelle app fermer ?"

    # ── RAPPELS ──
    if _REMINDERS_OK:
        if lower in KW_LIST_REMINDERS or any(k in lower for k in KW_LIST_REMINDERS):
            return str(lister_rappels())
        rid = _extract_after_prefix(txt, KW_CANCEL_REMINDER_PREFIX)
        if rid:
            return str(annuler_rappel(rid.strip()))
        if any(lower.startswith(p) for p in KW_REMINDER_PREFIX):
            from reminders import parse_reminder_intent
            msg, fire_at = parse_reminder_intent(txt)
            if fire_at is not None:
                return str(creer_rappel({"message": msg, "fire_at_iso": fire_at.isoformat()}))

    # ── DEBUG & ENREGISTREMENT ──
    if lower in KW_LOGS or any(k in lower for k in KW_LOGS):
        return executer_action({"type": "afficher_logs", "params": {}})
    if lower in KW_REC_STOP or any(k in lower for k in KW_REC_STOP):
        return executer_action({"type": "record_stop", "params": {}})
    if lower in KW_REC_START or any(k in lower for k in KW_REC_START):
        return executer_action({"type": "record_start", "params": {}})

    # ── DOSSIERS ──
    arg = _extract_after_prefix(txt, tuple(KW_OPEN_FOLDER))
    if arg:
        return executer_action({"type": "ouvrir_dossier", "params": {"chemin": _resolve_path(arg)}})
    if _prefix_or_bare(lower, KW_OPEN_FOLDER):
        return "Quel dossier ?"

    if any(k in lower for k in KW_LIST_DIR):
        arg = ""
        for k in sorted(KW_LIST_DIR, key=len, reverse=True):
            if k in lower:
                arg = txt[lower.find(k) + len(k):].strip(" :?.")
                break
        return executer_action({"type": "lister_dossier", "params": {"chemin": _resolve_path(arg) if arg else ""}})

    # ── RENOMMER / COPIER / DÉPLACER ──
    pair = _parse_two_paths(txt, KW_RENAME, ("en", "→", "vers"))
    if pair:
        src, nouveau = pair
        return executer_action({"type": "renommer_fichier",
                                "params": {"chemin": _resolve_path(src),
                                           "nouveau": re.sub(r"\s+", "_", nouveau.strip().strip("\"'"))}})
    if _prefix_or_bare(lower, KW_RENAME):
        return "Renommer quoi en quoi ? (ex: renomme a.txt en b.txt)"

    pair = _parse_two_paths(txt, KW_COPY, ("vers", "dans", "→", "en"))
    if pair:
        src, dst = pair
        return executer_action({"type": "copier_fichier",
                                "params": {"source": _resolve_path(src), "destination": _resolve_path(dst)}})
    if _prefix_or_bare(lower, KW_COPY):
        return "Copier quoi vers où ?"

    pair = _parse_two_paths(txt, KW_MOVE, ("vers", "dans", "→"))
    if pair:
        src, dst = pair
        return executer_action({"type": "deplacer_fichier",
                                "params": {"source": _resolve_path(src), "destination": _resolve_path(dst)}})
    if _prefix_or_bare(lower, KW_MOVE):
        return "Déplacer quoi vers où ?"

    # ── ZIP / UNZIP ──
    arg = _extract_after_prefix(txt, tuple(KW_UNZIP))
    if arg:
        return executer_action({"type": "dezipper", "params": {"chemin": _resolve_path(arg)}})
    if _prefix_or_bare(lower, KW_UNZIP):
        return "Quel .zip dézipper ?"
    arg = _extract_after_prefix(txt, tuple(KW_ZIP))
    if arg:
        return executer_action({"type": "zipper", "params": {"chemin": _resolve_path(arg)}})
    if _prefix_or_bare(lower, KW_ZIP):
        return "Zipper quoi ?"

    # ── TAILLE ──
    arg = _extract_after_prefix(txt, tuple(KW_SIZE))
    if arg:
        return executer_action({"type": "taille_fichier", "params": {"chemin": _resolve_path(arg)}})
    if _prefix_or_bare(lower, KW_SIZE):
        return "Taille de quoi ?"

    # ── SUPPRIMER ──
    arg = _extract_after_prefix(txt, tuple(KW_DELETE_FILE))
    if arg:
        return executer_action({"type": "supprimer_fichier", "params": {"chemin": _resolve_path(arg)}})
    if _prefix_or_bare(lower, KW_DELETE_FILE):
        return "Supprimer quoi ?"

    # ── CRÉER / OUVRIR FICHIER ──
    if any(k in lower for k in KW_CREATE_FILE):
        if not ACTIONS_OK:
            return "Module actions non chargé."
        infos = _parse_file_creation(txt)
        if not infos:
            return "Je n'ai pas compris, reformule ?"
        chemin  = str(Path(infos.get("dossier", str(USER_HOME / "Desktop"))) / infos.get("nom", "nouveau.txt"))
        contenu = infos.get("contenu", "")
        return executer_action({"type": "creer_fichier", "params": {"chemin": chemin, "contenu": contenu}})

    if any(k in lower for k in KW_OPEN_FILE):
        if not ACTIONS_OK:
            return "Module actions non chargé."
        infos = _parse_file_open(txt)
        if not infos:
            return "Je n'ai pas compris, reformule ?"
        chemin = str(Path(infos.get("dossier", str(USER_HOME / "Desktop"))) / infos.get("nom", ""))
        return executer_action({"type": "ouvrir_fichier", "params": {"chemin": chemin}})

    # ── OUVRIR APP (en dernier pour éviter les faux positifs) ──
    for prefix in KW_APP_PREFIX:
        if lower.startswith(prefix) and "fichier" not in lower and "dossier" not in lower:
            nom = txt[len(prefix):].strip()
            if not nom:
                return "Quelle application ?"
            return executer_action({"type": "ouvrir_app", "params": {"nom": nom}})

    # ── INFOS SYSTÈME ──
    if any(k in lower for k in KW_INFOS):       return executer_action({"type": "infos_systeme",    "params": {}})

    # ── SKILL FORGE ──────────────────────────────────────────────────────────
    if _FORGE_OK:
        forge = get_forge(model=MODEL_NAME)

        # Confirmation d'un plan en attente
        if forge.has_pending and lower in {"ok", "oui", "go", "yes", "valide", "confirme",
                                            "c'est bon", "fais-le", "fais le", "yep"}:
            return forge.forge()

        # Annulation d'un plan en attente
        if forge.has_pending and lower in {"non", "annule", "cancel", "stop", "nope", "no"}:
            return forge.cancel()

        # Lister skills
        if lower in KW_SKILL_LIST or any(k in lower for k in KW_SKILL_LIST):
            return forge.list_skills()

        # Supprimer skill
        sid_del = _extract_after_prefix(txt, KW_SKILL_DELETE_PREFIX)
        if sid_del:
            return forge.delete_skill(sid_del.strip())

        # Apprendre un skill → phase 1 réflexion
        learn_arg = _extract_after_prefix(txt, KW_SKILL_LEARN)
        if learn_arg:
            return forge.reflect(txt)  # passe la phrase complète

        # Routing vers skills dynamiques existants
        dyn = route_dynamic(txt)
        if dyn is not None:
            return dyn

    return None


# ── Web search helper (appelé depuis router) ─────────────────────────────────

def _recherche_web(query: str) -> str:
    """DDG + résumé Ollama. Fallback si actions non chargé."""
    if ACTIONS_OK:
        return executer_action({"type": "recherche_web", "params": {"query": query}})
    # Fallback minimal si actions KO
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=5))
        lignes = [f"🔍 {query}\n"]
        for i, r in enumerate(raw, 1):
            lignes.append(f"  {i}. {r.get('title', '')}")
            if r.get("body"):
                lignes.append(f"     {r['body'][:120]}")
            lignes.append(f"     {r.get('href', '')}\n")
        return "\n".join(lignes)
    except ImportError:
        return "pip install ddgs"
    except Exception as exc:
        return f"Recherche échouée : {exc}"

"""
reminders.py — ONYX Rappels v1
Rappels programmés : parse durée/heure via LLM → job en mémoire → notif + TTS.

Dépendances :
    pip install schedule pyttsx3 plyer
    # win10toast optionnel (fallback si plyer indisponible)

Persistance : JSON dans LOG_DIR/reminders.json
Survie aux redémarrages : rechargé au démarrage via load_from_disk().

Intégration :
    # Dans actions.py, importer et enregistrer dans _ACTIONS :
    from reminders import creer_rappel, lister_rappels, annuler_rappel, ResultatAction
    _ACTIONS["rappel"]         = lambda p: creer_rappel(p)
    _ACTIONS["lister_rappels"] = lambda _: lister_rappels()
    _ACTIONS["annuler_rappel"] = lambda p: annuler_rappel(p.get("id", ""))

    # Dans main.py / démarrage :
    from reminders import start_scheduler, load_from_disk
    load_from_disk()
    start_scheduler()
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import schedule

from config import LOG_DIR

log = logging.getLogger(__name__)

# ── Fichier de persistance ────────────────────────────────────────────────────
_PERSIST_FILE = LOG_DIR / "reminders.json"

# ── TTS ───────────────────────────────────────────────────────────────────────
_tts_engine = None
_tts_lock   = threading.Lock()


def _get_tts():
    """Lazy-init pyttsx3. Thread-safe."""
    global _tts_engine
    with _tts_lock:
        if _tts_engine is None:
            try:
                import pyttsx3
                _tts_engine = pyttsx3.init()
                _tts_engine.setProperty("rate", 160)
                _tts_engine.setProperty("volume", 0.9)
                log.info("[Reminders] TTS pyttsx3 initialisé ✓")
            except Exception as exc:
                log.warning("[Reminders] TTS indisponible : %s", exc)
        return _tts_engine


def _speak(text: str) -> None:
    """Lit le texte à voix haute. Silencieux si pyttsx3 absent. Thread-safe."""
    engine = _get_tts()
    if engine is None:
        return
    with _tts_lock:  # pyttsx3 non thread-safe : 2 rappels simultanés plantaient
        try:
            engine.say(text)
            engine.runAndWait()
        except Exception as exc:
            log.warning("[Reminders] TTS erreur : %s", exc)


# ── Notifications ─────────────────────────────────────────────────────────────

def _notify(title: str, message: str) -> None:
    """Notification Windows. Essaie plyer → win10toast → log seul."""
    # Tentative plyer (cross-platform)
    try:
        from plyer import notification
        notification.notify(
            title=title,
            message=message,
            app_name="ONYX",
            timeout=10,
        )
        return
    except Exception:
        pass

    # Fallback win10toast (Windows uniquement)
    try:
        from win10toast import ToastNotifier
        ToastNotifier().show_toast(
            title,
            message,
            duration=10,
            threaded=True,
        )
        return
    except Exception:
        pass

    # Dernier recours : log uniquement
    log.info("[Reminders] 🔔 %s — %s", title, message)


# ── Modèle ────────────────────────────────────────────────────────────────────

@dataclass
class Reminder:
    id:          str
    message:     str
    fire_at:     datetime          # heure absolue du déclenchement
    created_at:  datetime = field(default_factory=datetime.now)
    fired:       bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fire_at"]    = self.fire_at.isoformat()
        d["created_at"] = self.created_at.isoformat()
        return d

    @staticmethod
    def from_dict(d: dict) -> "Reminder":
        return Reminder(
            id         = d["id"],
            message    = d["message"],
            fire_at    = datetime.fromisoformat(d["fire_at"]),
            created_at = datetime.fromisoformat(d["created_at"]),
            fired      = d.get("fired", False),
        )


# ── Store en mémoire ──────────────────────────────────────────────────────────
_reminders: dict[str, Reminder] = {}
_timers:    dict[str, threading.Timer] = {}  # id → Timer (pour annulation réelle)
_store_lock = threading.Lock()


# ── Parsing de la durée ───────────────────────────────────────────────────────
# Patterns FR : "dans 30 minutes", "dans 2 heures", "dans 1 heure 30"
# + heure absolue : "à 15h30", "à 9h", "demain à 8h"

_RE_DELTA = re.compile(
    r"(?:dans\s+)?"
    r"(?:(?P<heures>\d+)\s*h(?:eure[s]?)?)?"
    r"\s*(?:(?P<minutes>\d+)\s*(?:min(?:ute[s]?)?)?)?",
    re.IGNORECASE,
)

_RE_ABS = re.compile(
    r"(?P<demain>demain\s+)?à\s+(?P<h>\d{1,2})h(?:(?P<m>\d{2}))?",
    re.IGNORECASE,
)


def _parse_fire_at(texte_temps: str) -> Optional[datetime]:
    """
    Parse une expression temporelle française → datetime absolu.
    Exemples :
        "dans 30 minutes"  → now + 30min
        "dans 2h30"        → now + 2h30
        "à 15h30"          → aujourd'hui 15:30 (ou demain si passé)
        "demain à 8h"      → demain 08:00
    Retourne None si parsing impossible.
    """
    texte = texte_temps.strip().lower()
    now   = datetime.now()

    # Heure absolue
    m_abs = _RE_ABS.search(texte)
    if m_abs:
        h      = int(m_abs.group("h"))
        mi     = int(m_abs.group("m") or 0)
        demain = bool(m_abs.group("demain"))
        target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if demain or target <= now:
            target += timedelta(days=1)
        return target

    # Durée relative
    m_delta = _RE_DELTA.search(texte)
    if m_delta:
        heures  = int(m_delta.group("heures")  or 0)
        minutes = int(m_delta.group("minutes") or 0)
        if heures == 0 and minutes == 0:
            return None
        return now + timedelta(hours=heures, minutes=minutes)

    return None


def parse_reminder_intent(user_text: str) -> tuple[Optional[str], Optional[datetime]]:
    """
    Extrait (message_rappel, fire_at) depuis une phrase naturelle.
    Exemples :
        "rappelle-moi dans 30 minutes d'appeler Marc"
        "dans 2 heures rappelle-moi la réunion"
        "rappel à 15h30 : envoyer le rapport"

    Retourne (None, None) si parsing échoue.
    """
    text = user_text.strip()

    # Normalise : retire les mots introductifs courants
    text_clean = re.sub(
        r"(?i)^(rappelle[- ]moi|rappel|souviens[- ]moi|n'oublie pas)\s*[:]?\s*",
        "",
        text,
    ).strip()

    # Cherche l'expression temporelle
    # Pattern : "dans X / à Xh" peut être en début, milieu ou fin
    time_match = re.search(
        r"((?:demain\s+)?à\s+\d{1,2}h\d*"
        r"|dans\s+\d+\s*h(?:eure[s]?)?\s*\d*\s*(?:min(?:ute[s]?)?)?"
        r"|dans\s+\d+\s*(?:heure[s]?|h|min(?:ute[s]?)?)"
        r"|dans\s+\d+h\d+)",
        text_clean,
        re.IGNORECASE,
    )

    if not time_match:
        return None, None

    time_str  = time_match.group(0).strip()
    fire_at   = _parse_fire_at(time_str)

    if fire_at is None:
        return None, None

    # Le message = tout ce qui reste après l'expression temporelle
    msg = text_clean[time_match.end():].strip()
    msg = re.sub(r"^(?:de|d[''e]|pour|:)\s*", "", msg).strip()

    # Si message vide → l'expression était peut-être en fin de phrase
    if not msg:
        msg = text_clean[: time_match.start()].strip()
        msg = re.sub(r"(?i)^(rappel[:]?\s*|de\s+|d[''e]\s+)", "", msg).strip()

    if not msg:
        msg = "Rappel ONYX"

    return msg, fire_at


# ── Déclenchement ─────────────────────────────────────────────────────────────

def _fire_reminder(reminder_id: str) -> None:
    """Callback schedule : notif + TTS + marque comme fired."""
    with _store_lock:
        _timers.pop(reminder_id, None)
        r = _reminders.get(reminder_id)
        if r is None or r.fired:
            return
        r.fired = True

    log.info("[Reminders] 🔔 Déclenchement : %s", r.message)
    _notify("⏰ ONYX — Rappel", r.message)
    _speak(f"Rappel : {r.message}")
    _save_to_disk()
    # NB : on n'utilise PAS schedule.jobs ici (on utilise threading.Timer one-shot).
    # L'ancien code « schedule.cancel_job(schedule.jobs[0]) » annulait un job aléatoire — bug.


# ── Planification ─────────────────────────────────────────────────────────────

def _schedule_reminder(r: Reminder) -> None:
    """Crée un job schedule one-shot pour ce rappel."""
    now     = datetime.now()
    delay_s = max(0.0, (r.fire_at - now).total_seconds())

    # schedule n'a pas de "dans X secondes one-shot" propre →
    # on utilise un thread Timer pour la précision
    t = threading.Timer(delay_s, _fire_reminder, args=(r.id,))
    t.daemon = True
    with _store_lock:
        old = _timers.pop(r.id, None)
        _timers[r.id] = t
    if old is not None:
        old.cancel()
    t.start()
    log.info(
        "[Reminders] Rappel planifié : id=%s msg=%r dans %.0fs (à %s)",
        r.id, r.message, delay_s,
        r.fire_at.strftime("%H:%M"),
    )


# ── Persistance ───────────────────────────────────────────────────────────────

def _save_to_disk() -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        data = [r.to_dict() for r in _reminders.values()]
        _PERSIST_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("[Reminders] Sauvegarde échouée : %s", exc)


def load_from_disk() -> int:
    """
    Charge les rappels depuis le JSON. Replanifie ceux non encore fired.
    Appelé au démarrage. Retourne le nombre de rappels rechargés.
    """
    if not _PERSIST_FILE.exists():
        return 0
    try:
        data = json.loads(_PERSIST_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("[Reminders] Chargement échoué : %s", exc)
        return 0

    recharged = 0
    now = datetime.now()
    with _store_lock:
        for d in data:
            try:
                r = Reminder.from_dict(d)
            except (KeyError, ValueError) as exc:
                log.warning("[Reminders] Entrée invalide ignorée : %s", exc)
                continue
            _reminders[r.id] = r
            if not r.fired and r.fire_at > now:
                _schedule_reminder(r)
                recharged += 1
            elif not r.fired and r.fire_at <= now:
                # Rappel manqué pendant downtime → déclenche immédiatement
                log.info("[Reminders] Rappel manqué, déclenché maintenant : %s", r.message)
                threading.Timer(1.0, _fire_reminder, args=(r.id,)).start()
                recharged += 1

    log.info("[Reminders] %d rappel(s) rechargé(s)", recharged)
    return recharged


# ── API publique ──────────────────────────────────────────────────────────────

@dataclass
class ResultatRappel:
    succes:  bool
    message: str

    def __str__(self) -> str:
        return self.message


def creer_rappel(params: dict) -> ResultatRappel:
    """
    Crée un rappel depuis un dict de params.

    Params attendus (l'un ou l'autre) :
        { "texte": "rappelle-moi dans 30 minutes d'appeler Marc" }
        { "message": "appeler Marc", "duree_minutes": 30 }
        { "message": "réunion", "fire_at_iso": "2026-06-01T15:30:00" }

    Retourne ResultatRappel (succes=True + message de confirmation).
    """
    # Cas 1 : phrase naturelle complète
    if "texte" in params:
        msg, fire_at = parse_reminder_intent(params["texte"])
        if fire_at is None:
            return ResultatRappel(
                False,
                "Je n'ai pas compris la durée. Dis-moi : "
                "« rappelle-moi dans 30 minutes de … »",
            )

    # Cas 2 : params structurés depuis le LLM
    elif "message" in params:
        msg = params["message"].strip() or "Rappel"

        if "fire_at_iso" in params:
            try:
                fire_at = datetime.fromisoformat(params["fire_at_iso"])
            except ValueError:
                return ResultatRappel(False, "Format fire_at_iso invalide (ISO 8601 attendu).")

        elif "duree_minutes" in params:
            try:
                minutes = int(params["duree_minutes"])
                fire_at = datetime.now() + timedelta(minutes=minutes)
            except (ValueError, TypeError):
                return ResultatRappel(False, "duree_minutes doit être un entier.")

        elif "duree_secondes" in params:
            try:
                secs    = int(params["duree_secondes"])
                fire_at = datetime.now() + timedelta(seconds=secs)
            except (ValueError, TypeError):
                return ResultatRappel(False, "duree_secondes doit être un entier.")

        else:
            return ResultatRappel(
                False,
                "Précise quand : duree_minutes, fire_at_iso, ou texte libre.",
            )

    else:
        return ResultatRappel(
            False,
            "Params manquants. Utilise { texte: '…' } ou { message: '…', duree_minutes: N }.",
        )

    # Sanity check : pas dans le passé
    if fire_at <= datetime.now():
        return ResultatRappel(False, "La date/heure est dans le passé.")

    r = Reminder(
        id      = str(uuid.uuid4())[:8],
        message = msg,
        fire_at = fire_at,
    )

    with _store_lock:
        _reminders[r.id] = r

    _schedule_reminder(r)
    _save_to_disk()

    delta = fire_at - datetime.now()
    total_min = int(delta.total_seconds() / 60)
    heure_str = fire_at.strftime("%H:%M")

    if total_min < 60:
        delai_str = f"dans {total_min} min"
    else:
        h, m = divmod(total_min, 60)
        delai_str = f"dans {h}h{m:02d}" if m else f"dans {h}h"

    return ResultatRappel(
        True,
        f"⏰ Rappel programmé {delai_str} (à {heure_str}) : « {msg} »",
    )


def lister_rappels() -> ResultatRappel:
    """Retourne la liste des rappels actifs (non fired)."""
    with _store_lock:
        actifs = [r for r in _reminders.values() if not r.fired]

    if not actifs:
        return ResultatRappel(True, "Aucun rappel actif.")

    actifs.sort(key=lambda r: r.fire_at)
    lignes = ["⏰ Rappels actifs :"]
    for r in actifs:
        delta  = r.fire_at - datetime.now()
        mins   = max(0, int(delta.total_seconds() / 60))
        heure  = r.fire_at.strftime("%H:%M")
        lignes.append(f"  [{r.id}] {heure} (+{mins}min) — {r.message}")

    return ResultatRappel(True, "\n".join(lignes))


def annuler_rappel(rappel_id: str) -> ResultatRappel:
    """Annule un rappel par son ID court (8 chars)."""
    rappel_id = rappel_id.strip()
    with _store_lock:
        r = _reminders.pop(rappel_id, None)
        t = _timers.pop(rappel_id, None)
    if t is not None:
        t.cancel()

    if r is None:
        return ResultatRappel(False, f"Rappel « {rappel_id} » introuvable.")

    _save_to_disk()
    return ResultatRappel(True, f"✅ Rappel annulé : « {r.message} »")


def annuler_tous() -> ResultatRappel:
    """Annule tous les rappels actifs."""
    with _store_lock:
        nb = sum(1 for r in _reminders.values() if not r.fired)
        for r in _reminders.values():
            r.fired = True
        timers = list(_timers.values())
        _timers.clear()
    for t in timers:
        t.cancel()

    _save_to_disk()
    return ResultatRappel(True, f"✅ {nb} rappel(s) annulé(s).")


# ── Scheduler background (optionnel — surtout pour jobs récurrents futurs) ────

_scheduler_thread: Optional[threading.Thread] = None
_scheduler_running = False


def start_scheduler() -> None:
    """
    Démarre le thread schedule (1 tick/s).
    Les rappels one-shot utilisent threading.Timer directement,
    mais ce thread permet d'ajouter des jobs récurrents plus tard.
    """
    global _scheduler_thread, _scheduler_running
    if _scheduler_running:
        return

    _scheduler_running = True

    def _run() -> None:
        while _scheduler_running:
            schedule.run_pending()
            time.sleep(1.0)

    _scheduler_thread = threading.Thread(target=_run, name="onyx-scheduler", daemon=True)
    _scheduler_thread.start()
    log.info("[Reminders] Scheduler démarré ✓")


def stop_scheduler() -> None:
    global _scheduler_running
    _scheduler_running = False


# ── CLI de test ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    start_scheduler()

    # Test parsing
    tests = [
        "rappelle-moi dans 2 minutes d'appeler Marc",
        "dans 1h30 rappel réunion standup",
        "rappelle-moi à 15h30 envoyer le rapport",
    ]
    for t in tests:
        msg, fire_at = parse_reminder_intent(t)
        print(f"  '{t}'\n  → msg={msg!r}  fire_at={fire_at}\n")

    # Crée un vrai rappel dans 5 secondes
    r = creer_rappel({"message": "Test ONYX reminders", "duree_secondes": 5})
    print(r)
    print(lister_rappels())

    print("Attente 8s…")
    time.sleep(8)
    print("Terminé.")

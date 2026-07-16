"""
scheduler.py — ONYX Scheduler v1
Inspiré de OpenJarvis (TaskScheduler/SchedulerStore) mais réécrit 100% pour
le stack ONYX : pas de croniter, pas de EventBus — thread natif + SQLite
dans AppData, intégration directe avec main.py (callback `executor`).

Différence vs reminders.py existant :
- reminders.py = rappels ponctuels ("dans 10 min", one-shot)
- scheduler.py = tâches RÉCURRENTES ("tous les jours à 8h", "toutes les heures")
  + tâches "once" programmées à une date précise (vs délai relatif)

Usage depuis main.py / actions.py :
    from scheduler import scheduler
    scheduler.start(executor=lambda prompt: route_message(prompt))

    # Créer une tâche récurrente quotidienne à 8h
    scheduler.create_task("résume mes mails", "daily", "08:00")

    # Toutes les N secondes
    scheduler.create_task("vérifie le cpu", "interval", "3600")

    # Une fois à une date précise
    scheduler.create_task("rappelle-moi le rdv", "once", "2026-06-20T09:00:00")

schedule_type accepté : "daily" (HH:MM), "weekly" (jour:HH:MM, ex "mon:08:00"),
                        "interval" (secondes), "once" (ISO datetime)
Pas de cron complet (pas de croniter dispo offline) — couvre 95% des besoins
persos sans dépendance externe.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# ── Stockage : AppData/Roaming/ONYX/scheduler.db ─────────────────────────────
if os.name == "nt":
    _APPDATA = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    _DB_DIR  = _APPDATA / "ONYX"
else:
    _DB_DIR  = Path.home() / ".local" / "share" / "onyx"

_DB_PATH = _DB_DIR / "scheduler.db"

_DAYS_FR = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
    "vendredi": 4, "samedi": 5, "dimanche": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


# ── Modèle de tâche ────────────────────────────────────────────────────────

@dataclass(slots=True)
class ScheduledTask:
    id: str
    prompt: str
    schedule_type: str        # "daily" | "weekly" | "interval" | "once"
    schedule_value: str       # "08:00" | "mon:08:00" | "3600" | ISO datetime
    status: str = "active"    # "active" | "paused" | "cancelled" | "completed"
    next_run: Optional[str] = None
    last_run: Optional[str] = None
    created_at: str = ""

    def to_row(self) -> tuple:
        return (
            self.id, self.prompt, self.schedule_type, self.schedule_value,
            self.status, self.next_run, self.last_run, self.created_at,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ScheduledTask":
        return cls(
            id=row["id"], prompt=row["prompt"],
            schedule_type=row["schedule_type"], schedule_value=row["schedule_value"],
            status=row["status"], next_run=row["next_run"], last_run=row["last_run"],
            created_at=row["created_at"],
        )

    def describe(self) -> str:
        """Description lisible pour 'mes tâches planifiées'."""
        when = {
            "daily":    f"tous les jours à {self.schedule_value}",
            "weekly":   f"chaque semaine ({self.schedule_value})",
            "interval": f"toutes les {self.schedule_value}s",
            "once":     f"le {self.schedule_value}",
        }.get(self.schedule_type, self.schedule_value)
        return f"[{self.status}] {self.prompt} — {when}"


# ── Store SQLite ──────────────────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id              TEXT PRIMARY KEY,
    prompt          TEXT    NOT NULL,
    schedule_type   TEXT    NOT NULL,
    schedule_value  TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'active',
    next_run        TEXT,
    last_run        TEXT,
    created_at      TEXT    NOT NULL
);
"""


class _Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()
        self._lock = threading.Lock()

    def save(self, task: ScheduledTask) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO scheduled_tasks "
                "(id, prompt, schedule_type, schedule_value, status, next_run, last_run, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                task.to_row(),
            )
            self._conn.commit()

    def get(self, task_id: str) -> Optional[ScheduledTask]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return ScheduledTask.from_row(row) if row else None

    def list_all(self, status: Optional[str] = None) -> list[ScheduledTask]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM scheduled_tasks WHERE status = ? ORDER BY created_at",
                    (status,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM scheduled_tasks ORDER BY created_at"
                ).fetchall()
        return [ScheduledTask.from_row(r) for r in rows]

    def due(self, now_iso: str) -> list[ScheduledTask]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scheduled_tasks WHERE status = 'active' "
                "AND next_run IS NOT NULL AND next_run <= ?",
                (now_iso,),
            ).fetchall()
        return [ScheduledTask.from_row(r) for r in rows]

    def delete(self, task_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM scheduled_tasks WHERE id = ?", (task_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0


# ── Calcul next_run (sans dépendance externe) ────────────────────────────

def _compute_next_run(task: ScheduledTask, now: Optional[datetime] = None) -> Optional[str]:
    now = now or datetime.now()

    if task.schedule_type == "once":
        if task.last_run is not None:
            return None  # déjà exécutée, pas de répétition
        return task.schedule_value

    if task.schedule_type == "interval":
        try:
            seconds = float(task.schedule_value)
        except ValueError:
            log.warning("[Scheduler] interval invalide : %s", task.schedule_value)
            return None
        return (now + timedelta(seconds=seconds)).isoformat()

    if task.schedule_type == "daily":
        try:
            hh, mm = (int(x) for x in task.schedule_value.split(":"))
        except Exception:
            log.warning("[Scheduler] daily invalide (attendu HH:MM) : %s", task.schedule_value)
            return None
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate.isoformat()

    if task.schedule_type == "weekly":
        try:
            day_str, time_str = task.schedule_value.split(":", 1)
            target_day = _DAYS_FR[day_str.strip().lower()]
            hh, mm = (int(x) for x in time_str.split(":"))
        except Exception:
            log.warning("[Scheduler] weekly invalide (attendu jour:HH:MM) : %s", task.schedule_value)
            return None
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        days_ahead = (target_day - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate.isoformat()

    log.warning("[Scheduler] schedule_type inconnu : %s", task.schedule_type)
    return None


# ── Scheduler principal ───────────────────────────────────────────────────

class Scheduler:
    """
    Boucle de polling en thread daemon. À chaque tick, exécute les tâches
    dues via le callback `executor` fourni au démarrage (= route_message
    de main.py, qui traite le prompt comme si l'utilisateur l'avait tapé).
    """

    def __init__(self, db_path: Path = _DB_PATH, poll_interval: int = 30) -> None:
        self._store = _Store(db_path)
        self._poll_interval = poll_interval
        self._executor: Optional[Callable[[str], str]] = None
        self._on_result: Optional[Callable[[str, str], None]] = None
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(
        self,
        executor: Callable[[str], str],
        on_result: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        """
        executor(prompt) -> réponse texte. Typiquement main.route_message.
        on_result(prompt, résultat) -> callback optionnel (ex: TTS, notif).
        """
        self._executor = executor
        self._on_result = on_result
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="onyx-scheduler")
        self._thread.start()
        log.info("[Scheduler] démarré (poll=%ds)", self._poll_interval)

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=self._poll_interval + 5)
            self._thread = None
        log.info("[Scheduler] arrêté")

    def create_task(self, prompt: str, schedule_type: str, schedule_value: str) -> ScheduledTask:
        task = ScheduledTask(
            id=uuid.uuid4().hex[:12],
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            created_at=datetime.now().isoformat(),
        )
        task.next_run = _compute_next_run(task)
        self._store.save(task)
        log.info("[Scheduler] tâche créée : %s", task.describe())
        return task

    def list_tasks(self, status: Optional[str] = None) -> list[ScheduledTask]:
        return self._store.list_all(status)

    def pause(self, task_id: str) -> bool:
        t = self._store.get(task_id)
        if not t:
            return False
        t.status = "paused"
        self._store.save(t)
        return True

    def resume(self, task_id: str) -> bool:
        t = self._store.get(task_id)
        if not t:
            return False
        t.status = "active"
        t.next_run = _compute_next_run(t)
        self._store.save(t)
        return True

    def cancel(self, task_id: str) -> bool:
        return self._store.delete(task_id)

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                now_iso = datetime.now().isoformat()
                for task in self._store.due(now_iso):
                    self._run_task(task)
            except Exception:
                log.exception("[Scheduler] erreur boucle de poll")
            self._stop_evt.wait(timeout=self._poll_interval)

    def _run_task(self, task: ScheduledTask) -> None:
        log.info("[Scheduler] exécution : %s", task.prompt)
        result = ""
        try:
            if self._executor:
                result = self._executor(task.prompt)
        except Exception as exc:
            result = f"Erreur tâche planifiée : {exc}"
            log.error("[Scheduler] échec tâche %s : %s", task.id, exc)

        task.last_run = datetime.now().isoformat()
        task.next_run = _compute_next_run(task)
        if task.next_run is None:
            task.status = "completed"
        self._store.save(task)

        if self._on_result:
            try:
                self._on_result(task.prompt, result)
            except Exception:
                log.exception("[Scheduler] on_result callback a échoué")


# ── Instance globale (import direct depuis main.py/actions.py) ─────────────
scheduler = Scheduler()


# ── Parsing langage naturel FR → création de tâche ──────────────────────────
# Couvre les formulations courantes pour le routing dans actions.py

import re as _re


def parse_and_schedule(texte: str) -> str:
    """
    Parse une commande FR du type :
      "tous les jours à 8h résume mes mails"
      "toutes les heures vérifie le cpu"
      "chaque lundi à 9h fais le point sur la semaine"
    Retourne un message de confirmation ou d'erreur.
    """
    t = texte.lower().strip()

    # tous les jours à HH(:MM)?
    m = _re.search(r"tous les jours? à (\d{1,2})h(\d{2})?\s*(.*)", t)
    if m:
        hh, mm, prompt = m.groups()
        mm = mm or "00"
        if not prompt:
            return "Précise quoi faire après l'heure (ex: 'tous les jours à 8h résume mes mails')."
        task = scheduler.create_task(prompt.strip(), "daily", f"{int(hh):02d}:{mm}")
        return f"OK, planifié quotidiennement à {int(hh):02d}:{mm} → {task.prompt}"

    # toutes les N heures/minutes
    m = _re.search(r"toutes les (\d+) (heures?|minutes?)\s*(.*)", t)
    if m:
        n, unit, prompt = m.groups()
        seconds = int(n) * (3600 if "heure" in unit else 60)
        if not prompt:
            return "Précise quoi faire (ex: 'toutes les 2 heures vérifie le cpu')."
        task = scheduler.create_task(prompt.strip(), "interval", str(seconds))
        return f"OK, planifié toutes les {n} {unit} → {task.prompt}"

    # chaque <jour> à HH(:MM)?
    m = _re.search(
        r"chaque (lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche) à (\d{1,2})h(\d{2})?\s*(.*)",
        t,
    )
    if m:
        jour, hh, mm, prompt = m.groups()
        mm = mm or "00"
        if not prompt:
            return "Précise quoi faire après l'heure."
        task = scheduler.create_task(prompt.strip(), "weekly", f"{jour}:{int(hh):02d}:{mm}")
        return f"OK, planifié chaque {jour} à {int(hh):02d}:{mm} → {task.prompt}"

    return (
        "Format pas reconnu. Essaie :\n"
        "  'tous les jours à 8h <action>'\n"
        "  'toutes les 2 heures <action>'\n"
        "  'chaque lundi à 9h <action>'"
    )


def list_tasks_human() -> str:
    """Pour le shortcut 'mes tâches planifiées'."""
    tasks = scheduler.list_tasks()
    if not tasks:
        return "Aucune tâche planifiée."
    return "\n".join(f"• {t.describe()}" for t in tasks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    def _dummy_executor(prompt: str) -> str:
        print(f"[exec] {prompt}")
        return "ok"

    scheduler.start(executor=_dummy_executor)
    print(parse_and_schedule("toutes les 1 minutes vérifie le cpu"))
    print(list_tasks_human())
    import time
    time.sleep(90)
    scheduler.stop()

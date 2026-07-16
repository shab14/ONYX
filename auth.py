"""
auth.py — ONYX authentication v4
PIN hashé via PBKDF2-SHA256. Rate-limit côté serveur.

FIXES v4:
- Credentials (salt + hash) stockés dans AppData/Roaming/ONYX/credentials.json
  → salt ALÉATOIRE par installation, plus rien de secret dans le code source
- 300k itérations pour les nouveaux credentials (100k legacy conservé)
- Migration : `python auth.py set <pin>` crée le fichier credentials
- Fallback legacy (hash hardcodé) si le fichier n'existe pas → zéro breaking change
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from collections import deque
from pathlib import Path
from threading import Lock

# ── Stockage credentials ──────────────────────────────────────────────────────
if os.name == "nt":
    _CRED_DIR = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "ONYX"
else:
    _CRED_DIR = Path.home() / ".local" / "share" / "onyx"
_CRED_FILE = _CRED_DIR / "credentials.json"

# ── Legacy (fallback si credentials.json absent) ─────────────────────────────
_LEGACY_SALT       = b"ONYX_INTERNAL_SALT_7f3a_v2"
_LEGACY_ITERATIONS = 100_000
_LEGACY_PIN_HASH   = "4b48405738b24b065c8e1df848214f5195b17eb598bb334146af544ee071e149"

_NEW_ITERATIONS = 300_000


def _hash_pin(pin: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations).hex()


def _load_credentials() -> tuple[bytes, str, int] | None:
    """Retourne (salt, hash, iterations) depuis credentials.json, ou None."""
    try:
        if not _CRED_FILE.exists():
            return None
        data = json.loads(_CRED_FILE.read_text(encoding="utf-8"))
        return bytes.fromhex(data["salt"]), data["hash"], int(data["iterations"])
    except Exception:
        return None


def set_pin(new_pin: str) -> str:
    """Crée/remplace les credentials avec un salt aléatoire. Retourne le chemin."""
    if not new_pin or len(new_pin) < 4:
        raise ValueError("PIN trop court (min 4 caractères).")
    salt = secrets.token_bytes(16)
    data = {
        "salt":       salt.hex(),
        "hash":       _hash_pin(new_pin, salt, _NEW_ITERATIONS),
        "iterations": _NEW_ITERATIONS,
    }
    _CRED_DIR.mkdir(parents=True, exist_ok=True)
    _CRED_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return str(_CRED_FILE)


def verify_pin(entered: str) -> bool:
    """True si PIN correct. Constant-time comparison."""
    if not entered:
        return False
    creds = _load_credentials()
    if creds is not None:
        salt, expected, iterations = creds
        return hmac.compare_digest(_hash_pin(entered, salt, iterations), expected)
    # Fallback legacy
    return hmac.compare_digest(
        _hash_pin(entered, _LEGACY_SALT, _LEGACY_ITERATIONS), _LEGACY_PIN_HASH
    )


# ── Session tokens post-authentification ─────────────────────────────────────
_SESSION_TTL = 3600.0  # secondes (1h)


class SessionStore:
    """Tokens de session éphémères. 32 bytes aléatoires, expiry configurable."""

    def __init__(self, ttl_seconds: float = _SESSION_TTL) -> None:
        self._ttl   = ttl_seconds
        self._store: dict[str, float] = {}  # token → expiry (monotonic)
        self._lock  = Lock()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            self._store[token] = time.monotonic() + self._ttl
        return token

    def validate(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            expiry = self._store.get(token)
            if expiry is None:
                return False
            if time.monotonic() > expiry:
                del self._store[token]
                return False
            return True

    def revoke(self, token: str) -> None:
        with self._lock:
            self._store.pop(token, None)

    def _purge(self) -> None:
        now = time.monotonic()
        for t in [t for t, exp in self._store.items() if now > exp]:
            del self._store[t]


session_store = SessionStore()


# ── Rate limiter ──────────────────────────────────────────────────────────────
class RateLimiter:
    """Bloque après N tentatives échouées sur fenêtre glissante."""

    def __init__(self, max_attempts: int = 5, window_seconds: float = 300.0) -> None:
        self._max    = max_attempts
        self._window = window_seconds
        self._fails: dict[str, deque[float]] = {}
        self._lock   = Lock()

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            now = time.time()
            q   = self._fails.get(key)
            if not q:
                return False
            while q and now - q[0] > self._window:
                q.popleft()
            return len(q) >= self._max

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._fails.setdefault(key, deque()).append(time.time())

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


server_rate_limiter = RateLimiter(max_attempts=5, window_seconds=300.0)


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "set":
        path = set_pin(args[1])
        print(f"✓ Credentials créés (salt aléatoire, {_NEW_ITERATIONS} itérations) : {path}")
    elif len(args) == 1:
        # rétro-compat : affiche un hash legacy
        print(_hash_pin(args[0], _LEGACY_SALT, _LEGACY_ITERATIONS))
        print("→ Recommandé : python auth.py set <pin>  (salt aléatoire hors code)")
    else:
        print("Usage:\n  python auth.py set <pin>   # crée credentials.json (recommandé)\n  python auth.py <pin>       # affiche hash legacy")

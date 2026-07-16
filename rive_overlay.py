"""
rive_overlay.py — ONYX Rive Avatar Overlay v1
Remplace le canvas tkinter par un avatar Rive animé (.riv) affiché dans une
fenêtre Edge en mode app (kiosk sans chrome).

Architecture :
    gui.py.set_state(s)
        ↓
    RiveOverlay.set_state(s)  → push dans queue
        ↓
    mini HTTP server (stdlib, 127.0.0.1, daemon thread)
        ↓ SSE stream /events
    msedge --app=http://127.0.0.1:8101/  (subprocess)
        ↓ embed rive.js runtime + JS reçoit états
    canvas WebGL Rive → joue animation correspondante

Zero dépendance externe (que stdlib + subprocess). Fallback gracieux si .riv
absent, Edge introuvable, ou port pris : retourne available=False, gui.py
bascule alors sur vocal_overlay.py (canvas tkinter).

API publique :
    overlay = RiveOverlay(on_close=callback)
    if overlay.available:
        overlay.start()
        overlay.set_state("LISTENING")
        ...
        overlay.destroy()
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

try:
    from config import (
        RIVE_EDGE_PATH, RIVE_ENABLED, RIVE_FILE, RIVE_OVERLAY_PORT,
        RIVE_STATE_INPUT, RIVE_STATE_MACHINE, RIVE_WINDOW_SIZE,
    )
except ImportError:
    RIVE_ENABLED       = False
    RIVE_FILE          = Path.home() / "ONYX" / "avatar" / "onyx.riv"
    RIVE_OVERLAY_PORT  = 8101
    RIVE_WINDOW_SIZE   = (380, 480)
    RIVE_EDGE_PATH     = ""
    RIVE_STATE_MACHINE = "OnyxStateMachine"
    RIVE_STATE_INPUT   = "state"

# Mapping état → entier (cohérent avec inputs Rive State Machine)
_STATE_MAP: dict[str, int] = {
    "OFF":          0,
    "LISTENING":    1,
    "THINKING":     2,
    "SPEAKING":     3,
    "MUTED":        4,
    "WAITING_WAKE": 5,
}


def _find_edge() -> Optional[str]:
    """Cherche msedge.exe. Retourne chemin ou None."""
    if RIVE_EDGE_PATH and Path(RIVE_EDGE_PATH).exists():
        return RIVE_EDGE_PATH
    # PATH d'abord
    in_path = shutil.which("msedge")
    if in_path:
        return in_path
    # Chemins Windows classiques
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            return s.connect_ex((host, port)) != 0
    except Exception:
        return True


# ── HTML embed (Rive runtime CDN + WebGL canvas + SSE client) ────────────────
_HTML_PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8" />
<title>ONYX</title>
<style>
  :root {
    --bg: #00060a;
    --pri: #00d4ff;
    --pri-dim: #007a99;
    --text: #8ffcff;
    --border: #1a5c7a;
    --green: #00ff88;
    --orange: #ff6b00;
    --yellow: #ffcc00;
    --red: #ff3366;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    width: 100%; height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: 'Courier New', monospace;
    overflow: hidden;
    -webkit-user-select: none; user-select: none;
  }
  #stage {
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    width: 100vw; height: 100vh;
    padding: 18px;
  }
  #rive-canvas {
    width: 320px; height: 320px;
    image-rendering: pixelated;
  }
  #fallback {
    display: none;
    width: 320px; height: 320px;
    align-items: center; justify-content: center;
    border-radius: 50%;
    border: 2px solid var(--pri-dim);
    box-shadow: 0 0 60px var(--pri-dim);
    background: radial-gradient(circle at center, var(--pri-dim) 0%, transparent 70%);
    transition: box-shadow .3s, border-color .3s;
  }
  #fallback.show { display: flex; }
  #label {
    margin-top: 22px;
    font-size: 13px; font-weight: bold;
    letter-spacing: 2px;
    text-align: center;
  }
  #status-dot {
    display: inline-block;
    width: 8px; height: 8px; border-radius: 50%;
    margin-right: 8px;
    background: var(--green);
    box-shadow: 0 0 8px currentColor;
    animation: pulse 1.4s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: .3; }
    50%      { opacity: 1; }
  }
  #connection {
    position: fixed; bottom: 12px; right: 14px;
    font-size: 10px; opacity: .35;
  }
  .state-LISTENING    #status-dot { background: var(--green);  }
  .state-THINKING     #status-dot { background: var(--yellow); }
  .state-SPEAKING     #status-dot { background: var(--orange); }
  .state-MUTED        #status-dot { background: var(--red);    }
  .state-WAITING_WAKE #status-dot { background: var(--pri-dim);}
  .state-OFF          #status-dot { background: #444;          }
  .state-LISTENING    #fallback   { border-color: var(--green);  box-shadow: 0 0 60px var(--green);  }
  .state-THINKING     #fallback   { border-color: var(--yellow); box-shadow: 0 0 60px var(--yellow); animation: spin 2s linear infinite; }
  .state-SPEAKING     #fallback   { border-color: var(--orange); box-shadow: 0 0 80px var(--orange); animation: bump .35s ease infinite alternate; }
  .state-MUTED        #fallback   { border-color: var(--red);    box-shadow: 0 0 30px var(--red);    }
  .state-WAITING_WAKE #fallback   { animation: breathe 3s ease-in-out infinite; }
  @keyframes spin    { to { transform: rotate(360deg); } }
  @keyframes bump    { from { transform: scale(1); } to { transform: scale(1.08); } }
  @keyframes breathe { 0%,100% { transform: scale(.96); opacity: .7; } 50% { transform: scale(1.04); opacity: 1; } }
</style>
</head>
<body class="state-LISTENING">
  <div id="stage">
    <canvas id="rive-canvas" width="640" height="640"></canvas>
    <div id="fallback"><span style="font-size:18px;letter-spacing:6px;">ONYX</span></div>
    <div id="label"><span id="status-dot"></span><span id="state-text">LISTENING</span></div>
  </div>
  <div id="connection">●</div>

<script src="https://unpkg.com/@rive-app/canvas@2.21.6"></script>
<script>
(function() {
  const STATE_MAP = { OFF:0, LISTENING:1, THINKING:2, SPEAKING:3, MUTED:4, WAITING_WAKE:5 };
  const CFG = __CFG__;
  const labelEl = document.getElementById('state-text');
  const bodyEl  = document.body;
  const conn    = document.getElementById('connection');
  const fb      = document.getElementById('fallback');
  const canvas  = document.getElementById('rive-canvas');
  let stateInput = null;
  let riveOk = false;

  function applyState(state) {
    bodyEl.className = 'state-' + state;
    labelEl.textContent = state.replace('_', ' ');
    if (riveOk && stateInput) {
      try { stateInput.value = STATE_MAP[state] ?? 0; } catch (e) {}
    }
  }

  // Tente de charger le .riv. Si fail (404, no WebGL, etc.) → fallback CSS.
  function bootRive() {
    if (typeof rive === 'undefined') {
      console.warn('Rive runtime absent → fallback CSS');
      fb.classList.add('show');
      return;
    }
    try {
      const r = new rive.Rive({
        src: '/avatar.riv',
        canvas: canvas,
        autoplay: true,
        stateMachines: CFG.stateMachine,
        onLoad: () => {
          const inputs = r.stateMachineInputs(CFG.stateMachine);
          if (inputs) {
            stateInput = inputs.find(i => i.name === CFG.stateInput);
          }
          riveOk = true;
          canvas.style.display = 'block';
          fb.classList.remove('show');
          console.log('[ONYX] Rive OK', CFG.stateMachine);
        },
        onLoadError: (err) => {
          console.warn('[ONYX] Rive load error:', err);
          canvas.style.display = 'none';
          fb.classList.add('show');
        },
      });
    } catch (e) {
      console.warn('[ONYX] Rive init crash:', e);
      canvas.style.display = 'none';
      fb.classList.add('show');
    }
  }

  // Server-Sent Events : reçoit états depuis ONYX (gui/voice_mode).
  function bootSSE() {
    const es = new EventSource('/events');
    es.onopen = () => { conn.style.opacity = .35; conn.style.color = '#00ff88'; };
    es.onerror = () => { conn.style.opacity = 1;   conn.style.color = '#ff3366'; };
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.state) applyState(data.state);
      } catch (e) {}
    };
  }

  bootRive();
  bootSSE();
  applyState('LISTENING');
})();
</script>
</body>
</html>
"""


# ── Mini HTTP server (stdlib) ────────────────────────────────────────────────
class _OverlayHandler(BaseHTTPRequestHandler):
    # Variables de classe injectées par RiveOverlay
    state_queue: "queue.Queue[dict]" = queue.Queue()
    rive_file_path: Path = Path()
    listeners: list["queue.Queue[dict]"] = []
    listeners_lock = threading.Lock()
    cfg_json: str = "{}"

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        # Réduit le bruit dans stderr (HTTP server bavard par défaut)
        return

    def _ok(self, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        if ctype != "text/event-stream":
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _404(self) -> None:
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            page = _HTML_PAGE.replace("__CFG__", self.cfg_json)
            self._ok(page.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/avatar.riv":
            try:
                if not self.rive_file_path.exists():
                    self._404()
                    return
                data = self.rive_file_path.read_bytes()
                self._ok(data, "application/octet-stream")
            except Exception as exc:
                log.warning("[Rive] Lecture .riv : %s", exc)
                self._404()
            return

        if path == "/events":
            self._stream_sse()
            return

        if path == "/health":
            self._ok(b'{"ok":true}', "application/json")
            return

        self._404()

    def _stream_sse(self) -> None:
        """SSE long-polling. Reste ouvert et yield états jusqu'à déconnexion."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.flush()
        except Exception:
            return

        my_q: queue.Queue[dict] = queue.Queue()
        with self.listeners_lock:
            self.listeners.append(my_q)

        # Envoie immédiatement le dernier état connu (sinon écran vide au refresh)
        try:
            init = {"state": "LISTENING"}
            self.wfile.write(f"data: {json.dumps(init)}\n\n".encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass

        last_ping = time.time()
        try:
            while True:
                try:
                    msg = my_q.get(timeout=10.0)
                    payload = f"data: {json.dumps(msg)}\n\n".encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()
                except queue.Empty:
                    # Heartbeat pour garder la connexion vivante
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self.listeners_lock:
                try:
                    self.listeners.remove(my_q)
                except ValueError:
                    pass


# ── Overlay public API ───────────────────────────────────────────────────────
class RiveOverlay:
    """
    Overlay vocal next-gen avec avatar Rive animé.

    Fallback gracieux : si Rive indisponible (.riv absent, Edge absent, port pris),
    .available = False → gui.py bascule sur vocal_overlay.py.
    """

    def __init__(self, on_close: Optional[Callable[[], None]] = None) -> None:
        self._on_close = on_close
        self._server: Optional[ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._edge_proc: Optional[subprocess.Popen] = None
        self._watch_thread: Optional[threading.Thread] = None
        self._closed = False
        self._port = RIVE_OVERLAY_PORT

        self._available = self._check_availability()

    @property
    def available(self) -> bool:
        return self._available

    def _check_availability(self) -> bool:
        if not RIVE_ENABLED:
            log.info("[Rive] Désactivé via config")
            return False
        if not Path(RIVE_FILE).exists():
            log.warning("[Rive] .riv introuvable : %s", RIVE_FILE)
            # On reste available=True : le HTML a un fallback CSS animé
            # qui marche sans .riv. Décommente la ligne suivante pour
            # forcer fallback canvas tkinter si .riv manque.
            # return False
        if _find_edge() is None:
            log.warning("[Rive] msedge introuvable")
            return False
        # Cherche un port libre dans une plage
        for p in range(RIVE_OVERLAY_PORT, RIVE_OVERLAY_PORT + 20):
            if _port_free(p):
                self._port = p
                break
        else:
            log.warning("[Rive] Aucun port libre dans %d-%d",
                        RIVE_OVERLAY_PORT, RIVE_OVERLAY_PORT + 19)
            return False
        return True

    def start(self) -> bool:
        """Démarre HTTP server + lance Edge. Retourne True si tout OK."""
        if not self._available or self._closed:
            return False
        try:
            self._start_server()
            self._launch_edge()
            return True
        except Exception as exc:
            log.error("[Rive] Démarrage échoué : %s", exc, exc_info=True)
            self.destroy()
            return False

    def _start_server(self) -> None:
        # Injecte les variables de classe (queue, paths, config JSON)
        cfg = {
            "stateMachine": RIVE_STATE_MACHINE,
            "stateInput":   RIVE_STATE_INPUT,
        }
        _OverlayHandler.cfg_json       = json.dumps(cfg)
        _OverlayHandler.rive_file_path = Path(RIVE_FILE)
        _OverlayHandler.listeners      = []
        _OverlayHandler.listeners_lock = threading.Lock()

        # 127.0.0.1 only — pas exposé sur LAN
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), _OverlayHandler)
        self._server.daemon_threads = True
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="RiveOverlayHTTP",
        )
        self._server_thread.start()
        log.info("[Rive] HTTP server actif sur 127.0.0.1:%d", self._port)

    def _launch_edge(self) -> None:
        edge = _find_edge()
        if edge is None:
            raise RuntimeError("msedge introuvable")
        w, h = RIVE_WINDOW_SIZE
        url = f"http://127.0.0.1:{self._port}/"
        # --app= lance en mode "fenêtre app" sans onglets/URL bar
        # --user-data-dir isolé pour éviter de polluer le profil principal
        try:
            user_dir = Path(os.environ.get("TEMP", "/tmp")) / "onyx_rive_profile"
            user_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            user_dir = None

        cmd = [
            edge,
            f"--app={url}",
            f"--window-size={w},{h}",
            "--disable-features=GlobalMediaControls",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if user_dir:
            cmd.append(f"--user-data-dir={user_dir}")

        self._edge_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        log.info("[Rive] Edge lancé (PID %s)", self._edge_proc.pid)

        # Watch process : si user ferme la fenêtre Edge → on_close callback
        self._watch_thread = threading.Thread(
            target=self._watch_edge, daemon=True, name="RiveEdgeWatch",
        )
        self._watch_thread.start()

    def _watch_edge(self) -> None:
        if not self._edge_proc:
            return
        try:
            self._edge_proc.wait()
        except Exception:
            return
        if self._closed:
            return
        log.info("[Rive] Edge fermé par l'utilisateur")
        cb = self._on_close
        if cb:
            try:
                cb()
            except Exception as exc:
                log.warning("[Rive] on_close callback : %s", exc)

    def set_state(self, state: str) -> None:
        """Push un nouvel état vers les clients SSE connectés."""
        if self._closed or not self._available:
            return
        if state not in _STATE_MAP:
            log.warning("[Rive] État inconnu : %r", state)
            return
        msg = {"state": state, "code": _STATE_MAP[state]}
        with _OverlayHandler.listeners_lock:
            listeners = list(_OverlayHandler.listeners)
        for q in listeners:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    def destroy(self) -> None:
        """Tue Edge + arrête HTTP server. Idempotent."""
        if self._closed:
            return
        self._closed = True

        # Edge
        if self._edge_proc:
            try:
                self._edge_proc.terminate()
                try:
                    self._edge_proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._edge_proc.kill()
            except Exception:
                pass
            self._edge_proc = None

        # HTTP server
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None

        log.info("[Rive] Overlay détruit")


# ── Test standalone ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    overlay = RiveOverlay()
    if not overlay.available:
        print("Rive overlay indisponible. Vérifie config.RIVE_ENABLED, Edge installé, port libre.")
        raise SystemExit(1)
    overlay.start()
    print(f"Rive overlay actif sur http://127.0.0.1:{overlay._port}/")
    print("Cycle d'états (Ctrl+C pour quitter)…")
    try:
        states = ["LISTENING", "THINKING", "SPEAKING", "LISTENING", "WAITING_WAKE", "MUTED"]
        i = 0
        while True:
            s = states[i % len(states)]
            print(f"  → {s}")
            overlay.set_state(s)
            time.sleep(2.5)
            i += 1
    except KeyboardInterrupt:
        overlay.destroy()
        print("\nBye.")

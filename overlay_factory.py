"""
overlay_factory.py — ONYX Overlay Factory v1
Choisit automatiquement Rive (next-gen) ou tkinter (fallback) selon dispo.

Usage depuis gui.py :
    from overlay_factory import make_overlay
    overlay = make_overlay(master, on_close=self._stop_vocal)
    overlay.set_state("LISTENING")
    overlay.destroy()

L'interface publique (set_state, destroy) est identique pour les 2 backends.
Si Rive est dispo → fenêtre Edge avec avatar .riv.
Sinon → VocalOverlay tkinter classique (canvas animé).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Protocol

log = logging.getLogger(__name__)


class _OverlayLike(Protocol):
    """Interface commune Rive / tkinter."""
    def set_state(self, state: str) -> None: ...
    def destroy(self) -> None: ...


class _RiveOverlayAdapter:
    """Wrap RiveOverlay pour matcher l'interface VocalOverlay (focus, etc.)."""
    def __init__(self, rive: Any) -> None:
        self._rive = rive

    def set_state(self, state: str) -> None:
        try:
            self._rive.set_state(state)
        except Exception as exc:
            log.warning("[Overlay] Rive set_state : %s", exc)

    def destroy(self) -> None:
        try:
            self._rive.destroy()
        except Exception as exc:
            log.warning("[Overlay] Rive destroy : %s", exc)

    def focus(self) -> None:
        # No-op : Edge gère son focus seul
        pass


def make_overlay(
    master: Any,
    on_close: Optional[Callable[[], None]] = None,
) -> Optional[_OverlayLike]:
    """
    Fabrique l'overlay vocal optimal.

    1. Tente Rive (fenêtre Edge + avatar .riv).
    2. Si Rive KO → VocalOverlay tkinter (canvas animé).
    3. Si les 2 KO → None (gui.py continue sans overlay).
    """
    # 1. Tente Rive
    try:
        from rive_overlay import RiveOverlay
        rive = RiveOverlay(on_close=on_close)
        if rive.available and rive.start():
            log.info("[Overlay] Backend : Rive (Edge)")
            return _RiveOverlayAdapter(rive)
        else:
            # Cleanup si start() a partiellement réussi
            try:
                rive.destroy()
            except Exception:
                pass
    except ImportError:
        log.info("[Overlay] rive_overlay absent → tkinter")
    except Exception as exc:
        log.warning("[Overlay] Rive échec (%s) → fallback tkinter", exc)

    # 2. Fallback VocalOverlay tkinter
    try:
        from vocal_overlay import VocalOverlay
        overlay = VocalOverlay(master, on_close=on_close)
        log.info("[Overlay] Backend : tkinter Canvas")
        return overlay
    except ImportError:
        log.warning("[Overlay] vocal_overlay absent")
    except Exception as exc:
        log.warning("[Overlay] VocalOverlay crash : %s", exc)

    # 3. Plus rien
    log.warning("[Overlay] Aucun backend disponible")
    return None

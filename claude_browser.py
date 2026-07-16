"""
claude_browser.py — ONYX v9
Fixes : FAILSAFE=True, clipboard save/restore, opencv check pour confidence.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pyautogui
import pygetwindow as gw
import pyperclip

log = logging.getLogger(__name__)

pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.15

_TPL       = Path(__file__).parent / "templates"
_NEW_CHAT  = _TPL / "new_chat_btn.png"
_INPUT     = _TPL / "input_zone.png"

_CONF_LEVELS = (0.75, 0.65, 0.55)

_T_AFTER_OPEN  = 1.5
_T_AFTER_MAX   = 1.2
_T_AFTER_FOCUS = 0.5
_T_BETWEEN_TRY = 0.8


try:
    import cv2  # noqa: F401
    _OPENCV_OK = True
except ImportError:
    _OPENCV_OK = False
    log.warning("[Claude] opencv-python absent — locateOnScreen sans confidence")


class ClaudeError(Exception): pass
class ClaudeLaunchError(ClaudeError): pass


def _get_win():
    wins = gw.getWindowsWithTitle("Claude")
    for w in wins:
        title = (w.title or "").lower()
        if title.endswith(".txt") or title.endswith(".py") or title.endswith(".md"):
            continue
        return w
    return None


def wait_for_window(timeout: float = 8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        win = _get_win()
        if win:
            return win
        time.sleep(0.3)
    return None


def _is_maximized(win) -> bool:
    try:
        sw, sh = pyautogui.size()
        return win.width >= sw * 0.9 and win.height >= sh * 0.85
    except Exception:
        return False


def force_maximize(win) -> None:
    try:
        win.activate()
    except Exception:
        try:
            win.minimize()
            time.sleep(0.3)
            win.restore()
        except Exception:
            pass
    time.sleep(_T_AFTER_FOCUS)

    if _is_maximized(win):
        log.info("[Claude] Déjà maximisée ✓")
        return

    try:
        win.maximize()
        time.sleep(0.6)
        if _is_maximized(win):
            log.info("[Claude] Maximisée via pygetwindow ✓")
            return
    except Exception:
        pass

    pyautogui.hotkey("win", "up")
    time.sleep(0.8)
    win = _get_win()
    if win and _is_maximized(win):
        log.info("[Claude] Maximisée via Win+Up ✓")
        return

    if win:
        title_x = win.left + win.width // 2
        title_y = win.top + 12
        log.info(f"[Claude] Double-clic barre titre ({title_x}, {title_y})")
        pyautogui.doubleClick(title_x, title_y)
        time.sleep(_T_AFTER_MAX)


def _find_with_retries(tpl: Path, label: str):
    if not tpl.exists():
        log.warning(f"[Claude] Template absent : {tpl}")
        return None

    for attempt in range(3):
        if _OPENCV_OK:
            for conf in _CONF_LEVELS:
                try:
                    loc = pyautogui.locateOnScreen(str(tpl), confidence=conf)
                    if loc:
                        pt = pyautogui.center(loc)
                        log.info(f"[Claude] '{label}' conf={conf} → ({int(pt.x)},{int(pt.y)})")
                        return pt
                except Exception:
                    continue
        else:
            try:
                loc = pyautogui.locateOnScreen(str(tpl))
                if loc:
                    pt = pyautogui.center(loc)
                    log.info(f"[Claude] '{label}' (no opencv) → ({int(pt.x)},{int(pt.y)})")
                    return pt
            except Exception:
                pass
        time.sleep(_T_BETWEEN_TRY)
    return None


def click_new_chat() -> bool:
    pt = _find_with_retries(_NEW_CHAT, "New chat")
    if pt:
        pyautogui.click(pt.x, pt.y)
        time.sleep(1.0)
        return True

    log.info("[Claude] Template raté → fallback Ctrl+N")
    pyautogui.hotkey("ctrl", "n")
    time.sleep(1.0)
    return True


def click_input() -> None:
    pt = _find_with_retries(_INPUT, "Input zone")
    if pt:
        pyautogui.click(pt.x, pt.y)
    else:
        sw, sh = pyautogui.size()
        x, y = sw // 2, int(sh * 0.59)
        log.info(f"[Claude] Input fallback ratio → ({x},{y})")
        pyautogui.click(x, y)
    time.sleep(0.6)


def _clipboard_save_restore(prompt: str) -> None:
    """Save clipboard, paste prompt, restore après envoi."""
    try:
        saved = pyperclip.paste()
    except Exception:
        saved = ""
    pyperclip.copy(prompt)
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.4)
    pyautogui.press("enter")
    time.sleep(0.5)
    try:
        pyperclip.copy(saved)
    except Exception:
        pass


def send_prompt(prompt: str) -> None:
    click_new_chat()
    click_input()
    _clipboard_save_restore(prompt)
    log.info("[Claude] Prompt envoyé ✓")


def demander_a_claude(prompt: str, modele: str = "sonnet") -> str:
    if not prompt.strip():
        raise ClaudeError("Prompt vide.")

    time.sleep(_T_AFTER_OPEN)
    win = wait_for_window(timeout=8.0)
    if win is None:
        raise ClaudeLaunchError(
            "Fenêtre Claude introuvable après 8s.\n"
            "Vérifie que 'ouvre claude' fonctionne dans Onyx."
        )

    force_maximize(win)

    win = _get_win()
    if win and not _is_maximized(win):
        log.warning(f"[Claude] Pas vraiment maximisée: {win.width}x{win.height}")

    send_prompt(prompt)
    return "Prompt envoyé à Claude ✓"


def claude(prompt: str, modele: str = "sonnet") -> str:
    return demander_a_claude(prompt, modele)


def wait_for_claude(timeout: float = 8.0) -> bool:
    win = wait_for_window(timeout)
    if win is None:
        return False
    force_maximize(win)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(claude("Test ONYX v9 — réponds juste 'reçu'."))

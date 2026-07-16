"""
actions.py — ONYX v9
NOUVEAUTÉS v9 :
- recherche_web : DDG via API ddgs + résumé Ollama (plus robuste que scraping HTML)
- youtube_jouer   : scrape YouTube + ouvre la vidéo
- youtube_resumer : transcript API + résumé LLM (optionnel)
- meteo           : ouvre Google météo pour une ville
- envoyer_message : WhatsApp/Telegram/Discord/Signal via PyAutoGUI

FIXES conservés de v8.1 :
- taper_texte : pyperclip+Ctrl+V pour Unicode
- _safe_url   : logique morte nettoyée
- record_stop : guard contre _rec_file=None
- _safe_path  : précise le traversal
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
import webbrowser
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import pyautogui
import pyperclip
import requests
from bs4 import BeautifulSoup

from config import FFMPEG_PATH, LOG_FILE, MODEL_NAME, OLLAMA_URL, RECORDINGS_DIR, SCREENSHOTS_DIR, USER_HOME

try:
    from reminders import creer_rappel, lister_rappels, annuler_rappel
    REMINDERS_OK = True
except ImportError:
    REMINDERS_OK = False
    def creer_rappel(p): return type("R", (), {"succes": False, "message": "pip install schedule pyttsx3 plyer", "__str__": lambda s: s.message})()
    def lister_rappels(): return type("R", (), {"succes": False, "message": "Module reminders non chargé.", "__str__": lambda s: s.message})()
    def annuler_rappel(rid): return type("R", (), {"succes": False, "message": "Module reminders non chargé.", "__str__": lambda s: s.message})()

log = logging.getLogger(__name__)


def _strip_think(text: str) -> str:
    """Retire les blocs <think>…</think> (deepseek-r1) des réponses LLM."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


pyautogui.PAUSE    = 0.3
pyautogui.FAILSAFE = True

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}
_TIMEOUT         = 15
_MAX_RESULTATS   = 5
_ALLOWED_ROOTS   = {Path.home()}


# ── Types ─────────────────────────────────────────────────────────────────────

@dataclass
class ResultatAction:
    succes:  bool
    message: str
    donnees: Any = None

    def __str__(self) -> str:
        return self.message


# ── Sécurité chemins / URLs ───────────────────────────────────────────────────

def _safe_path(chemin: str) -> Path | None:
    if not chemin or not chemin.strip():
        return None
    try:
        p = Path(chemin.strip()).resolve()
    except Exception:
        return None
    for root in _ALLOWED_ROOTS:
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    log.warning("[safe_path] Traversal bloqué : %s", chemin)
    return None


def _safe_url(url: str) -> str | None:
    url = url.strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.hostname or ""
    if (
        host in ("localhost", "127.0.0.1", "0.0.0.0")
        or host.startswith("192.168.") or host.startswith("10.")
        or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host)
        or host.endswith(".local")
    ):
        return None
    return url


# ── Apps / Fichiers ───────────────────────────────────────────────────────────

_APP_NAME_RE = re.compile(r"^[\w .\-+&'’éèêëàâîïôûüç]+$", re.IGNORECASE)


def ouvrir_app(nom: str) -> ResultatAction:
    nom = nom.strip().strip("\"'")
    if not nom:
        return ResultatAction(False, "Nom d'app vide.")
    # shell=True + nom libre = injection possible ("calc & shutdown …") → on valide
    if not _APP_NAME_RE.match(nom) or len(nom) > 64:
        return ResultatAction(False, f"Nom d'app invalide : {nom!r}")
    try:
        if os.name == "nt":
            subprocess.Popen(nom, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen([nom], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ResultatAction(True, f"Application « {nom} » lancée.")
    except Exception as exc:
        # Fallback Windows : recherche dans le menu démarrer
        try:
            pyautogui.press("win")
            time.sleep(0.6)
            pyperclip.copy(nom)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.8)
            pyautogui.press("enter")
            time.sleep(2.0)
            return ResultatAction(True, f"Application « {nom} » lancée via menu démarrer.")
        except Exception:
            return ResultatAction(False, f"Impossible de lancer « {nom} » : {exc}")


def ouvrir_fichier(chemin: str) -> ResultatAction:
    p = _safe_path(chemin)
    if p is None:
        return ResultatAction(False, f"Chemin invalide : {chemin}")
    if not p.exists():
        return ResultatAction(False, f"Fichier introuvable : {p}")
    try:
        os.startfile(str(p)) if os.name == "nt" else subprocess.Popen(["xdg-open", str(p)])
        return ResultatAction(True, f"Fichier ouvert : {p.name}")
    except Exception as exc:
        return ResultatAction(False, f"Impossible d'ouvrir : {exc}")


def creer_fichier(chemin: str, contenu: str = "") -> ResultatAction:
    p = _safe_path(chemin)
    if p is None:
        return ResultatAction(False, f"Chemin invalide : {chemin}")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(contenu, encoding="utf-8")
        return ResultatAction(True, f"Fichier créé : {p}")
    except Exception as exc:
        return ResultatAction(False, f"Impossible de créer : {exc}")


def taper_texte(texte: str) -> ResultatAction:
    if not texte:
        return ResultatAction(False, "Texte vide.")
    try:
        saved = pyperclip.paste()
    except Exception:
        saved = ""
    try:
        pyperclip.copy(texte)
        time.sleep(0.15)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.1)
        return ResultatAction(True, f"Texte tapé ({len(texte)} chars).")
    except Exception as exc:
        return ResultatAction(False, f"Erreur taper_texte : {exc}")
    finally:
        try:
            pyperclip.copy(saved)
        except Exception:
            pass


def screenshot(nom: str | None = None) -> ResultatAction:
    try:
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        fname = nom or f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        path  = SCREENSHOTS_DIR / fname
        img   = pyautogui.screenshot()
        img.save(str(path))
        return ResultatAction(True, f"Screenshot : {path}")
    except Exception as exc:
        return ResultatAction(False, f"Screenshot échoué : {exc}")


# ── Volume ────────────────────────────────────────────────────────────────────

def _volume_cmd(action: str) -> ResultatAction:
    _com_init = False
    try:
        from ctypes import cast, POINTER, windll
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        # COM doit être initialisé dans CE thread (les actions tournent en worker)
        try:
            windll.ole32.CoInitialize(None)
            _com_init = True
        except Exception:
            pass

        devices = AudioUtilities.GetSpeakers()
        iface   = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol     = cast(iface, POINTER(IAudioEndpointVolume))

        if action == "mute":
            muted = vol.GetMute()
            vol.SetMute(not muted, None)
            return ResultatAction(True, "Son coupé." if not muted else "Son remis.")

        current = vol.GetMasterVolumeLevelScalar()
        step    = 0.10
        if action == "up":
            new = min(1.0, current + step)
            vol.SetMasterVolumeLevelScalar(new, None)
            return ResultatAction(True, f"Volume : {int(new * 100)}%")
        if action == "down":
            new = max(0.0, current - step)
            vol.SetMasterVolumeLevelScalar(new, None)
            return ResultatAction(True, f"Volume : {int(new * 100)}%")
        return ResultatAction(False, f"Action volume inconnue : {action}")
    except ImportError:
        key = {"up": "volumeup", "down": "volumedown", "mute": "volumemute"}.get(action)
        if key:
            pyautogui.press(key)
            return ResultatAction(True, f"Volume {action} (touche média).")
        return ResultatAction(False, "pycaw non installé.")
    except Exception as exc:
        return ResultatAction(False, f"Volume échoué : {exc}")
    finally:
        if _com_init:
            try:
                from ctypes import windll
                windll.ole32.CoUninitialize()
            except Exception:
                pass


def volume_up()   -> ResultatAction: return _volume_cmd("up")
def volume_down() -> ResultatAction: return _volume_cmd("down")
def volume_mute() -> ResultatAction: return _volume_cmd("mute")


# ── Système ───────────────────────────────────────────────────────────────────

def shutdown() -> ResultatAction:
    subprocess.run(["shutdown", "/s", "/t", "5"], check=False)
    return ResultatAction(True, "Arrêt du PC dans 5 secondes.")

def restart() -> ResultatAction:
    subprocess.run(["shutdown", "/r", "/t", "5"], check=False)
    return ResultatAction(True, "Redémarrage dans 5 secondes.")

def sleep_pc() -> ResultatAction:
    subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], check=False)
    return ResultatAction(True, "Mise en veille.")

def lock_pc() -> ResultatAction:
    import ctypes
    ctypes.windll.user32.LockWorkStation()
    return ResultatAction(True, "Session verrouillée.")

def infos_systeme() -> ResultatAction:
    try:
        import psutil
        cpu  = psutil.cpu_percent(interval=0.5)
        ram  = psutil.virtual_memory()
        disk = psutil.disk_usage(str(Path.home()))
        bat  = psutil.sensors_battery()
        bat_str = f"{bat.percent:.0f}% {'🔌' if bat.power_plugged else '🔋'}" if bat else "N/A"
        msg = (
            f"CPU : {cpu:.1f}%\n"
            f"RAM : {ram.percent:.1f}% ({ram.used // 1_048_576} Mo / {ram.total // 1_048_576} Mo)\n"
            f"Disque : {disk.percent:.1f}% ({disk.free // 1_073_741_824:.1f} Go libres)\n"
            f"Batterie : {bat_str}"
        )
        return ResultatAction(True, msg)
    except ImportError:
        return ResultatAction(False, "psutil non installé.")
    except Exception as exc:
        return ResultatAction(False, f"Infos système échouées : {exc}")


# ── Média ─────────────────────────────────────────────────────────────────────

def _media_key(key: str) -> ResultatAction:
    _map = {
        "play_pause": "playpause",
        "next":       "nexttrack",
        "prev":       "prevtrack",
        "stop":       "stop",
    }
    k = _map.get(key)
    if not k:
        return ResultatAction(False, f"Clé média inconnue : {key}")
    try:
        pyautogui.press(k)
        return ResultatAction(True, f"Média : {key}")
    except Exception as exc:
        return ResultatAction(False, f"Touche média échouée : {exc}")

def media_play_pause() -> ResultatAction: return _media_key("play_pause")
def media_next()       -> ResultatAction: return _media_key("next")
def media_prev()       -> ResultatAction: return _media_key("prev")
def media_stop()       -> ResultatAction: return _media_key("stop")


# ── Luminosité ────────────────────────────────────────────────────────────────

def luminosite_up(step: int = 10) -> ResultatAction:
    try:
        import screen_brightness_control as sbc
        cur = sbc.get_brightness()[0]
        sbc.set_brightness(min(100, cur + step))
        return ResultatAction(True, f"Luminosité : {min(100, cur + step)}%")
    except Exception as exc:
        return ResultatAction(False, f"Luminosité échouée : {exc}")

def luminosite_down(step: int = 10) -> ResultatAction:
    try:
        import screen_brightness_control as sbc
        cur = sbc.get_brightness()[0]
        sbc.set_brightness(max(0, cur - step))
        return ResultatAction(True, f"Luminosité : {max(0, cur - step)}%")
    except Exception as exc:
        return ResultatAction(False, f"Luminosité échouée : {exc}")

def luminosite_set(valeur: int) -> ResultatAction:
    try:
        import screen_brightness_control as sbc
        sbc.set_brightness(max(0, min(100, valeur)))
        return ResultatAction(True, f"Luminosité : {valeur}%")
    except Exception as exc:
        return ResultatAction(False, f"Luminosité échouée : {exc}")


# ── Recherche web DDG + résumé Ollama ─────────────────────────────────────────

def _ddg_search(query: str, max_results: int = _MAX_RESULTATS) -> list[dict]:
    """DDG via API ddgs. Plus robuste que scraping HTML."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append({
                "titre":   r.get("title", ""),
                "extrait": r.get("body", ""),
                "url":     r.get("href", ""),
            })
    return results


def _ollama_resume(query: str, raw: str) -> str:
    """Résumé Ollama des résultats DDG. Fallback : retourne raw si Ollama KO."""
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL_NAME,
                "stream": False,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Tu es ONYX. Résume les résultats de recherche web de façon concise. "
                            "Réponds en français, 2-4 phrases max. "
                            "Ne génère pas de liste, parle naturellement."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Question : {query}\n\nRésultats :\n{raw[:3000]}",
                    },
                ],
            },
            timeout=60,
        )
        resp.raise_for_status()
        return _strip_think(resp.json()["message"]["content"])
    except Exception as exc:
        log.warning("[Web] Résumé Ollama échoué : %s", exc)
        return raw


def recherche_web(query: str, nb: int = _MAX_RESULTATS, avec_resume: bool = True) -> ResultatAction:
    """
    Recherche DDG + résumé Ollama optionnel.
    avec_resume=True  → résumé LLM (défaut, 10-30s)
    avec_resume=False → liste brute immédiate
    """
    if not query.strip():
        return ResultatAction(False, "Requête vide.")
    nb = min(nb, 10)

    try:
        resultats = _ddg_search(query, nb)
    except Exception as exc:
        # Fallback BeautifulSoup si ddgs échoue
        log.warning("[Web] DDG API échoué, fallback BS4 : %s", exc)
        return _recherche_web_fallback(query, nb)

    if not resultats:
        return ResultatAction(False, "Aucun résultat.")

    # Texte brut pour le résumé ou l'affichage
    lignes = [f"🔍 Résultats pour « {query} »\n"]
    for i, r in enumerate(resultats, 1):
        lignes.append(f"  {i}. {r['titre']}")
        if r["extrait"]:
            lignes.append(f"     {r['extrait'][:150]}")
        lignes.append(f"     {r['url']}\n")
    brut = "\n".join(lignes)

    if avec_resume:
        resume = _ollama_resume(query, brut)
        return ResultatAction(True, resume, donnees=resultats)

    return ResultatAction(True, brut, donnees=resultats)


def _recherche_web_fallback(query: str, nb: int = _MAX_RESULTATS) -> ResultatAction:
    """Fallback scraping HTML DDG si l'API ddgs est KO."""
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        soup      = BeautifulSoup(resp.text, "html.parser")
        resultats = []
        for res in soup.select(".result__body")[:nb]:
            titre_tag   = res.select_one(".result__title a")
            extrait_tag = res.select_one(".result__snippet")
            if not titre_tag:
                continue
            href = titre_tag.get("href", "")
            if "uddg=" in href:
                qs   = parse_qs(urlparse(href).query)
                href = unquote(qs.get("uddg", [href])[0])
            resultats.append({
                "titre":   titre_tag.get_text(strip=True),
                "url":     href,
                "extrait": extrait_tag.get_text(strip=True) if extrait_tag else "",
            })
        if not resultats:
            return ResultatAction(False, "Aucun résultat.")
        lignes = [f"🔍 {query}\n"]
        for i, r in enumerate(resultats, 1):
            lignes.append(f"  {i}. {r['titre']}")
            if r["extrait"]:
                lignes.append(f"     {r['extrait'][:120]}")
            lignes.append(f"     {r['url']}\n")
        return ResultatAction(True, "\n".join(lignes), donnees=resultats)
    except Exception as exc:
        return ResultatAction(False, f"Recherche échouée : {exc}")


def ouvrir_url(url: str) -> ResultatAction:
    safe = _safe_url(url)
    if not safe:
        return ResultatAction(False, f"URL invalide ou bloquée : {url}")
    try:
        webbrowser.open(safe)
        return ResultatAction(True, f"URL ouverte : {safe}")
    except Exception as exc:
        return ResultatAction(False, f"Impossible d'ouvrir l'URL : {exc}")

def scraper_page(url: str, selecteur: str | None = None) -> ResultatAction:
    safe = _safe_url(url)
    if not safe:
        return ResultatAction(False, f"URL invalide : {url}")
    try:
        resp = requests.get(safe, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        if selecteur:
            el = soup.select(selecteur)
            text = "\n".join(e.get_text(separator=" ", strip=True) for e in el)
        else:
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return ResultatAction(True, text[:3000] if len(text) > 3000 else text)
    except Exception as exc:
        return ResultatAction(False, f"Scraping échoué : {exc}")


# ── YouTube ───────────────────────────────────────────────────────────────────

_YT_FILTER   = "EgIQAQ%3D%3D"  # filtre "vidéos uniquement" (pas les shorts)
_YT_HEADERS  = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


def _yt_scrape_url(query: str) -> str | None:
    """Scrape le premier résultat non-Shorts YouTube pour une requête."""
    search_url = (
        f"https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}&sp={_YT_FILTER}"
    )
    try:
        resp     = requests.get(search_url, headers=_YT_HEADERS, timeout=12)
        html     = resp.text
        video_ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html)
        seen: set[str] = set()
        for vid in video_ids:
            if vid in seen:
                continue
            seen.add(vid)
            if f"/shorts/{vid}" in html:
                continue
            return f"https://www.youtube.com/watch?v={vid}"
    except Exception as exc:
        log.warning("[YouTube] Scrape échoué : %s", exc)
    return None


def youtube_jouer(query: str) -> ResultatAction:
    """Cherche et ouvre la première vidéo YouTube (non-Short) pour `query`."""
    if not query.strip():
        return ResultatAction(False, "Requête YouTube vide.")

    log.info("[YouTube] Recherche : %s", query)
    video_url = _yt_scrape_url(query)

    if video_url:
        webbrowser.open(video_url)
        return ResultatAction(True, f"YouTube : {query} ▶")

    # Fallback : ouvre la page de résultats filtrée
    fallback = (
        f"https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}&sp={_YT_FILTER}"
    )
    webbrowser.open(fallback)
    return ResultatAction(True, f"YouTube résultats pour : {query}")


def youtube_resumer(url_video: str) -> ResultatAction:
    """
    Récupère le transcript d'une vidéo YouTube et le résume via Ollama.
    Nécessite : pip install youtube-transcript-api
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return ResultatAction(False, "pip install youtube-transcript-api")

    # Extraction video ID
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", url_video)
    if not m:
        return ResultatAction(False, f"ID vidéo introuvable dans : {url_video}")
    vid = m.group(1)

    try:
        tl         = YouTubeTranscriptApi.list_transcripts(vid)
        langs      = ["fr", "en", "de", "es", "it", "pt"]
        transcript = None
        try:
            transcript = tl.find_manually_created_transcript(langs)
        except Exception:
            try:
                transcript = tl.find_generated_transcript(langs)
            except Exception:
                for t in tl:
                    transcript = t
                    break

        if transcript is None:
            return ResultatAction(False, "Pas de transcript disponible.")

        texte = " ".join(e["text"] for e in transcript.fetch())
    except Exception as exc:
        return ResultatAction(False, f"Transcript échoué : {exc}")

    # Résumé Ollama
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL_NAME,
                "stream": False,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Tu es ONYX. Résume ce transcript YouTube de façon concise. "
                            "Structure : 1 phrase d'intro, 3-5 points clés. "
                            "Réponds dans la langue du transcript."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"URL : {url_video}\n\nTranscript :\n{texte[:10000]}",
                    },
                ],
            },
            timeout=120,
        )
        resp.raise_for_status()
        resume = _strip_think(resp.json()["message"]["content"])
        return ResultatAction(True, resume)
    except Exception as exc:
        return ResultatAction(False, f"Résumé LLM échoué : {exc}")


# ── Météo ─────────────────────────────────────────────────────────────────────

def meteo(ville: str, quand: str = "aujourd'hui") -> ResultatAction:
    """Ouvre Google avec la météo pour une ville."""
    if not ville.strip():
        return ResultatAction(False, "Ville non spécifiée.")
    q   = f"météo {ville} {quand}"
    url = f"https://www.google.com/search?q={quote_plus(q)}"
    try:
        webbrowser.open(url)
        return ResultatAction(True, f"Météo de {ville} ({quand}) ouverte.")
    except Exception as exc:
        return ResultatAction(False, f"Impossible d'ouvrir la météo : {exc}")


# ── Envoyer message (PyAutoGUI) ───────────────────────────────────────────────

def _paste(texte: str) -> None:
    """Colle du texte via clipboard (unicode-safe)."""
    try:
        saved = pyperclip.paste()
    except Exception:
        saved = ""
    pyperclip.copy(texte)
    time.sleep(0.15)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.1)
    try:
        pyperclip.copy(saved)
    except Exception:
        pass


def _ouvrir_app_msg(nom_app: str) -> bool:
    """Lance une app via menu démarrer Windows."""
    try:
        pyautogui.press("win")
        time.sleep(0.5)
        _paste(nom_app)
        time.sleep(0.7)
        pyautogui.press("enter")
        time.sleep(2.5)
        return True
    except Exception as exc:
        log.warning("[Message] Lancement %s échoué : %s", nom_app, exc)
        return False


def _chercher_contact(contact: str) -> None:
    pyautogui.hotkey("ctrl", "f")
    time.sleep(0.5)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    _paste(contact)
    time.sleep(1.0)
    pyautogui.press("enter")
    time.sleep(0.8)


def _envoi_desktop(app: str, contact: str, message: str) -> ResultatAction:
    if not _ouvrir_app_msg(app):
        return ResultatAction(False, f"Impossible d'ouvrir {app}.")
    _chercher_contact(contact)
    _paste(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)
    return ResultatAction(True, f"Message envoyé à {contact} via {app}.")


def _envoi_whatsapp(contact: str, message: str) -> ResultatAction:
    return _envoi_desktop("WhatsApp", contact, message)

def _envoi_telegram(contact: str, message: str) -> ResultatAction:
    return _envoi_desktop("Telegram", contact, message)

def _envoi_discord(contact: str, message: str) -> ResultatAction:
    return _envoi_desktop("Discord", contact, message)

def _envoi_signal(contact: str, message: str) -> ResultatAction:
    return _envoi_desktop("Signal", contact, message)


_PLATFORM_MAP: list[tuple[frozenset[str], Any]] = [
    (frozenset({"whatsapp", "wp", "wapp"}),              _envoi_whatsapp),
    (frozenset({"telegram", "tg"}),                      _envoi_telegram),
    (frozenset({"discord"}),                             _envoi_discord),
    (frozenset({"signal"}),                              _envoi_signal),
]


def envoyer_message(contact: str, message: str, plateforme: str = "whatsapp") -> ResultatAction:
    """
    Envoie un message texte via une app de messagerie installée (PyAutoGUI).
    plateforme : 'whatsapp' | 'telegram' | 'discord' | 'signal'
    """
    if not contact.strip():
        return ResultatAction(False, "Contact non spécifié.")
    if not message.strip():
        return ResultatAction(False, "Message vide.")

    key = plateforme.lower().strip()
    for kws, handler in _PLATFORM_MAP:
        if any(k in key for k in kws):
            log.info("[Message] %s → %s", plateforme, contact)
            return handler(contact, message)

    # Fallback générique
    return _envoi_desktop(plateforme.title(), contact, message)


# ── Fichiers ──────────────────────────────────────────────────────────────────

def _fmt_size(octets: int) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if octets < 1024:
            return f"{octets:.1f} {unit}"
        octets /= 1024
    return f"{octets:.1f} To"

def supprimer_fichier(chemin: str) -> ResultatAction:
    p = _safe_path(chemin)
    if p is None:
        return ResultatAction(False, f"Chemin invalide : {chemin}")
    if not p.exists():
        return ResultatAction(False, f"Introuvable : {p}")
    try:
        import send2trash
        send2trash.send2trash(str(p))
        return ResultatAction(True, f"Mis à la corbeille : {p.name}")
    except ImportError:
        try:
            if p.is_dir():
                shutil.rmtree(str(p))
            else:
                p.unlink()
            return ResultatAction(True, f"Supprimé : {p.name}")
        except Exception as exc:
            return ResultatAction(False, f"Suppression échouée : {exc}")

def renommer_fichier(chemin: str, nouveau: str) -> ResultatAction:
    p = _safe_path(chemin)
    if p is None:
        return ResultatAction(False, f"Chemin invalide : {chemin}")
    if not p.exists():
        return ResultatAction(False, f"Introuvable : {p}")
    dest = p.parent / nouveau
    try:
        p.rename(dest)
        return ResultatAction(True, f"Renommé : {p.name} → {dest.name}")
    except Exception as exc:
        return ResultatAction(False, f"Renommage échoué : {exc}")

def copier_fichier(source: str, destination: str) -> ResultatAction:
    src = _safe_path(source)
    dst = _safe_path(destination)
    if not src or not dst:
        return ResultatAction(False, "Chemin source ou destination invalide.")
    if not src.exists():
        return ResultatAction(False, f"Source introuvable : {src}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))
        return ResultatAction(True, f"Copié : {src.name} → {dst}")
    except Exception as exc:
        return ResultatAction(False, f"Copie échouée : {exc}")

def deplacer_fichier(source: str, destination: str) -> ResultatAction:
    src = _safe_path(source)
    dst = _safe_path(destination)
    if not src or not dst:
        return ResultatAction(False, "Chemin invalide.")
    if not src.exists():
        return ResultatAction(False, f"Source introuvable : {src}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return ResultatAction(True, f"Déplacé : {src.name} → {dst}")
    except Exception as exc:
        return ResultatAction(False, f"Déplacement échoué : {exc}")

def lister_dossier(chemin: str) -> ResultatAction:
    p = _safe_path(chemin) if chemin else USER_HOME / "Desktop"
    if p is None:
        return ResultatAction(False, f"Chemin invalide : {chemin}")
    if not p.exists():
        return ResultatAction(False, f"Dossier introuvable : {p}")
    try:
        items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        lignes = [f"📁 {p}\n"]
        for item in items:
            icon = "📄" if item.is_file() else "📂"
            size = f" ({_fmt_size(item.stat().st_size)})" if item.is_file() else ""
            lignes.append(f"  {icon} {item.name}{size}")
        return ResultatAction(True, "\n".join(lignes), donnees=[str(i) for i in items])
    except Exception as exc:
        return ResultatAction(False, f"Listage échoué : {exc}")

def zipper(chemin: str) -> ResultatAction:
    p = _safe_path(chemin)
    if p is None:
        return ResultatAction(False, f"Chemin invalide : {chemin}")
    if not p.exists():
        return ResultatAction(False, f"Introuvable : {p}")
    dest = p.parent / (p.name + ".zip")
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        zf.write(f, f.relative_to(p.parent))
            else:
                zf.write(p, p.name)
        return ResultatAction(True, f"Zippé : {dest.name} ({_fmt_size(dest.stat().st_size)})")
    except Exception as exc:
        return ResultatAction(False, f"Zip échoué : {exc}")

def dezipper(chemin: str, destination: str | None = None) -> ResultatAction:
    p = _safe_path(chemin)
    if p is None:
        return ResultatAction(False, f"Chemin invalide : {chemin}")
    if not p.exists():
        return ResultatAction(False, f"Archive introuvable : {p}")
    dest = _safe_path(destination) if destination else p.parent / p.stem
    if dest is None:
        return ResultatAction(False, f"Destination invalide : {destination}")
    try:
        with zipfile.ZipFile(p, "r") as zf:
            zf.extractall(dest)
        return ResultatAction(True, f"Dézippé dans : {dest}")
    except Exception as exc:
        return ResultatAction(False, f"Dézip échoué : {exc}")

def ouvrir_dossier(chemin: str) -> ResultatAction:
    p = _safe_path(chemin) if chemin else USER_HOME / "Desktop"
    if p is None:
        return ResultatAction(False, f"Chemin invalide : {chemin}")
    if not p.exists():
        return ResultatAction(False, f"Dossier introuvable : {p}")
    try:
        os.startfile(str(p)) if os.name == "nt" else subprocess.Popen(["xdg-open", str(p)])
        return ResultatAction(True, f"Dossier ouvert : {p}")
    except Exception as exc:
        return ResultatAction(False, f"Impossible d'ouvrir : {exc}")

def chercher_fichier(motif: str, racine: str | None = None) -> ResultatAction:
    root = _safe_path(racine) if racine else USER_HOME
    if root is None:
        return ResultatAction(False, f"Racine invalide : {racine}")
    try:
        found = list(root.rglob(motif))[:20]
        if not found:
            return ResultatAction(False, f"Aucun fichier trouvé : {motif}")
        lignes = [f"🔎 {motif} ({len(found)} résultat(s))"]
        for f in found:
            lignes.append(f"  📄 {f}")
        return ResultatAction(True, "\n".join(lignes), donnees=[str(f) for f in found])
    except Exception as exc:
        return ResultatAction(False, f"Recherche échouée : {exc}")

def taille_fichier(chemin: str) -> ResultatAction:
    p = _safe_path(chemin)
    if p is None:
        return ResultatAction(False, f"Chemin invalide : {chemin}")
    if not p.exists():
        return ResultatAction(False, f"Introuvable : {p}")
    try:
        if p.is_dir():
            total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        else:
            total = p.stat().st_size
        return ResultatAction(True, f"Taille de « {p.name} » : {_fmt_size(total)}")
    except Exception as exc:
        return ResultatAction(False, f"Taille échouée : {exc}")


# ── Process ───────────────────────────────────────────────────────────────────

def lister_apps() -> ResultatAction:
    try:
        import psutil
        windows = []
        for p in psutil.process_iter(["name", "pid"]):
            try:
                if p.info["name"]:
                    windows.append(p.info["name"])
            except Exception:
                pass
        unique = sorted(set(windows))[:30]
        return ResultatAction(True, "Apps : " + ", ".join(unique))
    except ImportError:
        return ResultatAction(False, "psutil non installé.")

def fermer_app(nom: str) -> ResultatAction:
    try:
        import psutil
        killed = []
        for p in psutil.process_iter(["name", "pid"]):
            try:
                if nom.lower() in p.info["name"].lower():
                    p.terminate()
                    killed.append(p.info["name"])
            except Exception:
                pass
        if killed:
            return ResultatAction(True, f"Fermé : {', '.join(killed)}")
        return ResultatAction(False, f"App « {nom} » non trouvée.")
    except ImportError:
        return ResultatAction(False, "psutil non installé.")

def kill_process(nom: str) -> ResultatAction:
    try:
        import psutil
        killed = []
        for p in psutil.process_iter(["name", "pid"]):
            try:
                if nom.lower() in p.info["name"].lower():
                    p.kill()
                    killed.append(p.info["name"])
            except Exception:
                pass
        if killed:
            return ResultatAction(True, f"Tué : {', '.join(killed)}")
        return ResultatAction(False, f"Process « {nom} » non trouvé.")
    except ImportError:
        return ResultatAction(False, "psutil non installé.")

def _top_process(par: str, nb: int = 5) -> ResultatAction:
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["name", "pid", "cpu_percent", "memory_info"]):
            try:
                procs.append(p.info)
            except Exception:
                pass
        if par == "cpu":
            procs.sort(key=lambda x: x.get("cpu_percent") or 0, reverse=True)
            lignes = [f"  {i+1}. {p['name']} — {p.get('cpu_percent', 0):.1f}%" for i, p in enumerate(procs[:nb])]
        else:
            procs.sort(key=lambda x: (x.get("memory_info") or type("", (), {"rss": 0})()).rss, reverse=True)
            lignes = [f"  {i+1}. {p['name']} — {_fmt_size((p.get('memory_info') or type('', (), {'rss': 0})()).rss)}" for i, p in enumerate(procs[:nb])]
        return ResultatAction(True, f"Top {nb} {par.upper()} :\n" + "\n".join(lignes))
    except ImportError:
        return ResultatAction(False, "psutil non installé.")

def top_cpu() -> ResultatAction: return _top_process("cpu")
def top_ram() -> ResultatAction: return _top_process("ram")


# ── Logs ──────────────────────────────────────────────────────────────────────

def afficher_logs(nb_lignes: int = 30) -> ResultatAction:
    if not LOG_FILE.exists():
        return ResultatAction(False, "Fichier log introuvable.")
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        tail  = lines[-nb_lignes:]
        return ResultatAction(True, "\n".join(tail))
    except Exception as exc:
        return ResultatAction(False, f"Logs illisibles : {exc}")


# ── Enregistrement écran (ffmpeg) ─────────────────────────────────────────────

_rec_proc: subprocess.Popen | None = None
_rec_file: Path | None = None


def record_start() -> ResultatAction:
    global _rec_proc, _rec_file
    if _rec_proc and _rec_proc.poll() is None:
        return ResultatAction(False, "Enregistrement déjà en cours.")
    try:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        _rec_file = RECORDINGS_DIR / f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
        _rec_proc = subprocess.Popen(
            [
                FFMPEG_PATH, "-y",
                "-f", "gdigrab", "-framerate", "15",
                "-i", "desktop",
                "-c:v", "libx264", "-preset", "ultrafast",
                str(_rec_file),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return ResultatAction(True, f"Enregistrement démarré : {_rec_file.name}")
    except FileNotFoundError:
        return ResultatAction(False, "ffmpeg introuvable. Installe-le ou vérifie FFMPEG_PATH.")
    except Exception as exc:
        return ResultatAction(False, f"Enregistrement échoué : {exc}")


def record_stop() -> ResultatAction:
    global _rec_proc, _rec_file
    if not _rec_proc or _rec_proc.poll() is not None:
        return ResultatAction(False, "Aucun enregistrement en cours.")
    try:
        _rec_proc.stdin.write(b"q")
        _rec_proc.stdin.flush()
        _rec_proc.wait(timeout=10)
    except Exception:
        _rec_proc.kill()
    size_str = ""
    if _rec_file and _rec_file.exists():
        size_str = f" ({_fmt_size(_rec_file.stat().st_size)})"
    nom = _rec_file.name if _rec_file else "?"
    _rec_proc = None
    _rec_file = None
    return ResultatAction(True, f"Enregistrement arrêté : {nom}{size_str}")


# ── Dispatcher ────────────────────────────────────────────────────────────────

_ACTIONS: dict[str, Any] = {
    "ouvrir_app":       lambda p: ouvrir_app(p.get("nom", "")),
    "ouvrir_fichier":   lambda p: ouvrir_fichier(p.get("chemin", "")),
    "creer_fichier":    lambda p: creer_fichier(p.get("chemin", ""), p.get("contenu", "")),
    "taper_texte":      lambda p: taper_texte(p.get("texte", "")),
    "screenshot":       lambda p: screenshot(p.get("nom")),
    "volume_up":        lambda _: volume_up(),
    "volume_down":      lambda _: volume_down(),
    "volume_mute":      lambda _: volume_mute(),
    "shutdown":         lambda _: shutdown(),
    "restart":          lambda _: restart(),
    "sleep":            lambda _: sleep_pc(),
    "lock":             lambda _: lock_pc(),
    "infos_systeme":    lambda _: infos_systeme(),
    "media_play_pause": lambda _: media_play_pause(),
    "media_next":       lambda _: media_next(),
    "media_prev":       lambda _: media_prev(),
    "media_stop":       lambda _: media_stop(),
    "luminosite_up":    lambda p: luminosite_up(p.get("step", 10)),
    "luminosite_down":  lambda p: luminosite_down(p.get("step", 10)),
    "luminosite_set":   lambda p: luminosite_set(p.get("valeur", 50)),
    "recherche_web":    lambda p: recherche_web(p.get("query", ""), p.get("nb", _MAX_RESULTATS)),
    "ouvrir_url":       lambda p: ouvrir_url(p.get("url", "")),
    "scraper_page":     lambda p: scraper_page(p.get("url", ""), p.get("selecteur")),
    # ── Nouveau v9 ──
    "youtube_jouer":    lambda p: youtube_jouer(p.get("query", "")),
    "youtube_resumer":  lambda p: youtube_resumer(p.get("url", "")),
    "meteo":            lambda p: meteo(p.get("ville", ""), p.get("quand", "aujourd'hui")),
    "envoyer_message":  lambda p: envoyer_message(p.get("contact", ""), p.get("message", ""), p.get("plateforme", "whatsapp")),
    # ── Fichiers ──
    "supprimer_fichier": lambda p: supprimer_fichier(p.get("chemin", "")),
    "renommer_fichier":  lambda p: renommer_fichier(p.get("chemin", ""), p.get("nouveau", "")),
    "copier_fichier":    lambda p: copier_fichier(p.get("source", ""), p.get("destination", "")),
    "deplacer_fichier":  lambda p: deplacer_fichier(p.get("source", ""), p.get("destination", "")),
    "lister_dossier":    lambda p: lister_dossier(p.get("chemin", "")),
    "zipper":            lambda p: zipper(p.get("chemin", "")),
    "dezipper":          lambda p: dezipper(p.get("chemin", ""), p.get("destination")),
    "ouvrir_dossier":    lambda p: ouvrir_dossier(p.get("chemin", "")),
    "chercher_fichier":  lambda p: chercher_fichier(p.get("motif", ""), p.get("racine")),
    "taille_fichier":    lambda p: taille_fichier(p.get("chemin", "")),
    "lister_apps":       lambda _: lister_apps(),
    "fermer_app":        lambda p: fermer_app(p.get("nom", "")),
    "kill_process":      lambda p: kill_process(p.get("nom", "")),
    "top_cpu":           lambda _: top_cpu(),
    "top_ram":           lambda _: top_ram(),
    "afficher_logs":     lambda _: afficher_logs(),
    "record_start":      lambda _: record_start(),
    "record_stop":       lambda _: record_stop(),
    # ── Rappels ──
    "rappel":            lambda p: creer_rappel(p),
    "lister_rappels":    lambda _: lister_rappels(),
    "annuler_rappel":    lambda p: annuler_rappel(p.get("id", "")),
}


def executer_action(action: dict[str, Any]) -> str:
    type_action = action.get("type", "")
    params      = action.get("params", {})
    handler     = _ACTIONS.get(type_action)
    if handler is None:
        return f"Action inconnue : « {type_action} »."
    return str(handler(params))

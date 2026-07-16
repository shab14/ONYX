"""
vision.py — ONYX Vision / OCR v2
Capture l'écran + Tesseract + LLM = "aide moi" magique.
Config partagée via config.py — pas de duplication.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

import requests

from config import (
    MODEL_NAME, OCR_TEXT_MAX_CHARS, OLLAMA_URL,
    SYSTEM_PROMPT_VISION, TESSERACT_PATH, VISION_TIMEOUT,
)

log = logging.getLogger(__name__)


_pytesseract = None
_pyautogui   = None
_PIL_Image   = None


def _lazy_imports() -> bool:
    """Imports tardifs. Évite crash au load si libs manquent."""
    global _pytesseract, _pyautogui, _PIL_Image
    if _pytesseract is not None:
        return True
    try:
        import pytesseract
        import pyautogui
        from PIL import Image
        _pytesseract = pytesseract
        _pyautogui   = pyautogui
        _PIL_Image   = Image
        return True
    except ImportError as exc:
        log.warning("Vision imports manquants : %s", exc)
        return False


def _init_tesseract() -> bool:
    """Configure pytesseract si binaire dispo."""
    if not _lazy_imports():
        return False
    try:
        if os.path.exists(TESSERACT_PATH):
            _pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
        _pytesseract.get_tesseract_version()
        return True
    except Exception as exc:
        log.warning("Tesseract non disponible : %s", exc)
        return False


TESSERACT_OK = _init_tesseract()



def screenshot_texte() -> str:
    """Screenshot + OCR. Retourne texte ou '' si fail."""
    if not TESSERACT_OK:
        return ""
    try:
        img      = _pyautogui.screenshot()
        img_gris = img.convert("L")
        return _pytesseract.image_to_string(img_gris, lang="fra+eng").strip()
    except Exception as exc:
        log.warning("screenshot_texte échoué : %s", exc)
        return ""


def ocr_image(chemin: str) -> str:
    """OCR sur fichier image fourni."""
    if not TESSERACT_OK:
        return ""
    try:
        img = _PIL_Image.open(chemin).convert("L")
        return _pytesseract.image_to_string(img, lang="fra+eng").strip()
    except Exception as exc:
        log.warning("ocr_image échoué : %s", exc)
        return ""



def _appel_llm(messages: list[dict]) -> str:
    try:
        r = requests.post(
            OLLAMA_URL,
            json={"model": MODEL_NAME, "messages": messages, "stream": False},
            timeout=VISION_TIMEOUT,
        )
        r.raise_for_status()
        contenu = r.json()["message"]["content"]
        return re.sub(r"<think>.*?</think>", "", contenu, flags=re.DOTALL).strip()
    except requests.exceptions.ConnectionError:
        return "Ollama hors ligne — lance `ollama serve`."
    except requests.exceptions.Timeout:
        return "Timeout LLM."
    except Exception as exc:
        log.error("LLM vision échoué : %s", exc)
        return f"Erreur LLM : {exc}"


def aide_moi(question_utilisateur: str = "", fichier: str = "") -> str:
    """Screenshot (ou OCR fichier si fourni) → LLM → réponse."""
    if not TESSERACT_OK:
        return (
            "Vision non disponible. Installe Tesseract :\n"
            "winget install UB-Mannheim.TesseractOCR\n"
            f"Puis vérifie : {TESSERACT_PATH}"
        )

    if fichier:
        texte_ecran = ocr_image(fichier)
        if not texte_ecran:
            return f"Pas de texte extrait de {fichier} (image non-texte ou OCR KO)."
    else:
        texte_ecran = screenshot_texte()
        if not texte_ecran:
            return "Écran vide ou OCR a échoué. Vérifie Tesseract."

    if question_utilisateur:
        instruction = (
            f"L'utilisateur te demande : « {question_utilisateur} »\n\n"
            "Réponds directement à sa question en te basant sur ce que tu lis ci-dessous. "
            "Sois concis et pratique."
        )
    else:
        instruction = (
            "L'utilisateur a besoin d'aide mais n'a pas précisé quoi. "
            "Regarde l'écran et décide toi-même : explique erreur, résume texte, "
            "explique code, etc. Direct et actionnable."
        )

    snippet = texte_ecran[:OCR_TEXT_MAX_CHARS]
    trunc   = "\n[… texte tronqué]" if len(texte_ecran) > OCR_TEXT_MAX_CHARS else ""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_VISION},
        {
            "role": "user",
            "content": (
                f"{instruction}\n\n"
                f"--- CONTENU DE L'ÉCRAN ---\n{snippet}{trunc}\n--- FIN ---"
            ),
        },
    ]
    return _appel_llm(messages)



def statut_vision() -> str:
    if TESSERACT_OK:
        return "Vision ✓ (Tesseract OK)"
    return "Vision ✗ (Tesseract manquant)"

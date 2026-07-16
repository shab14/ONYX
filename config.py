"""
config.py — config centralisée ONYX v3
Single source of truth. Tous modules importent d'ici.

v3 :
- Barge-in v3 anti-faux-positif (BARGE_IN_REQUIRE_PRE_SILENCE)
- Wake word custom model path (WAKE_WORD_CUSTOM_MODEL)
"""
from __future__ import annotations

import os
from pathlib import Path

OLLAMA_URL  = "http://localhost:11434/api/chat"
# ⚡ Pour réponses 2-3s : `ollama pull qwen2.5:3b` puis MODEL_NAME = "qwen2.5:3b"
MODEL_NAME  = "deepseek-r1:7b"
LLM_TIMEOUT = 120
PARSE_TIMEOUT = 60
VISION_TIMEOUT = 120

USER_HOME       = Path.home()
SCREENSHOTS_DIR = USER_HOME / "Pictures" / "ONYX"
RECORDINGS_DIR  = USER_HOME / "Videos" / "ONYX"

# Logs dans AppData/Roaming sur Windows, ~/.local/share ailleurs
if os.name == "nt":
    _APPDATA = Path(os.environ.get("APPDATA", USER_HOME / "AppData" / "Roaming"))
    LOG_DIR  = _APPDATA / "ONYX"
else:
    LOG_DIR  = USER_HOME / ".local" / "share" / "onyx"

LOG_FILE = LOG_DIR / "onyx.log"

TESSERACT_PATH  = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# ffmpeg pour l'enregistrement écran (laisse "ffmpeg" si dans le PATH)
FFMPEG_PATH = "ffmpeg"

CONV_MAX_MESSAGES = 30
OCR_TEXT_MAX_CHARS = 3000

SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8000

# ── Voice ─────────────────────────────────────────────────────────────────────
# "base" = ~2x plus rapide que "small" sur CPU (155H, pas de GPU). "small" = + précis.
WHISPER_MODEL    = "base"
VOICE_TTS_RATE   = 175    # mots/min (fallback pyttsx3 seulement)
VOICE_TTS_ENABLED = True  # ONYX lit ses réponses à voix haute en mode vocal
VOICE_NOISE_CALIB_SEC = 1.2  # calibration bruit ambiant au démarrage du vocal

# ── Wake word "Hey ONYX" (offline, openwakeword) ──────────────────────────────
# Requiert : pip install openwakeword onnxruntime
WAKE_WORD_ENABLED = True
# Modèle pré-entraîné. Options : "hey_jarvis", "alexa", "hey_mycroft".
# Pas de "onyx" officiel — pour custom : train via https://github.com/dscripka/openWakeWord
WAKE_WORD_NAME    = "hey_jarvis"
WAKE_WORD_THRESHOLD = 0.5   # 0.0-1.0 ; ↓ = plus sensible, ↑ = moins de faux positifs
# Si tu entraînes un custom "hey_onyx.onnx", mets le chemin ici (sinon vide)
WAKE_WORD_CUSTOM_MODEL = ""  # ex: r"C:\Users\shabd\ONYX\models\hey_onyx.onnx"

# ── Barge-in : couper la voix d'ONYX quand l'utilisateur parle par-dessus ────
BARGE_IN_ENABLED  = True
BARGE_IN_RMS      = 0.045   # énergie mic mini pour interrompre (au-dessus de l'écho TTS)
BARGE_IN_FRAMES   = 4       # frames consécutives au-dessus du seuil avant coupure
# v3 : anti-faux-positif. N'interrompt que si N frames "voix" successives + N "silence" avant.
# 0 = désactivé (comportement v2). 3 = exige burst de voix après période calme.
BARGE_IN_REQUIRE_PRE_SILENCE = 3

# ── Piper TTS (voix naturelle, REQUIS pour vrai barge-in) ────────────────────
# Mets le chemin du .onnx ; vide = fallback pyttsx3 (non interruptible).
# Voix FR : https://huggingface.co/rhasspy/piper-voices (ex: fr_FR-siwis-medium)
PIPER_MODEL_PATH  = ""     # ex: r"C:\Users\shabd\ONYX\voices\fr_FR-siwis-medium.onnx"

SYSTEM_PROMPT = (
    "Tu es ONYX, assistant IA personnel sobre et efficace. "
    "Tu tutoies l'utilisateur. Concis, direct. "
    "Réponds en français, 1-3 phrases max. "
    "Ne génère jamais de JSON. "
    "Si tu n'es pas certain d'une info (date récente, fait précis, chiffre), "
    "commence ta réponse par « [incertain] » et dis-le franchement plutôt que d'inventer. "
    "Si tu ne sais pas, dis « je ne sais pas »."
)

SYSTEM_PROMPT_VISION = (
    "Tu es ONYX, assistant IA personnel. Tu tutoies l'utilisateur. "
    "Tu analyses ce qui est affiché sur l'écran et tu l'aides concrètement. "
    "Réponds en français, concis, max 5-6 lignes sauf si plus nécessaire."
)

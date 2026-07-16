"""
voice_mode.py — ONYX Voice Mode v6
NOUVEAUTÉS v6 :
- Wake word "Hey ONYX" (openwakeword, offline) : écoute passive → WAITING_WAKE,
  déclenche LISTENING sur détection. Toggle via config.WAKE_WORD_ENABLED.
- Barge-in : parler par-dessus ONYX coupe le TTS et relance l'écoute immédiatement.
- Piper TTS (voix naturelle .onnx) avec lecture interruptible par chunks ;
  fallback pyttsx3 (non interruptible) si Piper indisponible.

CONSERVÉ v5 :
- Anti-écho, calibration bruit, filtre hallucination, on_result(text,result,routed),
  start() non-bloquant, _loop protégé try/finally.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

try:
    from config import (
        BARGE_IN_ENABLED, BARGE_IN_FRAMES, BARGE_IN_REQUIRE_PRE_SILENCE, BARGE_IN_RMS,
        PIPER_MODEL_PATH, VOICE_NOISE_CALIB_SEC, VOICE_TTS_ENABLED, VOICE_TTS_RATE,
        WAKE_WORD_CUSTOM_MODEL, WAKE_WORD_ENABLED, WAKE_WORD_NAME, WAKE_WORD_THRESHOLD,
        WHISPER_MODEL,
    )
except ImportError:  # config ancienne version
    (WHISPER_MODEL, VOICE_TTS_RATE, VOICE_TTS_ENABLED, VOICE_NOISE_CALIB_SEC) = ("base", 175, True, 1.2)
    (WAKE_WORD_ENABLED, WAKE_WORD_NAME, WAKE_WORD_THRESHOLD, WAKE_WORD_CUSTOM_MODEL) = (False, "hey_jarvis", 0.5, "")
    (BARGE_IN_ENABLED, BARGE_IN_RMS, BARGE_IN_FRAMES, PIPER_MODEL_PATH) = (True, 0.045, 4, "")
    BARGE_IN_REQUIRE_PRE_SILENCE = 3

log = logging.getLogger(__name__)

_whisper     = None
_sounddevice = None

VOICE_AVAILABLE  = False
VOICE_ERROR: str = ""

SAMPLE_RATE = 16_000
CHANNELS    = 1
BLOCK_SIZE  = 1024
DTYPE_IN    = "float32"

_MODEL_DIR = Path.home() / ".cache" / "onyx" / "whisper"
_load_lock = threading.Lock()
_load_done = False


def _load_deps() -> bool:
    global _whisper, _sounddevice, VOICE_AVAILABLE, VOICE_ERROR, _load_done
    if _load_done:
        return VOICE_AVAILABLE
    with _load_lock:
        if _load_done:
            return VOICE_AVAILABLE
        missing = []
        try:
            import whisper
            _whisper = whisper
        except Exception as exc:
            missing.append(f"openai-whisper ({type(exc).__name__})")
        try:
            import sounddevice as sd
            _sounddevice = sd
        except Exception as exc:
            missing.append(f"sounddevice ({type(exc).__name__}: {exc})")
        if missing:
            VOICE_ERROR = (
                "Vocal indisponible : " + ", ".join(missing)
                + "\npip install openai-whisper sounddevice numpy pyttsx3"
            )
            log.warning("[Voice] %s", VOICE_ERROR)
            VOICE_AVAILABLE = False
        else:
            VOICE_AVAILABLE = True
        _load_done = True
    return VOICE_AVAILABLE


_whisper_model = None
_model_lock    = threading.Lock()


def _detect_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass
    return "cpu"


def _get_model():
    global _whisper_model
    with _model_lock:
        if _whisper_model is None:
            log.info("[Voice] Chargement Whisper %s…", WHISPER_MODEL)
            device = _detect_device()
            _MODEL_DIR.mkdir(parents=True, exist_ok=True)
            _whisper_model = _whisper.load_model(
                WHISPER_MODEL, device=device, download_root=str(_MODEL_DIR)
            )
            log.info("[Voice] Whisper %s prêt (%s)", WHISPER_MODEL, device)
    return _whisper_model


# ── TTS interruptible ─────────────────────────────────────────────────────────
class _TTSPlayer:
    """
    Lecture TTS interruptible.
    - Piper (.onnx) si dispo : synthèse → numpy → playback sounddevice par chunks,
      chaque chunk vérifie le stop_event → barge-in instantané.
    - Fallback pyttsx3 : lecture bloquante non interruptible (barge-in inactif).
    """
    def __init__(self) -> None:
        self._piper = None
        self._piper_tried = False

    def _try_piper(self):
        if self._piper_tried:
            return self._piper
        self._piper_tried = True
        if not PIPER_MODEL_PATH or not Path(PIPER_MODEL_PATH).exists():
            return None
        try:
            from piper import PiperVoice
            self._piper = PiperVoice.load(PIPER_MODEL_PATH)
            log.info("[Voice] Piper TTS chargé : %s", PIPER_MODEL_PATH)
        except Exception as exc:
            log.warning("[Voice] Piper indisponible (%s) → pyttsx3", exc)
            self._piper = None
        return self._piper

    def speak(self, text: str, stop_event: threading.Event) -> None:
        if not text.strip():
            return
        voice = self._try_piper()
        if voice is not None and _sounddevice is not None:
            self._speak_piper(voice, text, stop_event)
        else:
            self._speak_pyttsx3(text)

    def _speak_piper(self, voice, text: str, stop_event: threading.Event) -> None:
        try:
            sr = voice.config.sample_rate
            for chunk in voice.synthesize_stream_raw(text):
                if stop_event.is_set():
                    _sounddevice.stop()
                    return
                audio = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                _sounddevice.play(audio, sr)
                # attend la fin du chunk en vérifiant le stop régulièrement
                while _sounddevice.get_stream().active:
                    if stop_event.is_set():
                        _sounddevice.stop()
                        return
                    time.sleep(0.02)
        except Exception as exc:
            log.warning("[Voice] Piper playback : %s → pyttsx3", exc)
            self._speak_pyttsx3(text)

    @staticmethod
    def _speak_pyttsx3(text: str) -> None:
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", VOICE_TTS_RATE)
            try:
                for v in engine.getProperty("voices"):
                    if "fr" in (v.id or "").lower() or "french" in (v.name or "").lower():
                        engine.setProperty("voice", v.id)
                        break
            except Exception:
                pass
            engine.say(text)
            engine.runAndWait()
            try:
                engine.stop()
            except Exception:
                pass
        except Exception as exc:
            log.warning("[Voice] TTS pyttsx3 : %s", exc)


# ── Wake word (openwakeword) ──────────────────────────────────────────────────
class _WakeWord:
    """Détecteur wake word offline. No-op silencieux si openwakeword absent."""
    def __init__(self) -> None:
        self._model = None
        self._ok    = False
        # Priorité : modèle custom (.onnx) si fourni, sinon nom pré-entraîné
        custom = (WAKE_WORD_CUSTOM_MODEL or "").strip()
        try:
            from openwakeword.model import Model
            try:
                from openwakeword.utils import download_models
                download_models()
            except Exception:
                pass
            if custom and Path(custom).exists():
                self._model = Model(wakeword_models=[custom])
                log.info("[Voice] Wake word custom prêt : %s", custom)
            else:
                self._model = Model(wakeword_models=[WAKE_WORD_NAME])
                log.info("[Voice] Wake word « %s » prêt", WAKE_WORD_NAME)
            self._ok = True
        except Exception as exc:
            log.warning("[Voice] Wake word indisponible (%s) → activation manuelle", exc)

    @property
    def available(self) -> bool:
        return self._ok

    def detected(self, chunk_f32: np.ndarray) -> bool:
        if not self._ok:
            return False
        try:
            pcm16 = (np.clip(chunk_f32, -1, 1) * 32767).astype(np.int16)
            scores = self._model.predict(pcm16)
            return any(s >= WAKE_WORD_THRESHOLD for s in scores.values())
        except Exception:
            return False

    def reset(self) -> None:
        if self._ok:
            try:
                self._model.reset()
            except Exception:
                pass


# ── Anti-hallucination Whisper ────────────────────────────────────────────────
_HALLUCINATIONS = frozenset({
    "merci", "merci.", "merci d'avoir regardé", "merci d'avoir regardé cette vidéo",
    "sous-titres réalisés para la communauté d'amara.org",
    "sous-titres réalisés par la communauté d'amara.org",
    "sous-titrage st' 501", "sous-titrage société radio-canada",
    "...", "!", "♪", "♪♪",
})


def _is_hallucination(text: str) -> bool:
    t = text.strip().lower().strip(".!?… ")
    return (not t) or t in _HALLUCINATIONS or t.startswith("sous-titr")


def _transcribe(audio: np.ndarray) -> str:
    model  = _get_model()
    result = model.transcribe(
        audio, language="fr", fp16=False,
        beam_size=1, best_of=1, condition_on_previous_text=False,
    )
    text = result.get("text", "").strip()
    if _is_hallucination(text):
        log.info("[Voice] Hallucination filtrée : %r", text)
        return ""
    log.info("[Voice] Transcrit : %r", text)
    return text


class _VADBuffer:
    """VAD double-seuil avec hystérésis. Seuils ajustables (calibration bruit)."""
    def __init__(
        self, sample_rate=SAMPLE_RATE, silence_sec=0.9,
        speech_thresh=0.008, silence_thresh=0.004,
        min_speech_sec=0.3, max_speech_sec=30.0,
    ) -> None:
        self._sr         = sample_rate
        self._sil_n      = int(silence_sec * sample_rate)
        self._speech_thr = speech_thresh
        self._sil_thr    = silence_thresh
        self._min_n      = int(min_speech_sec * sample_rate)
        self._max_n      = int(max_speech_sec * sample_rate)
        self._buf: list[np.ndarray] = []
        self._in_spch = False
        self._sil_cnt = 0

    def set_thresholds(self, speech: float, silence: float) -> None:
        self._speech_thr, self._sil_thr = speech, silence

    def process(self, chunk: np.ndarray) -> Optional[np.ndarray]:
        rms     = float(np.sqrt(np.mean(chunk ** 2)))
        total_n = sum(len(c) for c in self._buf)
        if rms > self._speech_thr:
            self._in_spch = True
            self._sil_cnt = 0
            self._buf.append(chunk.copy())
        elif self._in_spch:
            self._buf.append(chunk.copy())
            if rms < self._sil_thr:
                self._sil_cnt += len(chunk)
            if self._sil_cnt >= self._sil_n or total_n >= self._max_n:
                audio         = np.concatenate(self._buf)
                self._buf     = []
                self._in_spch = False
                self._sil_cnt = 0
                if len(audio) >= self._min_n:
                    return audio
        return None

    def reset(self) -> None:
        self._buf, self._in_spch, self._sil_cnt = [], False, 0


class VoiceMode:
    """
    Cycle vocal ONYX v6.
    États : WAITING_WAKE | LISTENING | THINKING | SPEAKING | MUTED | OFF
    on_result(transcript, result, routed) — routed=True si géré par le router.
    """

    def __init__(
        self,
        on_transcript:   Callable[[str], None],
        on_result:       Callable[[str, str, bool], None],
        on_status:       Callable[[str], None],
        router_fn:       Callable[[str], Optional[str]],
        chat_fn:         Callable[[str], str],
        on_state_change: Optional[Callable[[str], None]] = None,
        tts_enabled:     bool = VOICE_TTS_ENABLED,
        wake_word:       bool = WAKE_WORD_ENABLED,
    ) -> None:
        self._on_transcript = on_transcript
        self._on_result     = on_result
        self._on_status     = on_status
        self._on_state      = on_state_change
        self._router        = router_fn
        self._chat          = chat_fn
        self._tts_on        = tts_enabled
        self._wake_on       = wake_word
        self._active        = False
        self._muted         = False
        self._busy          = False
        self._stop_event    = threading.Event()
        self._tts_stop      = threading.Event()  # signal barge-in
        self._tts           = _TTSPlayer()
        self._thread: Optional[threading.Thread] = None

    def _set_state(self, state: str) -> None:
        if self._on_state:
            self._on_state(state)

    def is_available(self) -> tuple[bool, str]:
        return _load_deps(), VOICE_ERROR

    def is_active(self) -> bool:
        return self._active

    def mute(self) -> None:
        self._muted = True
        self._tts_stop.set()  # coupe TTS en cours
        self._set_state("MUTED")
        self._on_status("🔇 muté")

    def unmute(self) -> None:
        self._muted = False
        self._set_state("LISTENING")
        self._on_status("🎙 écoute…")

    def toggle_mute(self) -> None:
        self.unmute() if self._muted else self.mute()

    def start(self) -> bool:
        """Non-bloquant : imports lourds (whisper, oww) dans le thread vocal."""
        if self._active:
            return False
        self._stop_event.clear()
        self._tts_stop.clear()
        self._muted = self._busy = False
        self._active = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._active = False
        self._stop_event.set()
        self._tts_stop.set()
        self._set_state("OFF")
        self._on_status("vocal off")

    # ── helpers audio ─────────────────────────────────────────────────────────

    @staticmethod
    def _calibrate(q: "queue.Queue", vad: _VADBuffer, seconds: float) -> None:
        deadline, samples = time.time() + seconds, []
        while time.time() < deadline:
            try:
                chunk = q.get(timeout=0.2)
                samples.append(float(np.sqrt(np.mean(chunk.flatten() ** 2))))
            except queue.Empty:
                pass
        if not samples:
            return
        noise   = float(np.median(samples))
        speech  = max(0.008, noise * 3.5)
        silence = max(0.004, noise * 1.8)
        vad.set_thresholds(speech, silence)
        log.info("[Voice] Calibration : RMS=%.4f speech=%.4f silence=%.4f", noise, speech, silence)

    @staticmethod
    def _flush(q: "queue.Queue") -> None:
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass

    # ── TTS dans un thread + surveillance barge-in ────────────────────────────

    def _speak_with_bargein(self, text: str, q: "queue.Queue") -> bool:
        """
        Parle. Surveille le mic ; si l'utilisateur parle → coupe. Retourne True si interrompu.

        v3 anti-faux-positif : exige BARGE_IN_REQUIRE_PRE_SILENCE frames de silence
        AVANT le burst de voix. Évite que les "pops" / clics / écho TTS déclenchent
        un barge-in fantôme.
        """
        if not self._tts_on or not text.strip() or not self._active:
            return False
        self._tts_stop.clear()
        self._set_state("SPEAKING")
        self._on_status("🔊 ONYX parle…")

        th = threading.Thread(target=self._tts.speak, args=(text, self._tts_stop), daemon=True)
        th.start()

        interrupted = False
        loud = 0          # frames consécutives "voix"
        quiet = 0         # frames consécutives "silence" récentes
        pre_silence_seen = (BARGE_IN_REQUIRE_PRE_SILENCE <= 0)
        # Seuil "silence" plus bas que "voix" → hystérésis
        quiet_thr = max(0.005, BARGE_IN_RMS * 0.4)

        while th.is_alive():
            if self._stop_event.is_set() or self._muted:
                self._tts_stop.set()
                break
            if BARGE_IN_ENABLED:
                try:
                    chunk = q.get(timeout=0.05)
                    rms = float(np.sqrt(np.mean(chunk.flatten() ** 2)))
                    if rms > BARGE_IN_RMS:
                        loud += 1
                        # Ne déclenche QUE si on a déjà vu une période calme avant
                        if pre_silence_seen and loud >= BARGE_IN_FRAMES:
                            self._tts_stop.set()
                            interrupted = True
                            log.info("[Voice] Barge-in (loud=%d, rms=%.3f) → coupe TTS", loud, rms)
                            break
                    else:
                        loud = 0
                        if rms < quiet_thr:
                            quiet += 1
                            if quiet >= BARGE_IN_REQUIRE_PRE_SILENCE:
                                pre_silence_seen = True
                except queue.Empty:
                    pass
            else:
                time.sleep(0.05)
        th.join(timeout=1.0)
        return interrupted

    # ── boucle principale ─────────────────────────────────────────────────────

    def _loop(self) -> None:
        try:
            self._loop_inner()
        except Exception as exc:
            log.exception("[Voice] Crash boucle vocale")
            self._on_status(f"❌ {exc}")
        finally:
            self._active = False
            self._set_state("OFF")

    def _loop_inner(self) -> None:
        self._on_status("⟳ chargement des libs vocales…")
        self._set_state("THINKING")
        if not _load_deps():
            self._on_status(f"❌ {VOICE_ERROR}")
            return

        self._on_status("⟳ chargement modèle…")
        try:
            _get_model()
        except Exception as exc:
            self._on_status(f"❌ modèle : {exc}")
            return

        wake = _WakeWord() if self._wake_on else None
        use_wake = bool(wake and wake.available)

        vad = _VADBuffer()
        q: queue.Queue = queue.Queue(maxsize=400)

        def _callback(indata, frames, time_info, status):
            # En SPEAKING on LAISSE passer (barge-in lit la queue). Sinon anti-écho via _busy.
            if self._stop_event.is_set() or self._muted:
                return
            try:
                q.put_nowait(indata.copy())
            except queue.Full:
                pass

        try:
            with _sounddevice.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE_IN,
                blocksize=BLOCK_SIZE, callback=_callback,
            ):
                self._on_status("⟳ calibration bruit…")
                self._calibrate(q, vad, VOICE_NOISE_CALIB_SEC)

                if use_wake:
                    self._set_state("WAITING_WAKE")
                    self._on_status('💤 dis « Hey ONYX »')
                else:
                    self._set_state("LISTENING")
                    self._on_status("🎙 vocal actif — parle")

                awake = not use_wake  # si pas de wake word → toujours éveillé

                while self._active and not self._stop_event.is_set():
                    try:
                        chunk = q.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    flat = chunk.flatten()

                    # ── Phase wake word ──
                    if use_wake and not awake:
                        if self._busy:
                            continue
                        if wake.detected(flat):
                            awake = True
                            wake.reset()
                            vad.reset()
                            self._flush(q)
                            self._set_state("LISTENING")
                            self._on_status("🎙 oui ? parle")
                        continue

                    # ── Phase écoute (VAD) ──
                    if self._busy:
                        continue
                    audio = vad.process(flat)
                    if audio is None:
                        continue

                    self._busy = True
                    self._set_state("THINKING")
                    self._on_status("⟳ transcription…")
                    try:
                        text = _transcribe(audio)
                    except Exception as exc:
                        log.error("[Voice] Transcription : %s", exc)
                        text = ""

                    if text:
                        self._on_transcript(text)
                        self._on_status("⟳ traitement…")
                        result = self._router(text)
                        routed = result is not None
                        if not routed:
                            result = self._chat(text)
                        self._on_result(text, result, routed)
                        # Parle (interruptible). On vide la queue AVANT pour ignorer l'écho déjà capté.
                        self._flush(q)
                        self._speak_with_bargein(result, q)

                    self._flush(q)
                    vad.reset()
                    self._busy = False

                    # Retour : wake mode → rendormir ; sinon → écoute
                    if not self._active:
                        break
                    if use_wake:
                        awake = False
                        wake.reset()
                        self._set_state("WAITING_WAKE")
                        self._on_status('💤 dis « Hey ONYX »')
                    elif not self._muted:
                        self._set_state("LISTENING")
                        self._on_status("🎙 parle")
        except Exception as exc:
            log.error("[Voice] Mic erreur : %s", exc)
            self._on_status(f"❌ Mic : {exc}")

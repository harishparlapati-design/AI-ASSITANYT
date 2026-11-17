"""Voice system: STT, TTS, language detection, wake word.

Language policy (mixed Telugu + Hindi + English):
  - Automatic script detection per message (Telugu తెలుగు, Hindi देवनागरी,
    Latin = English or romanized Telugu/Hindi).
  - Romanized Telugu/Hindi are detected with a curated keyword lexicon; the
    reply style mirrors the user's mix. No language selection is ever forced.
  - TTS: per-language voices are used when installed on the OS (e.g. Microsoft
    Heera for Hindi-in). Telugu TTS voices are rarely installed on Windows;
    in that case Telugu text is spoken with the default voice and the full
    text is always shown as text (graceful fallback, never an error).

Offline-first: Vosk STT + pyttsx3 TTS work fully offline.
"""

import difflib
import json
import logging
import os
import platform
import re
import threading

from . import config, events, runtime_paths

log = logging.getLogger("harry.speech")

IS_WINDOWS = platform.system() == "Windows"

# --------------------------------------------------------------------- language detection

_TELUGU_RANGE = re.compile(r"[\u0C00-\u0C7F]")
_DEVANAGARI_RANGE = re.compile(r"[\u0900-\u097F]")

# Romanized Telugu common words (transliterated)
_ROMAN_TELUGU = {
    "na", "mana", "mee", "cheyyi", "cheyyi", "chudu", "chudandi", "cheppu",
    "ela", "enduku", "eppudu", "ekkada", "enti", "emit", "kavali", "kavala",
    "ledu", "undi", "unnayi", "istu", "chesi", "chesi", "pettu", "pettandi",
    "openchesi", "chesko", "techi", "techuko", "kurchoko", "velli", "ra",
    "raa", "andi", "antanu", "antunna", "ayite", "kani", "tarvata", "ippudu",
    "malli", "inko", "oka", "rendu", "mudu", "nalugu", "aidu", "sare",
    "halu", "vanakaaram", "raatri", "telugu", "bhasha", "maatladu",
    "portfolio", "project", "error", "checkcheyyi", "konchem", "baaga",
    "fastga", " slowga", "aipoindi", "ayipoyindi", "startcheyyi",
}
_ROMAN_HINDI = {
    "kya", "kaise", "kyun", "kyu", "kahan", "kab", "kaun", "mera", "meri",
    "mere", "tumhara", "aapka", "hai", "hain", "nahi", "nahin", "haan",
    "kar", "karo", "karna", "karo", "batao", "bata", "dikhao", "sunao",
    "chalu", "band", "kholo", "khol", "bandkaro", "acha", "accha", "thik",
    "theek", "jaldi", "abhi", "baad", "phir", "bhi", "aur", "mein", "se",
    "ko", "ki", "ka", "hindi", "mujhe", "aap", "tum", "hum", "yaar",
}


def detect_language(text):
    """Return dict {telugu, hindi, english, roman_telugu, roman_hindi} ratios."""
    if not text:
        return {"primary": "english", "mix": {"english": 1.0}}
    telugu_chars = len(_TELUGU_RANGE.findall(text))
    hindi_chars = len(_DEVANAGARI_RANGE.findall(text))
    words = re.findall(r"[A-Za-z']+", text.lower())
    roman_te = sum(1 for w in words if w in _ROMAN_TELUGU)
    roman_hi = sum(1 for w in words if w in _ROMAN_HINDI and w not in _ROMAN_TELUGU)
    latin_total = max(len(words), 1)

    telugu_score = telugu_chars / max(len(text), 1) + (roman_te / latin_total) * 0.5
    hindi_score = hindi_chars / max(len(text), 1) + (roman_hi / latin_total) * 0.5
    english_score = latin_total / max(latin_total, 1) * (1 - min(
        (roman_te + roman_hi) / max(latin_total, 1), 1.0))

    mix = {"telugu": round(telugu_score, 2), "hindi": round(hindi_score, 2),
           "english": round(max(english_score, 0.0), 2)}
    primary = max(mix, key=mix.get)
    return {"primary": primary, "mix": mix,
            "has_telugu_script": telugu_chars > 0,
            "has_hindi_script": hindi_chars > 0}


def style_reply_hint(user_text):
    """System-prompt hint so replies mirror the user's language mix."""
    det = detect_language(user_text)
    mix = det["mix"]
    if det["has_telugu_script"] and det["has_hindi_script"]:
        return "Reply in the same Telugu-Hindi-English mixed style the user used."
    if det["primary"] == "telugu" and mix["telugu"] > 0.25 and mix["english"] > 0.1:
        return ("The user mixes Telugu (possibly romanized) with English. Reply in "
                "the same natural Telugu-English mix, keeping technical terms in English.")
    if det["primary"] == "hindi" and mix["hindi"] > 0.25 and mix["english"] > 0.1:
        return ("The user mixes Hindi (possibly romanized) with English. Reply in "
                "the same natural Hindi-English mix, keeping technical terms in English.")
    if det["primary"] == "telugu":
        return "Reply in Telugu (same script style as the user)."
    if det["primary"] == "hindi":
        return "Reply in Hindi (same script style as the user)."
    return "Reply in English."


# --------------------------------------------------------------------- TTS

class Speaker:
    """pyttsx3-based speech with per-language voice selection and interruption."""

    def __init__(self):
        self.enabled = config.CONFIG.get("voice", {}).get("enabled", True)
        self._engine = None
        self._lock = threading.Lock()
        self._voices = {}
        self._speaking = threading.Event()

    def _init_engine(self):
        if self._engine is not None:
            return self._engine
        try:
            import pyttsx3
            eng = pyttsx3.init()
            eng.setProperty("rate", int(config.CONFIG.get("voice", {}).get("tts_rate", 175)))
            self._index_voices(eng)
            self._engine = eng
            return eng
        except Exception as e:
            log.warning("TTS unavailable: %s", e)
            self.enabled = False
            return None

    def _index_voices(self, engine):
        self._voices = {}
        try:
            for v in engine.getProperty("voices"):
                langs = [str(l).lower() for l in (getattr(v, "languages", None) or [])]
                name = str(v.name).lower()
                vid = v.id
                if "te" in langs or "te-in" in langs or "telugu" in name:
                    self._voices["telugu"] = vid
                if any(l.startswith("hi") for l in langs) or "hindi" in name:
                    self._voices["hindi"] = vid
                if any(l.startswith("en") for l in langs):
                    self._voices.setdefault("english", vid)
        except Exception as e:
            log.debug("voice indexing failed: %s", e)

    def available_voices(self):
        return dict(self._voices)

    def speak(self, text, output_mode=None):
        """Speak text unless mode is text-only. Blocks while speaking unless
        an emergency stop fires."""
        mode = output_mode or config.CONFIG.get("voice", {}).get("output_mode", "both")
        if mode == "text" or not self.enabled:
            return
        if not IS_WINDOWS and not self._engine:
            pass  # still try; pyttsx3 works on other OSes too via espeak
        eng = self._init_engine()
        if not eng:
            return
        det = detect_language(text)
        with self._lock:
            try:
                self._speaking.set()
                # pick best voice for the dominant language; fallback silently
                voice_id = self._voices.get(det["primary"]) or self._voices.get("english")
                if voice_id:
                    try:
                        eng.setProperty("voice", voice_id)
                    except Exception:
                        pass
                eng.say(str(text))
                # runAndWait in small pieces so stop can interrupt reasonably fast
                eng.runAndWait()
            except Exception as e:
                log.warning("speak failed: %s", e)
            finally:
                self._speaking.clear()

    def stop_speaking(self):
        try:
            if self._engine:
                self._engine.stop()
        except Exception:
            pass


_speaker = None


def get_speaker():
    global _speaker
    if _speaker is None:
        _speaker = Speaker()
    return _speaker


# --------------------------------------------------------------------- STT (Vosk)

class Listener:
    """Vosk-based offline speech recognition with VAD-style silence handling."""

    def __init__(self):
        cfg = config.CONFIG.get("voice", {})
        model = cfg.get("stt_model", "vosk-model-en-us-0.22") or "vosk-model-en-us-0.22"
        from . import runtime_paths
        self.model_path = runtime_paths.find_asset(model) if not os.path.isabs(model) else model
        self._model = None
        self._available = None

    def available(self):
        if self._available is None:
            try:
                import vosk  # noqa
                self._available = os.path.isdir(self.model_path)
            except Exception:
                self._available = False
        return self._available

    def _get_model(self):
        if self._model is None:
            from vosk import Model
            self._model = Model(self.model_path)
        return self._model

    def listen_once(self, samplerate=16000, silence_blocks=4, max_seconds=20):
        """Record until speech starts then ends; return recognized text ('' if none)."""
        if not self.available():
            return ""
        try:
            import numpy as np
            import sounddevice as sd
            from vosk import KaldiRecognizer
            rec = KaldiRecognizer(self._get_model(), samplerate)
            speech_started = False
            silence = 0
            started_at = None
            with sd.RawInputStream(samplerate=samplerate, blocksize=8000,
                                   dtype="int16", channels=1) as stream:
                while True:
                    data, overflowed = stream.read(4000)
                    if overflowed:
                        continue
                    data = bytes(data)
                    audio = np.frombuffer(data, dtype=np.int16)
                    volume = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
                    if events.is_stopped():
                        return ""
                    if volume < 500:
                        if not speech_started:
                            continue
                        silence += 1
                        if silence >= silence_blocks:
                            break
                    else:
                        if not speech_started:
                            speech_started = True
                            started_at = __import__("time").time()
                        silence = 0
                    if speech_started:
                        rec.AcceptWaveform(data)
                        if started_at and __import__("time").time() - started_at > max_seconds:
                            break
            result = json.loads(rec.FinalResult())
            text = (result.get("text") or "").strip()
            return " ".join(text.split())
        except Exception as e:
            log.warning("listen failed: %s", e)
            return ""


    def transcribe_pcm(self, pcm_bytes, samplerate=16000):
        """Recognize raw mono PCM16 audio (browser fallback path).

        Offline: uses the same local Vosk model as microphone mode. Returns
        '' when the model or audio is unusable - callers must handle that as
        "did not understand" rather than inventing text.
        """
        if not pcm_bytes or not self.available():
            return ""
        try:
            from vosk import KaldiRecognizer
            rec = KaldiRecognizer(self._get_model(), samplerate)
            rec.AcceptWaveform(bytes(pcm_bytes))
            result = json.loads(rec.FinalResult())
            return " ".join((result.get("text") or "").split())
        except Exception as e:
            log.warning("transcribe_pcm failed: %s", e)
            return ""


_listener = None


def get_listener():
    global _listener
    if _listener is None:
        _listener = Listener()
    return _listener


# --------------------------------------------------------------------- wake word

def wait_for_wake_word(timeout=None):
    """Return True when the wake word fires.

    Modes:
      porcupine - real Picovoice keyword detection (needs free access key
                  in the env var named by config; uses the existing .ppn)
      text      - STT-based: listens and checks similarity to the wake word
      off       - always True (push-to-talk / UI driven)
    """
    mode = config.CONFIG.get("voice", {}).get("wake_word_mode", "text")
    word = config.CONFIG.get("wake_word", "harry")

    if mode == "off":
        return True

    if mode == "porcupine":
        try:
            cfg = config.CONFIG.get("voice", {})
            import os
            key = os.environ.get(cfg.get("porcupine_access_key_env", ""), "")
            if not key:
                log.warning("porcupine mode set but no access key env; falling back to text mode")
            else:
                import pvporcupine
                import numpy as np
                import sounddevice as sd
                keyword_path = runtime_paths.find_asset(cfg.get("keyword_path", "")) or cfg.get("keyword_path", "")
                porcupine = pvporcupine.create(
                    access_key=key,
                    keyword_paths=[keyword_path] if keyword_path else None,
                    keywords=[word] if not keyword_path else None,
                )
                frame = porcupine.frame_length
                with sd.RawInputStream(samplerate=porcupine.sample_rate,
                                       blocksize=frame, dtype="int16",
                                       channels=1) as stream:
                    while not events.is_stopped():
                        data, _ = stream.read(frame)
                        if porcupine.process(bytes(data)) == 0:
                            porcupine.delete()
                            return True
        except Exception as e:
            log.warning("porcupine failed (%s); falling back to text mode", e)

    # text mode: use STT and fuzzy-match the wake word
    text = get_listener().listen_once(max_seconds=6)
    if not text:
        return False
    for w in text.lower().split():
        if difflib.SequenceMatcher(None, w, word).ratio() > 0.6:
            return True
    return False


# --------------------------------------------------------------------- voice auth (preserved)

def verify_voice(threshold=None):
    """Voiceprint verification using the existing trained Resemblyzer profile."""
    cfg = config.CONFIG.get("voice", {})
    profile_path = runtime_paths.find_asset(cfg.get("voice_profile_path", "")) or cfg.get("voice_profile_path", "")
    if not os.path.exists(profile_path):
        log.info("no voice profile; skipping verification")
        return True
    try:
        import pickle
        import numpy as np
        import sounddevice as sd
        from resemblyzer import VoiceEncoder, preprocess_wav
        with open(profile_path, "rb") as f:
            voice_profile = pickle.load(f)
        encoder = VoiceEncoder()
        fs, seconds = 16000, 2
        scores = []
        for _ in range(3):
            recording = sd.rec(int(seconds * fs), samplerate=fs, channels=1, dtype="float32")
            sd.wait()
            audio = recording.flatten()
            if float(np.sqrt(np.mean(audio ** 2))) < 0.005:
                continue
            embedding = encoder.embed_utterance(preprocess_wav(audio))
            scores.append(float(np.dot(embedding, voice_profile)))
        best = max(scores) if scores else 0.0
        thresh = threshold or float(cfg.get("verify_threshold", 0.43))
        return best >= thresh
    except Exception as e:
        log.warning("voice verification failed (%s); allowing", e)
        return True

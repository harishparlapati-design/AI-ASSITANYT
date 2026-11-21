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
import re
import sys
import threading
import time

from . import config, events, runtime_paths

log = logging.getLogger("harry.speech")

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
#
# Voice choice is made from installed-voice METADATA (language + gender), never
# from a word in the voice name. pyttsx3 exposes ``gender`` for SAPI5 voices on
# most installs; the Windows registry token attributes are read as a second,
# authoritative source because that attribute is empty on some installs.

VOICE_GENDERS = ("male", "female")
_NATURAL_HINTS = ("natural", "neural", "online", "premium")


def _voice_cfg():
    return config.CONFIG.get("voice", {}) or {}


def _winreg_gender(voice_id):
    """Return 'male'/'female' from a SAPI token's registry attributes, else ''."""
    if not voice_id or not sys.platform.startswith("win"):
        return ""
    try:
        import winreg
    except Exception:
        return ""
    path = str(voice_id)
    hive = None
    for prefix, handle in (("HKEY_LOCAL_MACHINE\\", winreg.HKEY_LOCAL_MACHINE),
                           ("HKEY_CURRENT_USER\\", winreg.HKEY_CURRENT_USER),
                           ("HKEY_CLASSES_ROOT\\", winreg.HKEY_CLASSES_ROOT)):
        if path.upper().startswith(prefix):
            hive, path = handle, path[len(prefix):]
            break
    if hive is None:
        return ""
    for sub in (r"\Attributes", ""):
        try:
            with winreg.OpenKey(hive, path + sub) as key:
                value, _kind = winreg.QueryValueEx(key, "Gender")
        except OSError:
            continue
        text = str(value).strip().lower()
        if "female" in text:
            return "female"
        if "male" in text:
            return "male"
    return ""


def _voice_language(languages):
    """Map a voice's locale codes to one of Harry's languages ('' if none)."""
    for code in languages:
        code = str(code).lower()
        if code.startswith("te"):
            return "telugu"
        if code.startswith("hi"):
            return "hindi"
        if code.startswith("en"):
            return "english"
    return ""


def describe_voices(engine):
    """Installed voices plus the metadata used to select one."""
    catalog = []
    try:
        voices = engine.getProperty("voices") or []
    except Exception as e:
        log.debug("could not list TTS voices: %s", e)
        return catalog
    for v in voices:
        name = str(getattr(v, "name", "") or "")
        languages = [str(x).lower() for x in (getattr(v, "languages", None) or [])]
        gender = str(getattr(v, "gender", "") or "").strip().lower()
        if gender not in VOICE_GENDERS:
            gender = _winreg_gender(getattr(v, "id", "")) or gender
        catalog.append({
            "name": name,
            "id": getattr(v, "id", ""),
            "gender": gender,
            "languages": languages,
            "language": _voice_language(languages),
            "natural": any(h in name.lower() for h in _NATURAL_HINTS),
        })
    return catalog


def select_voice(catalog, gender="male", name="", language="english"):
    """Pick the best installed voice, or None to keep the engine default.

    Order: explicitly configured name -> same language with the requested
    gender (natural/premium first) -> requested gender in any language ->
    same language with any gender. A missing preferred gender never raises:
    it falls back and logs what was used instead.
    """
    if not catalog:
        return None
    wanted = str(name or "").strip().lower()
    if wanted:
        for v in catalog:
            if v["name"].lower() == wanted or wanted in v["name"].lower():
                return v
        log.warning("configured TTS voice %r is not installed; using metadata default",
                    name)
    wanted_gender = str(gender or "").strip().lower()
    same_lang = [v for v in catalog if v["language"] == language] or \
        [v for v in catalog if v["language"] == "english"] or list(catalog)
    if wanted_gender in VOICE_GENDERS:
        pool = [v for v in same_lang if v["gender"] == wanted_gender]
        if pool:
            return sorted(pool, key=lambda v: (v["natural"], v["language"] == language),
                          reverse=True)[0]
        # A voice that speaks the right language beats a right-gender voice in
        # the wrong language (a Hindi answer in an English voice is worse).
        if language != "english":
            log.info("no %s %s voice installed; using %s", wanted_gender, language,
                     same_lang[0]["name"])
            return same_lang[0]
        pool = [v for v in catalog if v["gender"] == wanted_gender]
        if pool:
            log.info("no %s %s voice installed; using %s", wanted_gender, language,
                     pool[0]["name"])
            return pool[0]
        log.warning("no %s voice installed; falling back to %s", wanted_gender,
                    same_lang[0]["name"])
    return sorted(same_lang, key=lambda v: (v["natural"], v["language"] == language),
                  reverse=True)[0]


# ------------------------------------------------------------- speech text prep
# The answer is still shown in full; only the spoken version is normalised.
# Markdown, URLs, code fences, abbreviations and numbers are handled so the
# voice does not read punctuation, "e.g." or "3.10.11" literally.

_ABBREVIATIONS = {
    "e.g.": "for example", "i.e.": "that is", "etc.": "et cetera",
    "vs.": "versus", "approx.": "approximately", "dept.": "department",
    "fig.": "figure", "govt.": "government", "est.": "estimated",
    "ref.": "reference", "avg.": "average", "temp.": "temperature",
    "info.": "information", "Mr.": "Mister", "Mrs.": "Missus",
    "Ms.": "Miss", "Dr.": "Doctor", "Prof.": "Professor", "St.": "Saint",
    "Jr.": "Junior", "Sr.": "Senior", "min.": "minimum", "max.": "maximum",
}

_ACRONYMS = {
    "AI": "A I", "API": "A P I", "CPU": "C P U", "GPU": "G P U",
    "SSD": "S S D", "HDD": "H D D", "URL": "U R L", "HTTP": "H T T P",
    "HTTPS": "H T T P S", "JSON": "jason", "HTML": "H T M L",
    "CSS": "C S S", "SQL": "sequel", "PDF": "P D F", "OS": "O S",
    "UI": "U I", "ID": "I D", "TTS": "T T S", "STT": "S T T",
    "LLM": "L L M", "CLI": "C L I", "SDK": "S D K", "OK": "okay",
    "MB": "megabytes", "GB": "gigabytes", "TB": "terabytes",
    "KB": "kilobytes", "AM": "A M", "PM": "P M",
}

_CURRENCY = (("$", "dollars"), ("\u20b9", "rupees"), ("\u20ac", "euros"),
             ("\u00a3", "pounds"))

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve "
         "thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety")


def number_to_words(value):
    """Read an integer aloud naturally: 2026 -> 'two thousand and twenty six'."""
    n = int(value)
    if n < 0:
        return "minus " + number_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return (_ONES[hundreds] + " hundred"
                + (f" and {number_to_words(rest)}" if rest else ""))
    for size, label in ((10 ** 9, "billion"), (10 ** 6, "million"),
                        (1000, "thousand")):
        if n >= size:
            head, rest = divmod(n, size)
            joiner = " and " if 0 < rest < 100 else " "
            return (number_to_words(head) + " " + label
                    + (joiner + number_to_words(rest) if rest else ""))
    return str(n)


def _say_decimal(raw):
    whole, _, frac = str(raw).partition(".")
    digits = " ".join(_ONES[int(d)] for d in frac if d.isdigit())
    return f"{number_to_words(whole or 0)} point {digits}".strip()


def _say_amount(raw):
    raw = str(raw).replace(",", "")
    return _say_decimal(raw) if "." in raw else number_to_words(raw)


def _expand_times(text):
    def repl(m):
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            return m.group(0)
        spoken = number_to_words(hour)
        if minute == 0:
            return spoken + " o'clock"
        if minute < 10:
            return f"{spoken} oh {number_to_words(minute)}"
        return f"{spoken} {number_to_words(minute)}"
    return re.sub(r"\b(\d{1,2}):(\d{2})\b", repl, text)


def _expand_numbers(text):
    text = _expand_times(text)
    for symbol, word in _CURRENCY:
        text = re.sub(re.escape(symbol) + r"\s?(\d[\d,]*(?:\.\d+)?)",
                      lambda m, w=word: f"{_say_amount(m.group(1))} {w}", text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s?%",
                  lambda m: f"{_say_amount(m.group(1))} percent", text)
    # version numbers (3.10.11) are read component by component
    text = re.sub(r"\b\d+(?:\.\d+){2,}\b",
                  lambda m: " dot ".join(m.group(0).split(".")), text)
    text = re.sub(r"(?<![\d.])\d+\.\d+(?![\d.])",
                  lambda m: _say_decimal(m.group(0)), text)
    text = re.sub(r"\b\d{1,3}(?:,\d{3})+\b",
                  lambda m: number_to_words(m.group(0).replace(",", "")), text)
    text = re.sub(r"(?<![\w.])(\d{1,9})(?![\w.])",
                  lambda m: number_to_words(m.group(1)), text)
    return text


def _expand_abbreviations(text):
    for short, spoken in _ABBREVIATIONS.items():
        text = re.sub(r"(?<!\w)" + re.escape(short), spoken, text, flags=re.IGNORECASE)
    for short, spoken in _ACRONYMS.items():
        text = re.sub(r"\b" + re.escape(short) + r"\b", spoken, text)
    return text


def _strip_markdown(text):
    text = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " the link ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+[.)]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    return text.replace("|", " ")


def prepare_speech_text(text, expand_numbers=True, expand_abbreviations=True):
    """Turn a written answer into something that reads naturally aloud."""
    out = str(text or "")
    if not out.strip():
        return ""
    out = _strip_markdown(out)
    out = re.sub(r"[ \t]+", " ", out)
    if expand_abbreviations:
        out = _expand_abbreviations(out)
    if expand_numbers:
        out = _expand_numbers(out)
    out = re.sub(r"\s*([.,;:!?])\s*", r"\1 ", out)
    out = re.sub(r"\.{2,}", ".", out)
    return re.sub(r"\s+", " ", out).strip()


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def speech_chunks(text, max_chars=240):
    """Split spoken text into sentence-sized pieces.

    Each piece is one engine utterance, which gives a natural pause between
    sentences and a point where stop-speaking can take effect immediately.
    """
    max_chars = max(60, int(max_chars or 240))
    chunks = []
    for sentence in _SENTENCE_SPLIT.split(str(text or "").strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= max_chars:
            chunks.append(sentence)
            continue
        for part in re.split(r"(?<=[,;:])\s+", sentence):
            part = part.strip()
            while len(part) > max_chars:
                cut = part.rfind(" ", 0, max_chars)
                cut = cut if cut > 40 else max_chars
                chunks.append(part[:cut].strip())
                part = part[cut:].strip()
            if part:
                chunks.append(part)
    return chunks


class Speaker:
    """pyttsx3 speech: metadata-driven voice choice, paced chunks, interruptible."""

    def __init__(self, engine=None, catalog=None):
        self.enabled = bool(_voice_cfg().get("enabled", True))
        self._engine = engine
        self._lock = threading.RLock()
        self._voices = {}
        self._catalog = list(catalog or [])
        self._current = None
        self._speaking = threading.Event()
        self._stop = threading.Event()

    # ---------------- engine ----------------

    def _init_engine(self):
        if self._engine is not None:
            return self._engine
        try:
            import pyttsx3
            eng = pyttsx3.init()
            self._engine = eng
            if not self._catalog:
                self._index_voices(eng)
            self._apply_engine_settings(eng)
            return eng
        except Exception as e:
            log.warning("TTS unavailable: %s", e)
            self.enabled = False
            return None

    def _apply_engine_settings(self, engine):
        cfg = _voice_cfg()
        try:
            engine.setProperty("rate", int(cfg.get("tts_rate", 170)))
        except Exception as e:
            log.debug("could not set TTS rate: %s", e)
        try:
            volume = max(0.0, min(1.0, float(cfg.get("tts_volume", 1.0))))
            engine.setProperty("volume", volume)
        except Exception as e:
            log.debug("could not set TTS volume: %s", e)

    def _index_voices(self, engine):
        self._catalog = describe_voices(engine)
        self._voices = {}
        for v in self._catalog:
            if v["language"]:
                self._voices.setdefault(v["language"], v["id"])
            if v["gender"]:
                self._voices.setdefault(v["gender"], v["id"])
        chosen = self.voice_for("")
        if chosen:
            self._voices["current"] = chosen["name"]
            self._current = chosen["name"]

    def available_voices(self):
        return dict(self._voices)

    # ---------------- selection ----------------

    def voice_for(self, text=""):
        """Voice that will speak this text (None = engine default)."""
        cfg = _voice_cfg()
        language = "english"
        if text:
            try:
                language = detect_language(text)["primary"]
            except Exception:
                language = "english"
        if language not in ("english", "hindi", "telugu"):
            language = "english"
        return select_voice(self._catalog, gender=cfg.get("voice_gender", "male"),
                            name=cfg.get("voice_name", ""), language=language)

    def voice_state(self):
        """Everything the dashboard needs to show and change the voice."""
        if self._engine is None and self.enabled:
            self._init_engine()
        chosen = self.voice_for("")
        cfg = _voice_cfg()
        return {
            "engine": cfg.get("tts_engine", "pyttsx3"),
            "available": bool(self._catalog),
            "current": (chosen or {}).get("name", ""),
            "current_gender": (chosen or {}).get("gender", ""),
            "configured_gender": cfg.get("voice_gender", "male"),
            "configured_name": cfg.get("voice_name", ""),
            "rate": int(cfg.get("tts_rate", 170)),
            "volume": float(cfg.get("tts_volume", 1.0)),
            "voices": [dict(v, current=bool(chosen and v["id"] == chosen["id"]))
                       for v in self._catalog],
        }

    def update_settings(self, name=None, gender=None, rate=None, volume=None):
        """Change voice/rate/volume, apply live and persist to config.json."""
        changes = {}
        if name is not None:
            changes["voice_name"] = str(name).strip()
        if gender is not None:
            value = str(gender).strip().lower()
            if value not in VOICE_GENDERS + ("any",):
                return False
            changes["voice_gender"] = value
        if rate is not None:
            try:
                changes["tts_rate"] = max(80, min(300, int(rate)))
            except (TypeError, ValueError):
                return False
        if volume is not None:
            try:
                changes["tts_volume"] = max(0.0, min(1.0, float(volume)))
            except (TypeError, ValueError):
                return False
        if not changes:
            return False
        config.CONFIG.setdefault("voice", {}).update(changes)
        config.update_user("voice", changes)
        engine = self._engine
        if engine is not None:
            self._apply_engine_settings(engine)
            self._apply_voice(engine, self.voice_for(""))
        return True

    def _apply_voice(self, engine, voice):
        if not voice:
            return
        try:
            engine.setProperty("voice", voice["id"])
            current = engine.getProperty("voice")
            if isinstance(current, str) and current and current != voice["id"]:
                log.warning("TTS voice did not switch (wanted %s)", voice["name"])
                return
            self._current = voice["name"]
            log.debug("speaking with %s (%s)", voice["name"], voice["gender"] or "unknown")
        except Exception as e:
            log.warning("could not select TTS voice %s: %s", voice.get("name"), e)

    # ---------------- speaking ----------------

    def speak(self, text, output_mode=None):
        """Speak text unless the mode is text-only or a stop was requested.

        The answer is spoken sentence by sentence so pauses are natural and
        stop-speaking is immediate. Returns True when audio was produced.
        """
        mode = output_mode or _voice_cfg().get("output_mode", "both")
        if mode == "text" or not self.enabled:
            return False
        cfg = _voice_cfg()
        spoken = prepare_speech_text(text,
                                    expand_numbers=cfg.get("tts_read_numbers", True),
                                    expand_abbreviations=cfg.get("tts_expand_abbreviations", True))
        if not spoken:
            return False
        chunks = speech_chunks(spoken, cfg.get("tts_max_sentence_chars", 240))
        if not chunks:
            return False
        pause = max(0, int(cfg.get("tts_sentence_pause_ms", 200))) / 1000.0

        engine = self._init_engine()
        if not engine:
            return False
        with self._lock:
            self._stop.clear()
            self._speaking.set()
            try:
                self._apply_voice(engine, self.voice_for(text))
                for index, chunk in enumerate(chunks):
                    if self._stop.is_set() or events.is_stopped():
                        log.info("speech stopped by request")
                        break
                    engine.say(chunk)
                    engine.runAndWait()
                    if pause and index < len(chunks) - 1 and not self._stop.is_set():
                        time.sleep(pause)
            except Exception as e:
                log.warning("speak failed: %s", e)
                return False
            finally:
                self._speaking.clear()
        return True

    def stop_speaking(self):
        """Stop the current utterance and the remaining queued sentences."""
        self._stop.set()
        engine = self._engine
        if engine is not None:
            try:
                engine.stop()
            except Exception as e:
                log.debug("engine stop failed: %s", e)

    @property
    def speaking(self):
        return self._speaking.is_set()


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
                            started_at = time.time()
                        silence = 0
                    if speech_started:
                        rec.AcceptWaveform(data)
                        if started_at and time.time() - started_at > max_seconds:
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

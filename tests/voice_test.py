"""TTS voice tests: male default, fallback, configuration, initialisation.

Run: python tests/voice_test.py

No audio is played and no microphone is touched. The selection and pacing
logic runs against fake engines/voices; one check initialises the real
pyttsx3 engine (silent) to confirm the installed voice list is readable.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL, SKIP = [], [], []


def check(name, fn):
    try:
        result = fn()
        ok = bool(result)
        (PASS if ok else FAIL).append(name)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if not ok and result is not None and not isinstance(result, bool):
            print(f"     detail: {result}")
        return ok
    except Exception as e:
        FAIL.append(f"{name} ({type(e).__name__}: {e})")
        print(f"[FAIL] {name} -> {type(e).__name__}: {e}")
        return False


def skip(name, why):
    SKIP.append(f"{name} ({why})")
    print(f"[SKIP] {name} -> {why}")


from harry_core import config, speech  # noqa: E402
from harry_core.speech import (  # noqa: E402
    Speaker, describe_voices, number_to_words, prepare_speech_text, select_voice,
    speech_chunks,
)


# --------------------------------------------------------------- fake engines
class FakeVoice:
    def __init__(self, name, vid, gender, languages=("en-US",)):
        self.name = name
        self.id = vid
        self.gender = gender
        self.languages = list(languages)


class FakeEngine:
    """Records what would be spoken; never touches audio hardware."""

    def __init__(self, voices):
        self._voices = voices
        self.props = {"rate": 200, "volume": 1.0, "voice": ""}
        self.said = []
        self.on_run = None

    def getProperty(self, name):
        if name == "voices":
            return self._voices
        return self.props.get(name)

    def setProperty(self, name, value):
        self.props[name] = value

    def say(self, text):
        self.said.append(text)

    def runAndWait(self):
        if self.on_run:
            self.on_run(len(self.said))

    def stop(self):
        pass


class FakeTts:
    """Minimal ISpeechVoice stand-in that records speech and purges."""

    def __init__(self):
        self.said = []
        self.purged = 0
        self.pending = 0
        self.hook = None

    def Speak(self, text, flags):
        if flags == 3:                 # purge
            self.purged += 1
            self.pending = 0
        else:
            self.said.append(text)
            self.pending = 2           # two "still speaking" polls, then done

    def WaitUntilDone(self, ms):
        if self.hook:
            self.hook()
        if self.pending > 0:
            self.pending -= 1
            return False
        return True


class FakeSapiDriver:
    def __init__(self):
        self._tts = FakeTts()
        self.props = {}

    def setProperty(self, name, value):
        self.props[name] = value


class FakeSapiEngine(FakeEngine):
    """Engine exposing a SAPI-like driver, i.e. the immediate path."""

    def __init__(self, voices):
        super().__init__(voices)
        self.driver = FakeSapiDriver()
        from types import SimpleNamespace
        self.proxy = SimpleNamespace(_driver=self.driver)


FEMALE = FakeVoice("Microsoft Hazel Desktop - English (Great Britain)",
                   "id-female", "Female", ("en-GB",))
MALE = FakeVoice("Microsoft George Desktop - English (Great Britain)",
                 "id-male", "Male", ("en-GB",))
NATURAL_MALE = FakeVoice("Microsoft Ryan Online (Natural) - English (United Kingdom)",
                         "id-natural", "Male", ("en-GB",))
UNKNOWN = FakeVoice("Some Voice", "id-unknown", "", ("en-US",))


def catalog_of(*voices):
    return describe_voices(FakeEngine(list(voices)))


def with_gender(gender, name=""):
    """Run fn with a temporary voice configuration."""
    original = dict(config.CONFIG.get("voice", {}))
    config.CONFIG["voice"]["voice_gender"] = gender
    config.CONFIG["voice"]["voice_name"] = name
    return original


def restore_voice(original):
    config.CONFIG["voice"] = original


# --------------------------------------------------------------- selection
def test_male_preferred_from_metadata():
    voices = catalog_of(FEMALE, MALE)          # female is listed first
    chosen = select_voice(voices, gender="male", language="english")
    return chosen and chosen["id"] == "id-male"


def test_natural_male_ranks_first():
    voices = catalog_of(MALE, FEMALE, NATURAL_MALE)
    chosen = select_voice(voices, gender="male", language="english")
    return chosen and chosen["id"] == "id-natural"


def test_name_never_decides_gender():
    # A voice called "Mark" whose metadata says female must not be treated as male.
    tricky = FakeVoice("Microsoft Mark", "id-tricky", "Female")
    plain = FakeVoice("Voice A", "id-a", "Male")
    chosen = select_voice(catalog_of(tricky, plain), gender="male", language="english")
    return chosen and chosen["id"] == "id-a"


def test_unknown_gender_is_not_assumed_male():
    chosen = select_voice(catalog_of(UNKNOWN), gender="male", language="english")
    return chosen and chosen["id"] == "id-unknown" and chosen["gender"] == ""


def test_fallback_when_no_male_installed():
    chosen = select_voice(catalog_of(FEMALE), gender="male", language="english")
    return chosen and chosen["id"] == "id-female"


def test_explicit_name_wins():
    voices = catalog_of(FEMALE, MALE)
    chosen = select_voice(voices, gender="male", name="Hazel", language="english")
    return chosen and chosen["id"] == "id-female"


def test_unknown_name_falls_back_to_gender():
    voices = catalog_of(FEMALE, MALE)
    chosen = select_voice(voices, gender="male", name="Not Installed Voice",
                          language="english")
    return chosen and chosen["id"] == "id-male"


def test_hindi_voice_used_when_installed():
    hindi = FakeVoice("Microsoft Heera - Hindi (India)", "id-hi", "Female",
                      ("hi-IN",))
    english_male = FakeVoice("Microsoft David", "id-en", "Male")
    chosen = select_voice(catalog_of(hindi, english_male), gender="male",
                          language="hindi")
    return chosen and chosen["id"] == "id-hi"


def test_empty_catalog_returns_none():
    return select_voice([], gender="male") is None


# --------------------------------------------------------------- configuration
def test_speaker_reads_configured_gender():
    original = with_gender("female")
    try:
        speaker = Speaker(engine=FakeEngine([FEMALE, MALE]),
                          catalog=catalog_of(FEMALE, MALE))
        chosen = speaker.voice_for("hello there")
        return chosen and chosen["id"] == "id-female"
    finally:
        restore_voice(original)


def test_speaker_defaults_to_male():
    original = with_gender("male")
    try:
        speaker = Speaker(engine=FakeEngine([FEMALE, MALE]),
                          catalog=catalog_of(FEMALE, MALE))
        chosen = speaker.voice_for("hello there")
        return chosen and chosen["id"] == "id-male"
    finally:
        restore_voice(original)


def test_update_settings_persists_and_applies():
    original = with_gender("male")
    saved = config.update_user
    written = {}

    def fake_update_user(section, values, path=None):
        written[section] = dict(values)
        return "config.json"

    config.update_user = fake_update_user
    try:
        engine = FakeEngine([FEMALE, MALE])
        speaker = Speaker(engine=engine, catalog=catalog_of(FEMALE, MALE))
        ok = speaker.update_settings(gender="female", rate=150, volume=0.8)
        return (ok and written["voice"]["voice_gender"] == "female"
                and written["voice"]["tts_rate"] == 150
                and written["voice"]["tts_volume"] == 0.8
                and engine.props["rate"] == 150
                and engine.props["volume"] == 0.8
                and config.CONFIG["voice"]["tts_rate"] == 150)
    finally:
        config.update_user = saved
        restore_voice(original)


def test_update_settings_rejects_bad_values():
    original = with_gender("male")
    try:
        speaker = Speaker(engine=FakeEngine([MALE]), catalog=catalog_of(MALE))
        return (speaker.update_settings(gender="robot") is False
                and speaker.update_settings(rate="fast") is False
                and speaker.update_settings() is False)
    finally:
        restore_voice(original)


def test_update_user_writes_only_voice_keys(tmp_path=None):
    import json
    import tempfile
    folder = tempfile.mkdtemp(prefix="harry_cfg_")
    path = os.path.join(folder, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"user_name": "Tester", "voice": {"output_mode": "text"}}, f)
    result = config.update_user("voice", {"tts_rate": 160}, path=path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return (result == path and data["user_name"] == "Tester"
            and data["voice"]["output_mode"] == "text"
            and data["voice"]["tts_rate"] == 160)


# --------------------------------------------------------------- text prep
def test_numbers_become_words():
    spoken = prepare_speech_text("There are 2026 items.")
    return "two thousand and twenty six" in spoken


def test_decimal_and_version_numbers():
    spoken = prepare_speech_text("Version 3.10.11 needs 3.14 gigabytes.")
    return ("three dot ten dot eleven" in spoken
            and "three point one four" in spoken)


def test_currency_percent_and_time():
    spoken = prepare_speech_text("It costs $45, a 50% rise at 10:30.")
    return ("forty five dollars" in spoken and "fifty percent" in spoken
            and "ten thirty" in spoken)


def test_abbreviations_expanded():
    spoken = prepare_speech_text("Use an API, e.g. the JSON one, etc.")
    return ("A P I" in spoken and "for example" in spoken
            and "jason" in spoken and "et cetera" in spoken)


def test_markdown_and_urls_stripped_for_speech():
    spoken = prepare_speech_text(
        "## Title\n**bold** text\n\n```py\nprint(1)\n```\nSee [docs](https://x.io).")
    return ("#" not in spoken and "**" not in spoken
            and "print(1)" not in spoken and "https" not in spoken)


def test_number_to_words_edges():
    return (number_to_words(0) == "zero" and number_to_words(21) == "twenty one"
            and number_to_words(105) == "one hundred and five"
            and number_to_words(1000000) == "one million")


# --------------------------------------------------------------- pacing
def test_chunks_split_long_text():
    text = "First sentence here. " + ("word " * 120).strip() + ". Last one."
    chunks = speech_chunks(text, max_chars=120)
    return len(chunks) >= 3 and all(len(c) <= 120 for c in chunks)


def test_short_text_is_one_chunk():
    return speech_chunks("Hello there.", 240) == ["Hello there."]


def test_speak_uses_chunks_in_order():
    original = dict(config.CONFIG["voice"])
    config.CONFIG["voice"]["tts_sentence_pause_ms"] = 0
    try:
        engine = FakeEngine([MALE])
        speaker = Speaker(engine=engine, catalog=catalog_of(MALE))
        ok = speaker.speak("One. Two. Three.")
        return (ok is True and engine.said == ["One.", "Two.", "Three."]
                and engine.props["voice"] == "id-male")
    finally:
        config.CONFIG["voice"] = original


def test_stop_speaking_interrupts_remaining_chunks():
    original = dict(config.CONFIG["voice"])
    config.CONFIG["voice"]["tts_sentence_pause_ms"] = 0
    try:
        engine = FakeEngine([MALE])
        speaker = Speaker(engine=engine, catalog=catalog_of(MALE))
        engine.on_run = lambda _n: (speaker.stop_speaking()
                                    if len(engine.said) == 1 else None)
        speaker.speak("One. Two. Three. Four.")
        return engine.said == ["One."]
    finally:
        config.CONFIG["voice"] = original


def test_sapi_path_speaks_all_chunks_with_selected_voice():
    original = dict(config.CONFIG["voice"])
    config.CONFIG["voice"]["tts_sentence_pause_ms"] = 0
    config.CONFIG["voice"]["tts_rate"] = 160
    config.CONFIG["voice"]["tts_volume"] = 0.7
    # Isolate from the user's persisted preference (auto, mixed or english).
    config.CONFIG["voice"]["voice_gender"] = "male"
    config.CONFIG["voice"]["voice_name"] = ""
    try:
        engine = FakeSapiEngine([FEMALE, MALE])
        speaker = Speaker(engine=engine, catalog=catalog_of(FEMALE, MALE))
        ok = speaker.speak("One. Two. Three.")
        driver = engine.driver
        return (ok is True and driver._tts.said == ["One.", "Two.", "Three."]
                and driver._tts.purged == 0
                and driver.props["voice"] == "id-male"
                and driver.props["rate"] == 160
                and driver.props["volume"] == 0.7)
    finally:
        config.CONFIG["voice"] = original


def test_stop_purges_mid_sentence():
    original = dict(config.CONFIG["voice"])
    config.CONFIG["voice"]["tts_sentence_pause_ms"] = 0
    try:
        engine = FakeSapiEngine([MALE])
        speaker = Speaker(engine=engine, catalog=catalog_of(MALE))
        tts = engine.driver._tts

        def stop_once():
            if tts.said == ["One."] and tts.pending == 1 and not tts.purged:
                speaker.stop_speaking()

        tts.hook = stop_once
        spoke = speaker.speak("One. Two. Three. Four.")
        return (spoke is True and tts.purged == 1 and tts.said == ["One."])
    finally:
        config.CONFIG["voice"] = original


def test_text_only_mode_speaks_nothing():
    engine = FakeEngine([MALE])
    speaker = Speaker(engine=engine, catalog=catalog_of(MALE))
    spoken = speaker.speak("Should not be spoken", output_mode="text")
    return spoken is False and engine.said == []


def test_speak_without_engine_fails_gracefully():
    speaker = Speaker()
    speaker.enabled = False
    return speaker.speak("nothing") is False


# --------------------------------------------------------------- real TTS init
def test_real_engine_initialises_and_picks_male():
    try:
        import pyttsx3  # noqa: F401
    except Exception as e:
        skip("real TTS engine initialises", f"pyttsx3 unavailable: {e}")
        return True
    original = dict(config.CONFIG["voice"])
    config.CONFIG["voice"]["voice_gender"] = "male"
    config.CONFIG["voice"]["voice_name"] = ""
    try:
        speaker = Speaker()
        engine = speaker._init_engine()
        if not engine:
            return False
        state = speaker.voice_state()
        if not state["voices"]:
            skip("real TTS reports a male voice", "no SAPI voices installed")
            return True
        print(f"     installed: {[v['name'] for v in state['voices']]}")
        print(f"     selected: {state['current']} ({state['current_gender']})")
        males = [v for v in state["voices"] if v["gender"] == "male"]
        return bool(males) and state["current_gender"] == "male"
    finally:
        config.CONFIG["voice"] = original


def test_language_detection_unchanged():
    from harry_core.speech import detect_language
    return (detect_language("hello there")["primary"] == "english"
            and detect_language("\u0c28\u0c3e \u0c2a\u0c47\u0c30\u0c41")["has_telugu_script"])


# --------------------------------------------------------------- run
TESTS = [
    ("male voice chosen from metadata (female listed first)",
     test_male_preferred_from_metadata),
    ("natural/premium male outranks a desktop male", test_natural_male_ranks_first),
    ("voice name is never used to guess gender", test_name_never_decides_gender),
    ("missing gender metadata is not treated as male",
     test_unknown_gender_is_not_assumed_male),
    ("graceful fallback when no male voice is installed",
     test_fallback_when_no_male_installed),
    ("explicitly configured voice wins", test_explicit_name_wins),
    ("uninstalled configured name falls back to gender",
     test_unknown_name_falls_back_to_gender),
    ("installed Hindi voice is used for Hindi replies",
     test_hindi_voice_used_when_installed),
    ("empty catalog selects nothing", test_empty_catalog_returns_none),
    ("speaker honours configured gender", test_speaker_reads_configured_gender),
    ("speaker defaults to a male voice", test_speaker_defaults_to_male),
    ("update_settings persists and applies live",
     test_update_settings_persists_and_applies),
    ("invalid voice settings are rejected", test_update_settings_rejects_bad_values),
    ("update_user keeps unrelated config keys", test_update_user_writes_only_voice_keys),
    ("numbers are spoken as words", test_numbers_become_words),
    ("decimals and version numbers read naturally",
     test_decimal_and_version_numbers),
    ("currency, percent and times read naturally",
     test_currency_percent_and_time),
    ("abbreviations and acronyms expanded", test_abbreviations_expanded),
    ("markdown, code and URLs are not read aloud",
     test_markdown_and_urls_stripped_for_speech),
    ("number_to_words covers 0/21/105/million", test_number_to_words_edges),
    ("long answers are chunked for pacing", test_chunks_split_long_text),
    ("short answer stays one utterance", test_short_text_is_one_chunk),
    ("speech uses the selected voice and chunk order",
     test_speak_uses_chunks_in_order),
    ("stop speaking interrupts remaining sentences",
     test_stop_speaking_interrupts_remaining_chunks),
    ("SAPI path speaks every chunk with the chosen voice",
     test_sapi_path_speaks_all_chunks_with_selected_voice),
    ("stop purges the utterance mid-sentence", test_stop_purges_mid_sentence),
    ("text-only mode produces no speech", test_text_only_mode_speaks_nothing),
    ("no engine degrades without raising", test_speak_without_engine_fails_gracefully),
    ("real TTS engine initialises and selects a male voice",
     test_real_engine_initialises_and_picks_male),
    ("language detection unchanged (no STT regression)",
     test_language_detection_unchanged),
]

print("\n--- voice tests ---")
for name, fn in TESTS:
    check(name, fn)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
for f in FAIL:
    print("  FAILED:", f)
for s in SKIP:
    print("  SKIPPED:", s)
sys.exit(1 if FAIL else 0)

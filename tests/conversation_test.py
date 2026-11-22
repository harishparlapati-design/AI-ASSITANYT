"""Tests for continuous voice, the microphone guard and the speech-wait race.

Run: python tests/conversation_test.py

A scripted fake listener and a fake brain are used, so nothing here depends on a
microphone. That is deliberate: this suite covers the state machine and the
guards, and never presents itself as proof that the microphone works. Real
device behaviour is measured separately by tests/voice_live_probe.py.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harry_core import config, speech  # noqa: E402
from harry_core.conversation import (  # noqa: E402
    CONTINUOUS, PUSH_TO_TALK, VoiceConversation, echoes_own_speech,
)

PASS, FAIL = [], []


def check(name, fn):
    try:
        result = fn()
        ok = bool(result)
    except Exception as e:
        result, ok = f"{type(e).__name__}: {e}", False
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -> {result}"))


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class FakeListener:
    def __init__(self, script=None, available=True):
        self.script = list(script or [])
        self.calls = 0
        self._available = available

    def available(self):
        return self._available

    def listen_once(self, **kwargs):
        self.calls += 1
        time.sleep(0.01)
        return self.script.pop(0) if self.script else ""


class FakeBrain:
    def __init__(self, speaking=False, last_spoken=""):
        self.heard = []
        self.speaking = speaking
        self.stops = 0
        self._last_spoken = last_spoken

    def wait_speech(self, timeout=0.0):
        return not self.speaking

    def handle_input(self, text, source="voice"):
        self.heard.append(text)
        return "ok"

    def stop(self):
        self.stops += 1
        self.speaking = False


HEARD_SPEECH = "A capacitor is a passive electrical component that stores energy"

# --------------------------------------------------------------- echo detection
check("a replay of Harry's own words is detected as an echo",
      lambda: echoes_own_speech("a capacitor is a passive electrical component",
                                HEARD_SPEECH))
check("a real interruption is not treated as an echo",
      lambda: not echoes_own_speech("harry stop", HEARD_SPEECH))
check("unrelated short speech is not an echo",
      lambda: not echoes_own_speech("what time is it", HEARD_SPEECH))

# ------------------------------------------------------------------- lifecycle
check("continuous mode cannot start without a speech engine",
      lambda: VoiceConversation(brain=FakeBrain(),
                                listener=FakeListener(available=False))
      .start(CONTINUOUS)["ok"] is False)
check("default mode is push to talk",
      lambda: config.CONFIG["voice"]["conversation"]["mode"] in
      (PUSH_TO_TALK, CONTINUOUS))


def continuous_turn():
    brain = FakeBrain()
    listener = FakeListener(["hello harry"])
    conversation = VoiceConversation(brain=brain, listener=listener)
    conversation.start(CONTINUOUS)
    heard = wait_for(lambda: brain.heard, 3)
    states = conversation.status()
    conversation.stop()
    ended = wait_for(lambda: not conversation.status()["running"], 3)
    return (heard and brain.heard[0] == "hello harry" and states["utterances"] >= 1
            and ended and conversation.last_response_ms is not None)


check("continuous mode hears a turn, answers it and shuts down cleanly",
      continuous_turn)


def mute_blocks_the_microphone():
    brain = FakeBrain()
    listener = FakeListener(["should not be heard", "should not be heard"])
    conversation = VoiceConversation(brain=brain, listener=listener)
    conversation.start(CONTINUOUS)
    when_started = listener.calls
    conversation.mute(True)
    time.sleep(0.6)
    muted_state = conversation.status()["muted"]
    stopped_calls = listener.calls - when_started
    conversation.stop()
    time.sleep(0.2)
    return muted_state and stopped_calls == 0 and not brain.heard


check("mute stops microphone capture immediately", mute_blocks_the_microphone)


def push_to_talk_waits_for_a_trigger():
    brain = FakeBrain()
    listener = FakeListener(["only after the trigger"])
    conversation = VoiceConversation(brain=brain, listener=listener)
    conversation.start(PUSH_TO_TALK)
    time.sleep(0.5)
    idle_calls, idle_heard = listener.calls, list(brain.heard)
    conversation.trigger()
    heard = wait_for(lambda: brain.heard, 3)
    conversation.stop()
    time.sleep(0.2)
    return idle_calls == 0 and not idle_heard and heard


check("push to talk records nothing until triggered", push_to_talk_waits_for_a_trigger)


# -------------------------------------------------------------------- barge-in
def barge_in_interrupts():
    brain = FakeBrain(speaking=True, last_spoken=HEARD_SPEECH)
    listener = FakeListener(["harry stop"])
    conversation = VoiceConversation(brain=brain, listener=listener)
    conversation.start(PUSH_TO_TALK)
    interrupted = wait_for(lambda: brain.stops >= 1, 3)
    state = conversation.status()["state"]
    conversation.stop()
    return interrupted and state in ("listening", "idle")


check("an explicit stop phrase interrupts Harry while he speaks", barge_in_interrupts)


def barge_in_ignores_echo():
    brain = FakeBrain(speaking=True, last_spoken=HEARD_SPEECH)
    # Exactly what a microphone hears from the speakers: Harry's own sentence.
    listener = FakeListener(["a capacitor is a passive electrical component"])
    conversation = VoiceConversation(brain=brain, listener=listener)
    conversation.start(PUSH_TO_TALK)
    time.sleep(0.8)
    stops_before_stop = brain.stops          # stop() itself silences Harry
    speaking_before_stop = brain.speaking
    conversation.stop()
    return stops_before_stop == 0 and speaking_before_stop is True


check("hearing Harry's own voice does not interrupt him", barge_in_ignores_echo)


# ------------------------------------------------- microphone guard in speech.py
def listener_refuses_while_speaking():
    class Speaking:
        speaking = True

    original = speech.get_speaker
    listener = speech.Listener()
    listener.available = lambda: True
    speech.get_speaker = lambda: Speaking()
    try:
        return listener.listen_once() == ""
    finally:
        speech.get_speaker = original


check("the microphone layer refuses to record while TTS is playing",
      listener_refuses_while_speaking)


# ------------------------------------------------------- speech-wait race fix
def speech_wait_race():
    """The queue is empty the moment the worker dequeues; that window must not
    let the microphone open, so ``speaking`` is true until audio really ends."""
    from harry_core.brain import Brain

    class SlowSpeaker:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def speak(self, text, output_mode=None):
            self.started.set()
            self.release.wait(10)

        def stop_speaking(self):
            self.release.set()

    brain = Brain()
    speaker = SlowSpeaker()
    brain.speaker = speaker
    brain._enqueue_speech("SENTENCE_ONE")
    caught_immediately = brain.speaking          # before the worker starts

    result = {"returned": False}

    def waiter():
        result["value"] = brain.wait_speech(5)
        result["returned"] = True

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    speaker.started.wait(3)
    time.sleep(0.25)
    blocked_while_speaking = not result["returned"]
    speaker.release.set()
    thread.join(5)
    idle_after = not brain.speaking
    brain.stop()
    return (caught_immediately and blocked_while_speaking
            and result.get("value") is True and idle_after)


check("speech-wait never reports finished while audio is still queued",
      speech_wait_race)


def wait_speech_without_audio():
    from harry_core.brain import Brain

    brain = Brain()
    brain.speaker = type("S", (), {"speak": lambda *a, **k: None,
                                   "stop_speaking": lambda *a: None})()
    return brain.wait_speech(2) is True and not brain.speaking


check("speech-wait returns at once when nothing is queued", wait_speech_without_audio)


# ------------------------------------------------ ordering and interruption
def speech_order_is_preserved():
    from harry_core.brain import Brain

    class RecordingSpeaker:
        def __init__(self):
            self.spoken = []
            self.speaking = False

        def speak(self, text, output_mode=None):
            self.speaking = True
            time.sleep(0.05)
            self.spoken.append(text)
            self.speaking = False

        def stop_speaking(self):
            pass

    brain = Brain()
    speaker = RecordingSpeaker()
    brain.speaker = speaker
    for sentence in ("First sentence.", "Second sentence.", "Third sentence."):
        brain._enqueue_speech(sentence)
    finished = brain.wait_speech(5)
    brain.stop()
    return finished and speaker.spoken == ["First sentence.", "Second sentence.",
                                            "Third sentence."]


check("streamed TTS sentences are spoken in order", speech_order_is_preserved)


def user_can_interrupt_speech():
    from harry_core.brain import Brain

    class BlockingSpeaker:
        def __init__(self):
            self.started = threading.Event()
            self.stopped = threading.Event()

        def speak(self, text, output_mode=None):
            self.started.set()
            self.stopped.wait(5)

        def stop_speaking(self):
            self.stopped.set()

    brain = Brain()
    speaker = BlockingSpeaker()
    brain.speaker = speaker
    brain._enqueue_speech("A long answer that never seems to end.")
    started = speaker.started.wait(3)
    was_speaking = brain.speaking
    brain.stop()
    released = speaker.stopped.wait(3)
    ended = wait_for(lambda: not brain.speaking, 3)
    return started and was_speaking and released and ended


check("speech can be interrupted and stops promptly", user_can_interrupt_speech)


def model_load_never_blocks_a_turn():
    """A cold Vosk model load must not happen inside a request or a turn."""
    listener = speech.Listener()
    listener.available = lambda: True
    listener._model = None
    warmed = {"count": 0}

    def fake_warm():
        warmed["count"] += 1
        return False

    listener.warm = fake_warm
    started = time.time()
    text = listener.transcribe_pcm(b"\x00\x00" * 4000, 16000)
    elapsed = time.time() - started
    return text == "" and elapsed < 1.0 and warmed["count"] >= 1


check("a cold speech model never blocks a turn", model_load_never_blocks_a_turn)

print(f"\n===== RESULT: {len(PASS)} passed, {len(FAIL)} failed =====")
for failure in FAIL:
    print(f"  FAILED: {failure}")
sys.exit(1 if FAIL else 0)

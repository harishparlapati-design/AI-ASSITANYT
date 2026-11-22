"""Continuous voice conversation.

One worker thread owns the microphone: listen -> transcribe (Vosk) -> think
(Brain) -> speak (male TTS) -> listen again. The microphone is never opened
while Harry is speaking, and ``wait_speech`` is checked before every capture, so
the assistant cannot transcribe its own voice.

Barge-in: while Harry speaks, a monitor listens for an explicit stop phrase. It
ignores anything that closely matches the sentence being spoken, because a
plain Python microphone chain has no acoustic echo cancellation - without that
check Harry would interrupt himself. Interruption is therefore phrase-based
("stop", "harry stop", "aagu"), not any-sound-based, and that limitation is
reported rather than hidden.

State is published on the event bus as ``conversation_state`` and exposed at
/api/conversation for the dashboard indicators.
"""

import logging
import re
import threading
import time

from . import config, events
from .brain import get_brain
from .speech import get_listener

log = logging.getLogger("harry.conversation")

IDLE = "idle"
LISTENING = "listening"
THINKING = "thinking"
SPEAKING = "speaking"
MUTED = "muted"
ERROR = "error"

CONTINUOUS = "continuous"
PUSH_TO_TALK = "push_to_talk"

# Only an explicit phrase interrupts: without echo cancellation, any-sound
# barge-in would trigger on Harry's own output.
_STOP_PHRASE = re.compile(
    r"\b(?:harry[, ]+)?(?:stop|stop it|stop talking|cancel|shut up|enough|"
    r"aagu|aapandi|chalu)\b", re.IGNORECASE)


def _tokens(text):
    return {w for w in re.findall(r"[a-z']+", str(text or "").lower()) if len(w) > 2}


def echoes_own_speech(heard, spoken):
    """True when ``heard`` is mostly a replay of what Harry just said."""
    heard_tokens, spoken_tokens = _tokens(heard), _tokens(spoken)
    if not heard_tokens or not spoken_tokens:
        return False
    return len(heard_tokens & spoken_tokens) / len(heard_tokens) >= 0.5


class VoiceConversation:
    """Drives continuous or push-to-talk voice turns for one Brain."""

    def __init__(self, brain=None, listener=None):
        self._brain = brain
        self._listener = listener
        self._thread = None
        self._barge_thread = None
        self._stop = threading.Event()
        self._mute = threading.Event()
        self._trigger = threading.Event()
        # One thread may read the microphone at a time. During speech the
        # barge-in monitor owns it; otherwise the conversation loop does.
        self._mic = threading.Lock()
        self._lock = threading.Lock()
        self._state = IDLE
        self.mode = self._cfg().get("mode", PUSH_TO_TALK)
        if self.mode not in (CONTINUOUS, PUSH_TO_TALK):
            self.mode = PUSH_TO_TALK
        self.last_heard = ""
        self.last_error = ""
        self.utterances = 0
        self.last_response_ms = None
        self.reconnects = 0

    # ---------------------------------------------------------------- deps
    @property
    def brain(self):
        if self._brain is None:
            self._brain = get_brain()
        return self._brain

    @property
    def listener(self):
        if self._listener is None:
            self._listener = get_listener()
        return self._listener

    def _cfg(self):
        return (config.CONFIG.get("voice", {}) or {}).get("conversation", {}) or {}

    # --------------------------------------------------------------- state
    def _set(self, state, detail=""):
        with self._lock:
            changed = state != self._state
            self._state = state
        if changed:
            log.info("conversation -> %s %s", state, detail)
        events.emit("conversation_state", {"state": state, "mode": self.mode,
                                           "detail": detail[:120],
                                           "at": time.time()})

    @property
    def state(self):
        with self._lock:
            return self._state

    def status(self):
        return {
            "state": self.state,
            "mode": self.mode,
            "running": bool(self._thread and self._thread.is_alive() and
                            not self._stop.is_set()),
            "muted": self._mute.is_set(),
            "available": bool(self.listener.available()),
            "speaking": bool(self.brain.speaking),
            "last_heard": self.last_heard,
            "last_error": self.last_error,
            "utterances": self.utterances,
            "last_response_ms": self.last_response_ms,
            "reconnects": self.reconnects,
            "barge_in": bool(self._cfg().get("barge_in", True)),
        }

    # ------------------------------------------------------------- control
    def start(self, mode=None):
        if mode in (CONTINUOUS, PUSH_TO_TALK):
            self.mode = mode
        if self._thread and self._thread.is_alive():
            return {"ok": True, "status": self.status()}
        if not self.listener.available():
            self.last_error = ("Voice input is unavailable: install Vosk and the "
                               "configured speech model.")
            self._set(ERROR, self.last_error)
            return {"ok": False, "error": self.last_error, "status": self.status()}
        self._stop.clear()
        self._mute.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="harry-voice")
        self._thread.start()
        if self._cfg().get("barge_in", True):
            self._barge_thread = threading.Thread(target=self._barge_loop,
                                                  daemon=True, name="harry-barge")
            self._barge_thread.start()
        return {"ok": True, "status": self.status()}

    def stop(self):
        self._stop.set()
        try:
            self.brain.stop()
        except Exception:
            pass
        self._set(IDLE, "stopped")
        return {"ok": True, "status": self.status()}

    def mute(self, value=True):
        if value:
            self._mute.set()
            self._set(MUTED, "microphone muted")
        else:
            self._mute.clear()
            self._set(IDLE, "microphone unmuted")
        return {"ok": True, "status": self.status()}

    def trigger(self):
        """Push-to-talk: allow exactly one utterance."""
        self._trigger.set()
        return {"ok": True, "status": self.status()}

    # ---------------------------------------------------------------- loops
    def _run(self):
        failures = 0
        while not self._stop.is_set():
            try:
                if self.mode == PUSH_TO_TALK:
                    if not self._trigger.wait(0.25):
                        continue
                    self._trigger.clear()
                self.listen_and_answer()
                failures = 0
            except Exception as e:
                failures += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("voice loop error: %s", e)
                self._set(ERROR, self.last_error)
                if failures >= 5:
                    break
                # Microphone unplugged / device busy: back off and retry.
                time.sleep(min(2 ** failures, 10))
                self.reconnects += 1
        self._set(IDLE, "conversation stopped")

    def listen_and_answer(self):
        """One turn: wait for silence, capture, answer, speak."""
        if self._mute.is_set() or self._stop.is_set():
            return ""
        # Never open the microphone while audio is queued or playing. The
        # pending counter inside Brain covers the window between dequeue and
        # the first spoken sample.
        if self.brain.speaking:
            self._set(SPEAKING)
            self.brain.wait_speech()
        if self._mute.is_set() or self._stop.is_set():
            return ""
        ready = getattr(self.listener, "model_ready", None)
        if ready is not None and not ready():
            # A cold speech model takes about a minute here; keep the loop
            # responsive and say so instead of appearing to hang on the mic.
            getattr(self.listener, "warm", lambda: None)()
            self._set(LISTENING, "loading speech model")
            time.sleep(0.5)
            return ""
        cfg = self._cfg()
        self._set(LISTENING)
        with self._mic:
            heard = self.listener.listen_once(
                silence_blocks=int(cfg.get("vad_silence_blocks", 4)),
                max_seconds=int(cfg.get("max_utterance_seconds", 20)),
                min_volume=int(cfg.get("vad_min_volume", 450)),
            )
        heard = (heard or "").strip()
        if not heard or self._stop.is_set() or self._mute.is_set():
            return ""
        self.last_heard = heard
        events.emit("voice_heard", {"text": heard})
        self._set(THINKING, heard[:80])
        started = time.perf_counter()
        self.brain.handle_input(heard, source="voice")
        self.utterances += 1
        self.last_response_ms = round((time.perf_counter() - started) * 1000)
        self._set(SPEAKING)
        return heard

    def _barge_loop(self):
        """Interrupt Harry when the user says an explicit stop phrase."""
        while not self._stop.is_set():
            if self._mute.is_set() or not self.brain.speaking:
                time.sleep(0.1)
                continue
            if not self._mic.acquire(timeout=0.2):
                continue
            try:
                heard = self.listener.listen_once(
                    silence_blocks=2,
                    max_seconds=int(self._cfg().get("barge_max_seconds", 4)),
                    ignore_while_speaking=False,
                )
            except Exception as e:
                log.debug("barge-in capture failed: %s", e)
                heard = ""
            finally:
                self._mic.release()
            if not heard:
                continue
            if echoes_own_speech(heard, getattr(self.brain, "_last_spoken", "")):
                log.info("barge-in ignored: that was Harry's own voice")
                continue
            if _STOP_PHRASE.search(heard):
                log.info("barge-in: user asked Harry to stop")
                try:
                    self.brain.stop()
                finally:
                    self._set(LISTENING, "interrupted by user")


_conversation = None


def get_conversation():
    global _conversation
    if _conversation is None:
        _conversation = VoiceConversation()
    return _conversation

"""Harry Brain - the orchestrator.

Input (text or voice) flows through here:

  1. fast deterministic replies (greetings, time/date, arithmetic) - no model
  2. emergency/control commands ("stop", "cancel", "emergency stop") - instant
  3. memory commands ("remember...", "forget...", "what do you remember")
  4. otherwise -> TaskEngine decides question vs task and runs the lifecycle
  5. output goes to text AND voice (per configured output mode)

Every answer is delivered ONCE: it is emitted as a single ``assistant_message``
event carrying a unique message id and the originating request id, so clients
can render exactly one bubble even if they also poll for the result. A client
must never render the same answer from two transports (HTTP + event stream).

The brain never contains business logic itself; it delegates to subsystems
and keeps the conversation memory + event flow coherent.
"""

import ast
import logging
import re
import threading
import time
import uuid
from datetime import datetime

from . import config, events
from .memory import get_memory
from .speech import get_speaker
from .task_engine import get_engine

log = logging.getLogger("harry.brain")

# --------------------------------------------------------------------- fast paths

_GREETING = re.compile(
    r"^(?:hi+|hello+|hey+|hai|namaste|namaskaram|"
    r"good\s+(?:morning|afternoon|evening|night)|"
    r"(?:hi|hello|hey)\s+harry|harry)[\s!.?]*$", re.IGNORECASE)

_THANKS = re.compile(r"^(?:ok\s+)?(?:thanks|thank\s+you|thank\s+u|thx|tq)[\s!.?]*$",
                     re.IGNORECASE)

_BYE = re.compile(r"^(?:ok\s+)?(?:bye|goodbye|good\s+bye|see\s+you)[\s!.?]*$",
                  re.IGNORECASE)

_WHO = re.compile(r"^(?:who\s+are\s+you|what(?:'s| is)\s+your\s+name|"
                  r"your\s+name)[\s?.!]*$", re.IGNORECASE)

_TIME_Q = re.compile(
    r"\b(?:what(?:'s| is)?\s+the\s+time|what\s+time\s+is\s+it|current\s+time|"
    r"time\s+now|itime\s+enti|time\s+enti|samayam\s+enti|samayam\s+emiti)\b",
    re.IGNORECASE)

_DATE_Q = re.compile(
    r"\b(?:what(?:'s| is)?\s+(?:the\s+|today'?s\s+)?date|current\s+date|"
    r"today'?s\s+date|date\s+enti|ee\s+roju\s+enti|today\s+date)\b",
    re.IGNORECASE)

_ARITH_EXPR = re.compile(r"^[\d\s.+\-*/%()^x×÷]+$", re.IGNORECASE)
_ARITH_PREFIX = re.compile(
    r"^(?:what(?:'s| is)|calculate|compute|solve|evaluate|how\s+much\s+is|"
    r"find)\s+", re.IGNORECASE)


def _format_number(value):
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(round(value, 10))
    return str(value)


def _safe_arithmetic(expr):
    """Evaluate a pure arithmetic expression safely (no names, no calls)."""
    src = (expr.replace("×", "*").replace("÷", "/").replace("^", "**")
           .replace("x", "*").replace("X", "*"))
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError:
        return None

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return node.value
            raise ValueError("bad constant")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            v = ev(node.operand)
            return v if isinstance(node.op, ast.UAdd) else -v
        if isinstance(node, ast.BinOp):
            left, right = ev(node.left), ev(node.right)
            op = node.op
            if isinstance(op, ast.Add):
                return left + right
            if isinstance(op, ast.Sub):
                return left - right
            if isinstance(op, ast.Mult):
                return left * right
            if isinstance(op, ast.Div):
                return left / right
            if isinstance(op, ast.FloorDiv):
                return left // right
            if isinstance(op, ast.Mod):
                return left % right
            if isinstance(op, ast.Pow):
                if abs(right) > 64 or abs(left) > 1e6:
                    raise ValueError("exponent too large")
                return left ** right
            raise ValueError("unsupported operator")
        raise ValueError("unsupported expression")

    try:
        return ev(tree)
    except Exception:
        return None


class Brain:
    def __init__(self):
        self.memory = get_memory()
        self.speaker = get_speaker()
        self.engine = get_engine()
        self.busy = False
        self._speech_q = None
        self._speech_thread = None
        self._speech_lock = threading.Lock()
        self._speaking = threading.Event()

    # ------------------------------------------------------------------ speech

    def _ensure_speech_worker(self):
        """Create + start the background TTS worker once."""
        with self._speech_lock:
            if self._speech_thread and self._speech_thread.is_alive():
                return
            import queue as _queue
            self._speech_q = _queue.Queue()
            self._speech_thread = threading.Thread(
                target=self._speech_loop, args=(self._speech_q,),
                daemon=True, name="harry-tts")
            self._speech_thread.start()

    def _speech_loop(self, q):
        while True:
            item = q.get()
            if item is None:
                return
            text = item
            if events.is_stopped():
                continue
            self._speaking.set()
            try:
                self.speaker.speak(text)
            except Exception as e:
                log.warning("queued speech failed: %s", e)
            finally:
                self._speaking.clear()

    def _enqueue_speech(self, text):
        """Speak without blocking the caller (used by the dashboard)."""
        try:
            self._ensure_speech_worker()
            self._speech_q.put(text)
        except Exception as e:
            log.warning("could not queue speech: %s", e)

    def wait_speech(self, timeout=30.0):
        """Block until queued speech is done (used on shutdown)."""
        t = self._speech_thread
        if not t or not t.is_alive():
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            q = self._speech_q
            if q is None or q.empty():
                time.sleep(0.1)
                if q is None or q.empty():
                    return
            time.sleep(0.05)

    def stop(self):
        """Emergency stop: cancel queued speech and halt current playback."""
        try:
            while self._speech_q is not None and not self._speech_q.empty():
                self._speech_q.get_nowait()
        except Exception:
            pass
        self.speaker.stop_speaking()

    @property
    def speaking(self):
        """True while Harry is producing speech or has speech queued."""
        if self._speaking.is_set():
            return True
        return not (self._speech_q is None or self._speech_q.empty())

    # ------------------------------------------------------------------ output

    def say(self, text, force_voice=False, request_id=None, wait=True,
            latency_ms=None):
        """Deliver an answer as text (+ voice per output mode). Exactly once."""
        if text is None:
            return
        text = str(text)
        try:
            print(f"Harry: {text}")
        except UnicodeEncodeError:   # console can't encode (e.g. cp1252)
            print(f"Harry: {text.encode('utf-8', 'replace').decode('utf-8', 'replace')}"
                  .encode("ascii", "replace").decode())

        payload = {
            "id": uuid.uuid4().hex,
            "text": text,
            "ts": time.time(),
        }
        if request_id:
            payload["request_id"] = request_id
        if latency_ms is not None:
            payload["latency_ms"] = int(latency_ms)
        events.emit("assistant_message", payload)

        mode = config.CONFIG.get("voice", {}).get("output_mode", "both")
        if mode in ("voice", "both") or force_voice:
            if wait:
                self._speaking.set()
                try:
                    self.speaker.speak(text)
                finally:
                    self._speaking.clear()
            else:
                self._enqueue_speech(text)

    # ------------------------------------------------------------------ input

    def handle_input(self, text, source="text", request_id=None):
        """Main entry for every user utterance. Returns the response text.

        ``wait`` behaviour: voice/console callers block while Harry speaks
        (so the microphone never records Harry's own voice); dashboard callers
        do not block, so an HTTP request never waits for audio playback.
        """
        text = (text or "").strip()
        if not text:
            return ""

        rid = request_id or uuid.uuid4().hex[:12]
        wait_speech = source not in ("ui", "ui-voice")
        t_start = time.perf_counter()
        log.info("[%s] request received (source=%s, len=%s)", rid, source, len(text))

        self.memory.conversation.add("user", text, meta={"source": source})
        events.emit("user_message", {"text": text, "source": source,
                                     "request_id": rid})
        low = text.lower()

        def finish(answer):
            # Record the answer for EVERY path (fast path, memory command, tool
            # task, model answer). Without this the history accumulated several
            # unanswered user turns and the model tried to answer all of them.
            self.memory.conversation.add("assistant", str(answer),
                                         meta={"request_id": rid})
            self.say(answer, request_id=rid, wait=wait_speech,
                     latency_ms=(time.perf_counter() - t_start) * 1000)
            log.info("[%s] answered in %.0fms", rid,
                     (time.perf_counter() - t_start) * 1000)
            return answer

        # ---- emergency / control commands (voice or text) ----
        if re.search(r"\b(emergency stop|emergency)\b", low):
            events.emergency_stop("user requested emergency stop")
            self.stop()
            answer = "Emergency stop. All operations halted."
            return finish(answer)
        if re.search(r"^\s*(harry[, ]*)?(stop|cancel|abort)\b", low) and self.busy:
            events.emergency_stop("user cancelled current work")
            return finish("Stopped.")
        if re.search(r"\b(what are you doing|status)\b", low) and self.busy:
            return finish(self._status_report())

        # ---- deterministic fast paths (never touch the model) ----
        fast = self._fast_reply(text, low)
        if fast is not None:
            log.info("[%s] fast path", rid)
            return finish(fast)

        # ---- memory commands ----
        response = self._memory_commands(low, text)
        if response is not None:
            return finish(response)

        # ---- system check ----
        if re.search(r"\b(system check|diagnostics|health check)\b", low):
            from .diagnostics import run_diagnostics
            report = run_diagnostics()
            return finish(report)

        # ---- task / question pipeline ----
        self.busy = True
        try:
            try:
                task = self.engine.create_task(text, request_id=rid)
                log.info("[%s] task %s created", rid, task["id"])
                result = self.engine.run_task(task["id"])
            except events.EmergencyStop:
                answer = "Okay, stopped that for you."
                return finish(answer)
            answer = result.get("answer") or result.get("result") \
                or self._summarize_result(result)
            if result.get("status") == "FAILED":
                answer = (f"I couldn't complete that. {result.get('error') or ''} "
                          "You can ask me to try again.").strip()
            return finish(answer)
        finally:
            self.busy = False
            events.clear_stop()

    # ------------------------------------------------------------------ helpers

    def _fast_reply(self, text, low):
        """Answer trivial requests locally: sub-millisecond, no model call."""
        name = (config.CONFIG.get("user_name") or "").strip()

        if _GREETING.match(text):
            hello = f"Hello{', ' + name if name else ''}!"
            return (hello + " How can I help?")
        if _THANKS.match(text):
            return "You're welcome!"
        if _BYE.match(text):
            return "Goodbye! Say the wake word when you need me."
        if _WHO.match(text):
            return ("I'm Harry, your personal AI assistant. I answer questions, "
                    "run tools on this computer and remember what you tell me.")
        if _TIME_Q.search(low):
            now = datetime.now()
            return "It's " + now.strftime("%I:%M %p").lstrip("0") + "."
        if _DATE_Q.search(low):
            return "Today is " + datetime.now().strftime("%A, %d %B %Y") + "."

        expr = _ARITH_PREFIX.sub("", text).strip().rstrip("?=").strip()
        if expr and _ARITH_EXPR.match(expr) and re.search(r"\d", expr) \
                and re.search(r"[+\-*/%^x×÷]", expr):
            value = _safe_arithmetic(expr)
            if value is not None:
                return f"{expr} = {_format_number(value)}"
        return None

    def _memory_commands(self, low, text):
        if low.startswith("remember") or "remember that" in low:
            payload = re.sub(r"^\s*remember\s+(that\s+)?", "", text, flags=re.IGNORECASE)
            mid = self.memory.store(payload, kind="fact", importance=2, source="user")
            return f"Okay, I'll remember that. (memory {mid})"
        if low.startswith("forget") and ("memory" in low or "forget that" in low
                                          or "forget about" in low):
            payload = re.sub(r"^\s*forget\s+(about\s+)?(that\s+)?", "", text,
                             flags=re.IGNORECASE)
            removed = self.memory.forget(text=payload) if payload else 0
            return ("Done, I forgot that." if removed
                    else "I don't have a memory like that.")
        if re.search(r"what do you (remember|know about me)", low):
            items = self.memory.longterm.all()
            if not items:
                return "I don't have anything stored yet. Tell me 'remember that ...'"
            top = "; ".join(i["text"][:80] for i in items[-5:])
            return f"Here's what I remember: {top}"
        return None

    def _status_report(self):
        active = [t for t in self.engine.list_tasks()
                  if t["status"] not in ("COMPLETED", "FAILED", "CANCELLED")]
        if not active:
            return "Nothing active right now."
        t = active[0]
        return f"Currently: {t['title'][:80]} (status {t['status']})"

    def _summarize_result(self, result):
        """Turn a finished task dict into a human report."""
        steps = result.get("steps") or []
        done = [s for s in steps if s.get("status") == "done"]
        lines = [f"Task '{result.get('title', '')}' finished - "
                 f"{len(done)}/{len(steps)} steps done."]
        for s in steps[:5]:
            mark = "[done]" if s.get("status") == "done" else "[fail]"
            lines.append(f" {mark} {s.get('description', '')[:100]}")
        return "\n".join(lines)


_brain = None


def get_brain():
    global _brain
    if _brain is None:
        _brain = Brain()
    return _brain

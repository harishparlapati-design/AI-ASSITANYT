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

from . import config, events, timing
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


_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


class _SpeechStream:
    """Speak complete sentences while the rest of the answer is generated.

    TTS runs on its own worker thread, so feeding sentences here never slows
    the model down. ``remainder()`` returns what was not spoken yet, which is
    all the final ``say`` call has to queue.
    """

    MAX_CHUNK = 220

    def __init__(self, speak, enabled=True):
        self._speak = speak
        self.enabled = bool(enabled)
        self.buf = ""
        self.spoken = 0

    def feed(self, piece):
        if not self.enabled or not piece:
            return
        self.buf += piece
        while True:
            match = _SENTENCE_END.search(self.buf)
            if match:
                cut = match.end()
            elif len(self.buf) >= self.MAX_CHUNK:
                cut = self.buf.rfind(" ", 0, self.MAX_CHUNK)
                if cut <= 0:
                    return
            else:
                return
            sentence, self.buf = self.buf[:cut].strip(), self.buf[cut:].lstrip()
            if sentence:
                self.spoken += len(sentence)
                try:
                    self._speak(sentence)
                except Exception as e:
                    log.debug("early speech failed: %s", e)

    def remainder(self):
        rest, self.buf = self.buf.strip(), ""
        return rest


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
        # Items queued but not finished. Checked by wait_speech(): the queue
        # alone is not enough, because the worker removes an item before it
        # starts speaking, which is exactly the window where the microphone
        # used to open while Harry was still about to talk.
        self._speech_pending = 0
        self._last_spoken = ""

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
            try:
                if not events.is_stopped():
                    self._speaking.set()
                    try:
                        self.speaker.speak(item)
                    except Exception as e:
                        log.warning("queued speech failed: %s", e)
                    finally:
                        self._speaking.clear()
            finally:
                with self._speech_lock:
                    self._speech_pending = max(0, self._speech_pending - 1)

    def _enqueue_speech(self, text):
        """Speak without blocking the caller (used by the dashboard)."""
        try:
            self._ensure_speech_worker()
            with self._speech_lock:
                self._speech_pending += 1
            self._speech_q.put(text)
        except Exception as e:
            with self._speech_lock:
                self._speech_pending = max(0, self._speech_pending - 1)
            log.warning("could not queue speech: %s", e)

    DELTA_FLUSH_SECONDS = 0.05

    def _delta_handler(self, rid, stream):
        """Forward answer fragments to the dashboard and to early speech."""
        state = {"buf": "", "at": 0.0, "visible": False}

        def on_delta(piece):
            if not piece:
                return
            stream.feed(piece)
            state["buf"] += piece
            now = time.perf_counter()
            if now - state["at"] < self.DELTA_FLUSH_SECONDS and len(state["buf"]) < 240:
                return
            state["at"] = now
            if not state["visible"]:
                state["visible"] = True
                timing.mark("first_visible")
            events.emit("assistant_delta", {"request_id": rid,
                                            "text": state["buf"]})
            state["buf"] = ""

        return on_delta

    def wait_speech(self, timeout=120.0):
        """Block until every queued sentence has finished playing.

        Returns True when the audio really is over. Callers that are about to
        open the microphone MUST honour this: it is the only thing standing
        between the user's ear and Harry transcribing himself.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._speech_lock:
                pending = self._speech_pending
            if pending == 0 and not self._speaking.is_set():
                return True
            time.sleep(0.02)
        return False

    def stop(self):
        """Emergency stop: cancel queued speech and halt current playback."""
        try:
            while self._speech_q is not None and not self._speech_q.empty():
                self._speech_q.get_nowait()
        except Exception:
            pass
        with self._speech_lock:
            self._speech_pending = 0
        self.speaker.stop_speaking()

    @property
    def speaking(self):
        """True while Harry is producing speech or has speech queued."""
        if self._speaking.is_set():
            return True
        with self._speech_lock:
            return self._speech_pending > 0

    # ------------------------------------------------------------------ output

    def say(self, text, force_voice=False, request_id=None, wait=True,
            latency_ms=None, speech_text=None):
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
            # speech_text lets a streamed answer speak only what has not already
            # been spoken sentence by sentence during generation.
            to_speak = text if speech_text is None else speech_text
            if str(to_speak).strip():
                # Remembered so barge-in can tell the user's voice from an echo
                # of Harry's own words.
                self._last_spoken = str(to_speak)
                # Always go through the worker: it keeps sentence order intact
                # when part of the answer was already spoken during generation.
                self._enqueue_speech(to_speak)
                if wait:
                    self.wait_speech()

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
        trace = timing.start(rid, source)
        log.info("[%s] request received (source=%s, len=%s)", rid, source, len(text))

        self.memory.conversation.add("user", text, meta={"source": source})
        events.emit("user_message", {"text": text, "source": source,
                                     "request_id": rid})
        low = text.lower()

        # ---- Core Brain: understand, route, open a decision record ----
        # Deterministic regex work only, so it costs no model round-trip. The
        # record (intent, reasons, chosen specialist, usable tools) is what
        # EDITH shows; it never contains private chain-of-thought.
        from . import communication, intelligence
        understanding, routing, pipeline_run = None, None, None
        try:
            understanding = intelligence.understand(text, source=source)
            routing = intelligence.route(understanding)
            pipeline_run = intelligence.start_run(understanding, routing, source)
            intelligence.add_stage(pipeline_run, "route", routing.get("why"))
        except Exception as e:      # the pipeline must never block a reply
            log.warning("core brain unavailable: %s", e)
        human = (understanding or {}).get("human") or {}
        guidance = (intelligence.instructions(understanding, routing)
                    if understanding else "")

        stream = None

        def finish(answer, executor="pipeline", status="ok"):
            # Record the answer for EVERY path (fast path, memory command, tool
            # task, model answer). Without this the history accumulated several
            # unanswered user turns and the model tried to answer all of them.
            answer = str(answer)
            # Human-aware shaping. A streamed answer may already be partly
            # spoken, and reshaping it then would desync the audio from the text.
            if stream is None or getattr(stream, "spoken", 0) == 0:
                answer = communication.shape_answer(
                    answer, human, (understanding or {}).get("intent"))
            self.memory.conversation.add("assistant", answer,
                                         meta={"request_id": rid})
            speech_rest = stream.remainder() if stream is not None else None
            self.say(answer, request_id=rid, wait=wait_speech,
                     latency_ms=(time.perf_counter() - t_start) * 1000,
                     speech_text=speech_rest)
            trace.mark("delivered")
            trace.finish()
            if pipeline_run:
                try:
                    intelligence.finish_run(
                        pipeline_run, status=status, executor=executor,
                        answer=answer,
                        ms=(time.perf_counter() - t_start) * 1000)
                except Exception as e:
                    log.debug("run record not saved: %s", e)
            log.info("[%s] answered in %.0fms", rid,
                     (time.perf_counter() - t_start) * 1000)
            return answer

        # ---- emergency / control commands (voice or text) ----
        if re.search(r"\b(emergency stop|emergency)\b", low):
            events.emergency_stop("user requested emergency stop")
            self.stop()
            answer = "Emergency stop. All operations halted."
            return finish(answer, "control")
        if re.search(r"^\s*(harry[, ]*)?(stop|cancel|abort)\b", low) and self.busy:
            events.emergency_stop("user cancelled current work")
            return finish("Stopped.", "control")
        if re.search(r"\b(what are you doing|status)\b", low) and self.busy:
            return finish(self._status_report(), "status")

        # ---- administrator commands ----
        # Recognition is deterministic, and the *verified* elevation from the web
        # session decides whether a command may run. A sentence never elevates.
        from . import admin_commands
        admin_reply = admin_commands.handle(text)
        if admin_reply is not None:
            return finish(admin_reply, "admin_command")

        # ---- conversational self-development ----
        # A request to change Harry becomes a tracked development run with an
        # approval gate; nothing in the running checkout is touched here.
        if understanding and understanding.get("intent") == intelligence.DEVELOPMENT:
            from . import devwork
            actor = None
            try:
                from . import admin
                actor = (admin.request_admin() or {}).get("user_id")
            except Exception:
                actor = None
            proposed = devwork.propose(text, requested_by=actor)
            if proposed.get("ok"):
                record = proposed["run"]
                answer = devwork.summary_for_chat(record, human)
                if pipeline_run:
                    intelligence.add_stage(pipeline_run, "plan",
                                           f"development run {record['id']}")
                events.emit("development_proposed",
                            {"run_id": record["id"],
                             "status": record["status"]})
            else:
                answer = ("I could not open a development run: "
                          + str(proposed.get("error") or "unknown error"))
            return finish(answer, "devwork")

        # ---- deterministic fast paths (never touch the model) ----
        fast = self._fast_reply(text, low)
        if fast is not None:
            from .language_policy import general_fast_reply
            preference = config.CONFIG.get("language", {}).get("preference", "auto")
            fast = general_fast_reply(fast, text, preference)
            timing.mark("fast_path")
            log.info("[%s] fast path", rid)
            return finish(fast, "fast_path")

        # ---- memory commands ----
        response = self._memory_commands(low, text)
        if response is not None:
            return finish(response, "memory")

        # ---- system check ----
        if re.search(r"\b(system check|diagnostics|health check)\b", low):
            from .diagnostics import run_diagnostics
            report = run_diagnostics()
            return finish(report, "diagnostics")

        # ---- task / question pipeline ----
        # Speech starts as soon as the first sentence exists instead of after
        # the whole answer, and generation never waits for audio.
        mode = config.CONFIG.get("voice", {}).get("output_mode", "both")
        stream = _SpeechStream(self._enqueue_speech,
                               enabled=mode in ("voice", "both"))
        self.busy = True
        try:
            try:
                task = self.engine.create_task(text, request_id=rid,
                                               understanding=understanding)
                timing.mark("task_created")
                log.info("[%s] task %s created", rid, task["id"])
                result = self.engine.run_task(
                    task["id"], on_delta=self._delta_handler(rid, stream))
            except events.EmergencyStop:
                answer = "Okay, stopped that for you."
                return finish(answer, "control", "cancelled")
            answer = result.get("answer") or result.get("result") \
                or self._summarize_result(result)
            # ---- observe / verify / recover evidence ----
            if pipeline_run:
                steps = result.get("steps") or []
                intelligence.add_stage(pipeline_run, "execute",
                                       f"{len(steps)} step(s)")
                for step in steps[:6]:
                    intelligence.add_evidence(pipeline_run, {
                        "step": str(step.get("description"))[:120],
                        "status": step.get("status"),
                        "result": str(step.get("result"))[:200]})
                intelligence.add_stage(pipeline_run, "verify",
                                       result.get("status"))
            if result.get("status") == "FAILED":
                if pipeline_run:
                    intelligence.add_stage(pipeline_run, "recover",
                                           str(result.get("error"))[:160])
                answer = (f"I couldn't complete that. {result.get('error') or ''} "
                          "You can ask me to try again.").strip()
                return finish(answer, "task_engine", "failed")
            return finish(answer, "task_engine")
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

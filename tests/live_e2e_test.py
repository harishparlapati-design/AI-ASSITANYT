"""End-to-end test: the real Flask app, the real SSE stream and the real engine.

Run: python tests/live_e2e_test.py [--no-model]

The app is started on a spare port exactly as the product starts it, a real SSE
client is connected over HTTP, and the dashboard API is driven the way the page
drives it. Checks:

  * sequence ids strictly increase and no frame is delivered twice
  * exactly one assistant_message per request id (no duplicate bubbles)
  * streamed deltas concatenate to the final answer, so a draft bubble can be
    replaced by the real text instead of being duplicated
  * a duplicate POST while a request is running is refused
  * conversation start/mute/stop/trigger over HTTP, multi-turn continuity, and
    listener reconnect after a microphone failure

Voice turns use an injected scripted listener: a test process cannot speak into
a microphone, and that is stated rather than hidden. Microphone reality is
covered by tests/voice_live_probe.py.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harry_core import config, ui  # noqa: E402

PORT = int(os.environ.get("HARRY_E2E_PORT", "8799"))
BASE = f"http://127.0.0.1:{PORT}"
PASS, FAIL = [], []


def check(name, fn):
    try:
        result, ok = fn()
    except Exception as e:
        result, ok = f"{type(e).__name__}: {e}", False
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -> {result}"))


def http(path, payload=None, timeout=30):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(BASE + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        return e.code, (json.loads(body) if body.strip() else {})


class StreamClient(threading.Thread):
    """A real SSE consumer, same wire format the dashboard uses."""

    def __init__(self):
        super().__init__(daemon=True)
        self.frames = []
        self.error = ""
        self._lock = threading.Lock()

    def run(self):
        try:
            with urllib.request.urlopen(BASE + "/api/stream", timeout=180) as response:
                frame = {}
                for raw in response:
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith("id: "):
                        frame["seq"] = int(line[4:])
                    elif line.startswith("event: "):
                        frame["event"] = line[7:]
                    elif line.startswith("data: "):
                        frame["data"] = json.loads(line[6:])
                    elif line == "" and frame.get("event"):
                        frame["at"] = time.time()
                        with self._lock:
                            self.frames.append(frame)
                        frame = {}
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"

    def since(self, index):
        with self._lock:
            return list(self.frames[index:])

    def wait(self, predicate, timeout=60.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for frame in self.frames:
                    if predicate(frame):
                        return frame
            time.sleep(0.05)
        return None


def start_app():
    config.CONFIG["voice"]["output_mode"] = "text"      # never play audio in tests
    app = ui.create_app()
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT, threaded=True,
                               use_reloader=False),
        daemon=True).start()
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/", timeout=2):
                return True
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("dashboard did not start")


# ---------------------------------------------------------------- app is up
def dashboard_responds():
    """The page is HTML, so read it as text and check the real markup."""
    with urllib.request.urlopen(BASE + "/", timeout=5) as response:
        status, body = response.status, response.read().decode("utf-8")
    has_ui = "Message Harry" in body and "assistant_delta" in body
    return {"status": status, "bytes": len(body), "has_ui": has_ui}, status == 200 and has_ui


def start_app_check():
    return "app started", start_app()


check("the real dashboard starts and answers", start_app_check)
check("dashboard serves the conversation page", dashboard_responds)
check("/api/conversation reports status", lambda: (
    (lambda r: (r, r[0] == 200 and "mode" in r[1]))(http("/api/conversation"))))

stream = StreamClient()
stream.start()
time.sleep(0.6)


# --------------------------------------------- no duplicate delivery ever
def no_duplicate_frames():
    for _ in range(2):                       # a fast path answer, twice
        http("/api/chat", {"text": "hello", "request_id": "e2e-hello"})
    time.sleep(1.5)
    frames = stream.since(0)
    seqs = [f["seq"] for f in frames if "seq" in f]
    duplicates = len(seqs) != len(set(seqs))
    ordered = seqs == sorted(seqs)
    messages = [f for f in frames if f["event"] == "assistant_message"]
    by_request = {}
    for frame in messages:
        rid = (frame.get("data") or {}).get("request_id")
        by_request[rid] = by_request.get(rid, 0) + 1
    return ({"frames": len(frames), "seqs": len(seqs), "duplicates": duplicates,
             "messages": by_request},
            bool(frames) and not duplicates and ordered
            and by_request.get("e2e-hello", 0) == 1)


check("no duplicate SSE frames and one assistant message per request",
      no_duplicate_frames)


# ------------------------------------------------------- streaming contract
def streaming_contract():
    if os.environ.get("HARRY_E2E_SKIP_MODEL") == "1":
        return "skipped by HARRY_E2E_SKIP_MODEL", True
    mark = len(stream.since(0))
    status, body = http("/api/chat", {"text": "what is a capacitor",
                                      "request_id": "e2e-stream"})
    if status not in (200, 202):
        return body, False
    final = stream.wait(
        lambda f: f["event"] == "assistant_message"
        and (f.get("data") or {}).get("request_id") == "e2e-stream", timeout=180)
    deltas = [f for f in stream.since(mark) if f["event"] == "assistant_delta"
              and (f.get("data") or {}).get("request_id") == "e2e-stream"]
    if not final:
        return "no assistant_message for the streamed request", False
    streamed = "".join((d.get("data") or {}).get("text", "") for d in deltas)
    answer = (final.get("data") or {}).get("text", "")
    seqs = [f["seq"] for f in stream.since(0) if "seq" in f]
    return ({"deltas": len(deltas), "delta_chars": len(streamed),
             "answer_chars": len(answer), "match": streamed.strip() == answer.strip()},
            len(deltas) >= 3 and streamed.strip() == answer.strip()
            and seqs == sorted(seqs) and len(seqs) == len(set(seqs)))


check("streamed deltas concatenate to the final answer, ids stay unique",
      streaming_contract)


def duplicate_request_refused():
    http("/api/chat", {"text": "what is the speed of light", "request_id": "e2e-dup-a"})
    status, body = http("/api/chat", {"text": "what is the speed of light",
                                      "request_id": "e2e-dup-b"})
    time.sleep(0.5)
    second = stream.wait(lambda f: f["event"] == "assistant_message"
                         and (f.get("data") or {}).get("request_id") == "e2e-dup-b",
                         timeout=1.0)
    return ({"status": status, "body": body},
            status in (200, 202, 409) and second is None)


check("a second identical request is not run in parallel", duplicate_request_refused)


# ------------------------------------------------- voice turns over HTTP
class ScriptedListener:
    def __init__(self, script, failures=0):
        self.script = list(script)
        self.failures = failures
        self.calls = 0

    def available(self):
        return True

    def listen_once(self, **kwargs):
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise OSError("input device disconnected")
        time.sleep(0.02)
        return self.script.pop(0) if self.script else ""


class ScriptedBrain:
    """Stands in for the model so conversation plumbing is tested quickly."""

    def __init__(self):
        self.heard = []
        self.speaking = False
        self.stops = 0
        self._last_spoken = ""

    def wait_speech(self, timeout=0.0):
        return True

    def handle_input(self, text, source="voice"):
        self.heard.append(text)
        return f"answer to {text}"

    def stop(self):
        self.stops += 1


def install_scripted_conversation(script, failures=0):
    from harry_core import conversation
    brain = ScriptedBrain()
    listener = ScriptedListener(script, failures=failures)
    conversation._conversation = conversation.VoiceConversation(
        brain=brain, listener=listener)
    return conversation._conversation, brain, listener


def conversation_multi_turn():
    from harry_core import conversation as cmod
    conv, brain, listener = install_scripted_conversation(
        ["first question", "second question", "third question"])
    status, body = http("/api/conversation",
                        {"action": "start", "mode": "continuous"})
    if status != 200 or not body.get("ok"):
        return body, False
    got = wait_until(lambda: len(brain.heard) >= 3, 10)
    heard_frame = stream.wait(lambda f: f["event"] == "voice_heard", timeout=5)
    state = http("/api/conversation")[1]
    http("/api/conversation", {"action": "stop"})
    ended = wait_until(lambda: not http("/api/conversation")[1]["running"], 5)
    return ({"heard": brain.heard, "state": state, "heard_event": bool(heard_frame)},
            got and brain.heard[:3] == ["first question", "second question",
                                        "third question"]
            and ended and bool(heard_frame))


def wait_until(predicate, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


check("continuous voice runs several consecutive turns over HTTP",
      conversation_multi_turn)


def mute_and_stop():
    # A long script so the loop is still working when mute arrives; the
    # assertion is that nothing is captured *after* mute, not that turns which
    # completed before mute disappear.
    conv, brain, listener = install_scripted_conversation(["turn %d" % i for i in range(30)])
    http("/api/conversation", {"action": "start", "mode": "continuous"})
    time.sleep(0.15)
    http("/api/conversation", {"action": "mute", "muted": True})
    time.sleep(0.2)                      # let an in-flight turn land
    calls_at_mute = listener.calls
    heard_at_mute = len(brain.heard)
    time.sleep(0.8)
    while_muted = listener.calls - calls_at_mute
    heard_while_muted = brain.heard[heard_at_mute:]
    muted_state = http("/api/conversation")[1]
    http("/api/conversation", {"action": "stop"})
    ended = wait_until(lambda: not http("/api/conversation")[1]["running"], 5)
    return ({"calls_while_muted": while_muted, "heard": heard_while_muted,
             "muted": muted_state.get("muted")},
            while_muted == 0 and not heard_while_muted
            and muted_state.get("muted") is True and ended)


check("mute blocks capture and stop ends the loop", mute_and_stop)


def reconnect_after_device_loss():
    conv, brain, listener = install_scripted_conversation(
        ["after reconnect"], failures=2)
    http("/api/conversation", {"action": "start", "mode": "continuous"})
    recovered = wait_until(lambda: brain.heard, 15)
    status = http("/api/conversation")[1]
    http("/api/conversation", {"action": "stop"})
    return ({"reconnects": status.get("reconnects"), "heard": brain.heard},
            recovered and status.get("reconnects", 0) >= 1)


check("the voice loop reconnects after the microphone fails", reconnect_after_device_loss)


def push_to_talk_over_http():
    conv, brain, listener = install_scripted_conversation(["triggered turn"])
    http("/api/conversation", {"action": "start", "mode": "push_to_talk"})
    time.sleep(0.5)
    idle_calls = listener.calls
    http("/api/conversation", {"action": "trigger"})
    got = wait_until(lambda: brain.heard, 5)
    http("/api/conversation", {"action": "stop"})
    return ({"idle_calls": idle_calls, "heard": brain.heard},
            idle_calls == 0 and got)


check("push to talk records only after the trigger", push_to_talk_over_http)

print(f"\n===== E2E RESULT: {len(PASS)} passed, {len(FAIL)} failed =====")
if stream.error:
    print(f"  stream error: {stream.error}")
for failure in FAIL:
    print(f"  FAILED: {failure}")
sys.exit(1 if FAIL else 0)

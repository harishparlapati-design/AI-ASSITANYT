"""Dashboard tests - duplication, latency paths and the voice request pipeline.

Run: python tests/dashboard_test.py

Uses a fake LLM (no Ollama needed) and a fake speaker (no audio hardware), so
the checks are deterministic. Nothing here touches a microphone: browser
microphone behaviour is verified separately in the live page.
"""

import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


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


# --------------------------------------------------------------- fake model
from harry_core import llm, events  # noqa: E402

LLM_CALLS = []


def fake_chat(messages, task_class=llm.CHAT, options=None, json_mode=False):
    LLM_CALLS.append({"class": task_class, "json": json_mode})
    prompt = messages[-1]["content"] if messages else ""
    if json_mode:
        if "Classify what the user wants" in prompt:
            return '{"type": "question", "goal": "asked a question"}'
        if "verified" in prompt:
            return '{"verified": true}'
        if "Break the GOAL" in prompt:
            return '{"steps": [{"description": "do it", "tool": null, "args": {}}]}'
        return '{"action": "answer", "content": "step done"}'
    return "FAKE_ANSWER from the model"


llm.chat = fake_chat


def fake_chat_stream(messages, task_class=llm.CHAT, options=None, json_mode=False):
    """Answers are streamed now: yield the same canned answer once."""
    yield fake_chat(messages, task_class=task_class, options=options,
                    json_mode=json_mode)


llm.chat_stream = fake_chat_stream

# --------------------------------------------------------------- app under test
from harry_core import config  # noqa: E402
from harry_core.brain import get_brain  # noqa: E402
from harry_core.ui import create_app  # noqa: E402
from harry_core.speech import get_listener  # noqa: E402
from harry_core.task_engine import get_engine  # noqa: E402

config.CONFIG["voice"]["output_mode"] = "both"

brain = get_brain()
SPOKEN = []
brain.speaker.enabled = True
brain.speaker.speak = lambda text, output_mode=None: SPOKEN.append(text)

MESSAGES = []
events.subscribe(lambda e, d: MESSAGES.append(d), event="assistant_message")

app = create_app()
client = app.test_client()
TOKEN = uuid.uuid4().hex[:8]


def fresh():
    # Let any speech queued by the previous test drain before clearing the
    # counters, otherwise a late reply lands in the next test's window.
    deadline = time.time() + 5
    while time.time() < deadline:
        q = getattr(brain, "_speech_q", None)
        if q is None or q.empty():
            break
        time.sleep(0.01)
    time.sleep(0.02)
    LLM_CALLS.clear()
    del MESSAGES[:]
    del SPOKEN[:]


def ask(text, rid=None, **extra):
    payload = {"text": text, "request_id": rid or uuid.uuid4().hex, "wait": True}
    payload.update(extra)
    return client.post("/api/chat", json=payload)


def wait_speech(timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not SPOKEN:
        time.sleep(0.02)


# --------------------------------------------------------------- fast paths
def test_greeting_fast_path():
    fresh()
    t0 = time.perf_counter()
    r = ask("hello")
    ms = (time.perf_counter() - t0) * 1000
    j = r.get_json()
    wait_speech()
    return (r.status_code == 200 and j["status"] == "done"
            and "Hello" in j["response"]
            and len(LLM_CALLS) == 0          # no model round-trip
            and len(MESSAGES) == 1           # exactly one answer event
            and len(SPOKEN) == 1             # spoken exactly once
            and ms < 200)


def test_time_fast_path():
    fresh()
    j = ask("what is the time").get_json()
    return j["status"] == "done" and "It's" in j["response"] and ":" in j["response"] \
        and len(LLM_CALLS) == 0


def test_arithmetic_fast_path():
    fresh()
    a = ask("what is 12*12?").get_json()["response"]
    b = ask("2+2").get_json()["response"]
    return "144" in a and "4" in b and len(LLM_CALLS) == 0


def test_greeting_does_not_create_task():
    before = len(get_engine().list_tasks())
    fresh()
    ask("hi")
    after = len(get_engine().list_tasks())
    return before == after


# --------------------------------------------------------------- question pipeline
def test_question_pipeline_single_answer():
    fresh()
    text = f"what is python used for {TOKEN}"
    r = ask(text)
    j = r.get_json()
    wait_speech()
    created = [t for t in get_engine().list_tasks() if t["title"] == text]
    return (j["status"] == "done" and "FAKE_ANSWER" in j["response"]
            and len(LLM_CALLS) == 1          # classification was rule-based
            and len(MESSAGES) == 1
            and len(SPOKEN) == 1
            and len(created) == 1)


def test_answer_event_carries_request_id():
    fresh()
    rid = "rid-" + TOKEN
    ask("what is a capacitor", rid=rid)
    return (len(MESSAGES) == 1 and MESSAGES[0].get("request_id") == rid
            and MESSAGES[0].get("id"))


def test_conversation_has_one_user_turn():
    fresh()
    text = f"what is a resistor {TOKEN}"
    ask(text)
    path = os.path.join("data", "conversation.jsonl")
    with open(path, "r", encoding="utf-8") as f:
        lines = [l for l in f if text in l]
    return len(lines) == 1


# --------------------------------------------------------------- duplication
def test_same_request_id_runs_once():
    fresh()
    rid = "same-" + TOKEN
    text = f"what is a diode {TOKEN}"
    first = ask(text, rid=rid).get_json()
    second = client.post("/api/chat", json={"text": text,
                                            "request_id": rid, "wait": True}).get_json()
    created = [t for t in get_engine().list_tasks() if t["title"] == text]
    return (first["status"] == "done" and second["id"] == rid
            and len(MESSAGES) == 1 and len(created) == 1)


def test_async_accepts_immediately_then_reports_once():
    fresh()
    rid = "async-" + TOKEN
    t0 = time.perf_counter()
    r = client.post("/api/chat", json={"text": f"what is an inductor {TOKEN}",
                                       "request_id": rid})
    ms = (time.perf_counter() - t0) * 1000
    j = r.get_json()
    accepted_fast = r.status_code == 202 and j["status"] == "running" \
        and j["response"] is None and ms < 200
    done = None
    for _ in range(200):
        done = client.get("/api/request/" + rid).get_json()
        if done["status"] != "running":
            break
        time.sleep(0.02)
    wait_speech()
    time.sleep(0.1)
    ok = (accepted_fast and done and done["status"] == "done"
          and len(MESSAGES) == 1 and len(SPOKEN) == 1)
    return ok or (r.status_code, round(ms, 1), j.get("status"),
                  (done or {}).get("status"), len(MESSAGES), len(SPOKEN))


def test_unknown_request_404():
    return client.get("/api/request/does-not-exist").status_code == 404


def test_empty_text_rejected():
    return client.post("/api/chat", json={"text": "   "}).status_code == 400


# --------------------------------------------------------------- task identity
def test_task_ids_unique_and_request_idempotent():
    engine = get_engine()
    a = engine.create_task("TEST task id A " + TOKEN)
    b = engine.create_task("TEST task id B " + TOKEN)
    rid = "tid-" + TOKEN
    c1 = engine.create_task("TEST task id C " + TOKEN, request_id=rid)
    c2 = engine.create_task("TEST task id C " + TOKEN, request_id=rid)
    for tid in (a["id"], b["id"], c1["id"]):
        engine.tasks.pop(tid, None)
    engine._save()
    return a["id"] != b["id"] and c1["id"] == c2["id"]


# --------------------------------------------------------------- stop / stt
def test_emergency_stop_endpoint():
    fresh()
    r = client.post("/api/stop")
    stopped = events.is_stopped()
    events.clear_stop()
    return r.status_code == 200 and stopped


def test_stt_route_uses_local_model():
    listener = get_listener()
    original = listener.transcribe_pcm
    listener.transcribe_pcm = lambda data, samplerate=16000: "HEARD TEXT"
    try:
        r = client.post("/api/stt", data=b"\x00" * 3200)
        j = r.get_json()
        empty = client.post("/api/stt", data=b"")
        return r.status_code == 200 and j["text"] == "HEARD TEXT" \
            and empty.status_code == 400
    finally:
        listener.transcribe_pcm = original


def test_stt_warm_is_idempotent():
    a = client.post("/api/stt/warm").get_json()
    b = client.post("/api/stt/warm").get_json()
    return a["started"] and b["started"]


def test_voice_and_text_share_pipeline():
    fresh()
    text = f"what is a thyristor {TOKEN}"
    via_text = ask(text, rid="txt-" + TOKEN).get_json()
    via_voice = ask(text, rid="voi-" + TOKEN, source="ui-voice").get_json()
    wait_speech()
    time.sleep(0.1)
    ok = (via_text["status"] == "done" and via_voice["status"] == "done"
          and via_text["response"] == via_voice["response"]
          and len(MESSAGES) == 2 and len(SPOKEN) == 2)
    return ok or (via_text["status"], via_voice["status"],
                  len(MESSAGES), len(SPOKEN))


TESTS = [
    ("greeting answered locally, in <200ms, one answer, one speech",
     test_greeting_fast_path),
    ("time query answered locally", test_time_fast_path),
    ("arithmetic answered locally and correctly", test_arithmetic_fast_path),
    ("greeting creates no task", test_greeting_does_not_create_task),
    ("question pipeline: one LLM call, one answer, one speech",
     test_question_pipeline_single_answer),
    ("answer event carries request id", test_answer_event_carries_request_id),
    ("conversation stores one user turn per request",
     test_conversation_has_one_user_turn),
    ("repeated request id runs once", test_same_request_id_runs_once),
    ("async chat returns at once, then reports exactly once",
     test_async_accepts_immediately_then_reports_once),
    ("unknown request id is 404", test_unknown_request_404),
    ("empty text is rejected", test_empty_text_rejected),
    ("task ids unique, request id idempotent",
     test_task_ids_unique_and_request_idempotent),
    ("emergency stop endpoint halts and flags", test_emergency_stop_endpoint),
    ("/api/stt transcribes through the local listener",
     test_stt_route_uses_local_model),
    ("/api/stt/warm is idempotent", test_stt_warm_is_idempotent),
    ("voice and typed input share one pipeline", test_voice_and_text_share_pipeline),
]

print("\n--- dashboard tests ---")
for name, fn in TESTS:
    check(name, fn)

# tidy up: remove tasks this test file created (now and in earlier runs) so the
# dashboard stays readable for the user.
import re  # noqa: E402

_engine = get_engine()
_stems = re.compile(
    r"^(?:what is (?:python used for|a (?:thyristor|diode|resistor|inductor|capacitor)"
    r"|an inductor|an diode|a diode)|TEST task id [ABC])\s*[0-9a-f]{8}$")
_removed = 0
for _tid, _task in list(_engine.tasks.items()):
    _title = _task.title or ""
    if TOKEN in _title or _stems.match(_title):
        _engine.tasks.pop(_tid, None)
        _removed += 1
if _removed:
    _engine._save()
    print(f"(cleaned up {_removed} test tasks)")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)

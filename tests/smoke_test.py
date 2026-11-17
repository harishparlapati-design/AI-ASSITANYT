"""Smoke tests for Harry core subsystems. Run: python tests/smoke_test.py"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL, SKIP = [], [], []


def check(name, fn):
    try:
        result = fn()
        status = "PASS" if result else "FAIL"
        (PASS if result else FAIL).append(name)
    except Exception as e:
        result = f"{type(e).__name__}: {e}"
        status = "FAIL"
        FAIL.append(f"{name} ({result})")
    print(f"[{status}] {name}" + (f" -> {result}" if result is not True and status == "PASS" else ""))


def skip(name, why):
    """Record a check that cannot run here. Never counts as a pass."""
    SKIP.append(f"{name} ({why})")
    print(f"[SKIP] {name} -> {why}")


# ---------------------------------------------------------------- config
from harry_core import config

check("config loads with defaults", lambda: config.CONFIG["models"]["local"]["reasoning_model"] == "qwen3:4b")

# ---------------------------------------------------------------- event bus / stop
from harry_core import events

got = []
events.subscribe(lambda e, d: got.append((e, d)), event="test_event")
events.emit("test_event", {"x": 1})
check("event bus delivers", lambda: got and got[0][0] == "test_event")

events.emergency_stop("test")
check("emergency stop flag", lambda: events.is_stopped())
events.clear_stop()
check("clear stop", lambda: not events.is_stopped())

# ---------------------------------------------------------------- memory
from harry_core.memory import get_memory

mem = get_memory()
mid = mem.store("Hareesh's favourite language is Telugu", kind="fact", importance=2)
check("memory store", lambda: mid is not None)
hits = mem.search("favourite language")
check("memory search finds it", lambda: any("Telugu" in h["text"] for h in hits))
check("memory dedup/update", lambda: mem.store("Hareesh's favourite language is Telugu") is not None
      and len([i for i in mem.longterm.all() if "favourite language" in i["text"]]) == 1)
removed = mem.forget(text="favourite language is Telugu")
check("memory forget", lambda: removed >= 1)
mem.conversation.add("user", "hello test")
check("conversation add/recent", lambda: mem.conversation.recent(1)[0]["text"] == "hello test")
mem.conversation.clear()

# ---------------------------------------------------------------- language detection
from harry_core.speech import detect_language, style_reply_hint

d1 = detect_language("Harry, na portfolio project open chesi latest error check cheyyi.")
check("roman Telugu+English detected", lambda: d1["mix"]["telugu"] > 0 and d1["mix"]["english"] > 0)
d2 = detect_language("నా పేరు హరీష్")
check("telugu script detected", lambda: d2["has_telugu_script"])
d3 = detect_language("aaj mujhe project kholo aur error dikhao")
check("roman hindi detected", lambda: d3["mix"]["hindi"] > 0)
d4 = detect_language("what time is it")
check("english detected", lambda: d4["primary"] == "english")
check("style hint mirrors mix", lambda: "Telugu" in style_reply_hint(
    "na portfolio open chesi error check cheyyi"))

# ---------------------------------------------------------------- security
from harry_core.security import classify, SAFE, CONTROLLED, SENSITIVE, get_permissions

check("classify read as safe", lambda: classify("read_file /tmp/x") == SAFE)
check("classify write as controlled", lambda: classify("write_file data.txt") == CONTROLLED)
check("classify delete as sensitive", lambda: classify("delete_file secret.txt") == SENSITIVE)

pm = get_permissions()
answers = []
pm.set_confirmer(lambda action, level: answers.append((action, level)) or True)
pm.check("delete the folder", level=SENSITIVE)
check("sensitive requires confirmation", lambda: len(answers) == 1)

# ---------------------------------------------------------------- tools
from harry_core.tools import get_registry

reg = get_registry()
names = reg.names()
check("builtin tools registered", lambda: "read_file" in names and "terminal" in names
      and "web_search" in names and "memory_store" in names and "computer_control" in names)
check("legacy tools loaded", lambda: any(n.startswith("legacy_") for n in names))

r = reg.run("write_file", {"path": "data/_test_file.txt", "content": "hello harry"})
check("write_file works", lambda: r.get("ok"))
r = reg.run("read_file", {"path": "data/_test_file.txt"})
check("read_file works", lambda: r.get("ok") and "hello harry" in r.get("content", ""))
r = reg.run("search_files", {"directory": "data", "pattern": "_test_file.txt"})
check("file_search works", lambda: r.get("ok") and r.get("count") >= 1)
r = reg.run("edit_file", {"path": "data/_test_file.txt", "old_text": "hello", "new_text": "hi"})
check("file_edit works", lambda: r.get("ok"))
r = reg.run("system_info", {})
check("system_info works", lambda: r.get("ok") and "ram_used_percent" in r.get("system", {}))
r = reg.run("run_python", {"code": "print(2+3)"})
check("run_python works", lambda: r.get("ok") and "5" in r.get("stdout", ""))
r = reg.run("terminal", {"command": "echo smoketest"})
check("terminal works", lambda: r.get("ok") and "smoketest" in r.get("stdout", ""))
r = reg.run("memory_store", {"text": "smoketest memory item"})
check("memory_store tool works", lambda: r.get("ok"))
r = reg.run("memory_search", {"query": "smoketest"})
check("memory_search tool works", lambda: r.get("ok") and len(r.get("results")) >= 1)
r = reg.run("delete_file", {"path": "data/_test_file.txt"})
check("delete_file gated+works", lambda: r.get("ok") or r.get("denied"))
r = reg.run("no_such_tool", {})
check("unknown tool returns error", lambda: not r.get("ok"))

# ---------------------------------------------------------------- legacy tools compat
r = reg.run("legacy_battery", {"command": "what is my battery"})
check("legacy battery tool responds", lambda: r.get("ok") and r.get("handled") in (True, False))
r = reg.run("legacy_calculator", {"command": "calculate 6 times 7"})
check("legacy calculator works", lambda: r.get("ok") and "42" in str(r.get("result", "")))

# ---------------------------------------------------------------- scheduler
from harry_core.scheduler import get_scheduler

sch = get_scheduler()
rid = sch.add_reminder("test reminder", delay_minutes=0.03)
import time as _t
_t.sleep(2.5)
sch._tick()
check("scheduler reminder fires", lambda: all(r["done"] for r in sch.pending() if r["id"] == rid)
      or not [r for r in sch.pending() if r["id"] == rid])

# ---------------------------------------------------------------- LLM router
from harry_core import llm

models = llm.get_router().local_models()
print(f"  local models: {models}")
check("ollama reachable", lambda: isinstance(models, list))

# Checks that genuinely need a live model only run when one is installed; a
# machine (or CI runner) without Ollama reports SKIP instead of a false pass.
LIVE_LLM = bool(models)

# ---------------------------------------------------------------- task engine (live model)
from harry_core.task_engine import get_engine

engine = get_engine()
if LIVE_LLM:
    kind, goal = engine.classify_request("what is python used for")
    check("classify question", lambda: kind == "question")
    kind2, goal2 = engine.classify_request("create a file called notes.txt with hello")
    check("classify task", lambda: kind2 == "task")
else:
    skip("classify question", "no local model installed")
    skip("classify task", "no local model installed")

# full Q&A pipeline through the brain
from harry_core.brain import get_brain
brain = get_brain()

import io
from contextlib import redirect_stdout
buf = io.StringIO()
if LIVE_LLM:
    with redirect_stdout(buf):
        answer = brain.handle_input("Reply with exactly: HARRY_SMOKETEST_OK", source="test")
    check("brain Q&A pipeline (live LLM)", lambda: "HARRY_SMOKETEST_OK" in str(answer))

    # full task pipeline: file creation through planner+executor
    with redirect_stdout(buf):
        result = brain.handle_input(
            "create a file named harry_agent_test.txt in the data folder containing the text HARRY_AGENT_WORKS",
            source="test")
    created = os.path.exists(os.path.join("data", "harry_agent_test.txt"))
    check("autonomous task: file created end-to-end", lambda: created)
    if created:
        content = open(os.path.join("data", "harry_agent_test.txt")).read()
        print(f"  file content: {content.strip()[:60]!r}")
        os.remove(os.path.join("data", "harry_agent_test.txt"))
else:
    skip("brain Q&A pipeline (live LLM)", "no local model installed")
    skip("autonomous task: file created end-to-end", "no local model installed")

# ---------------------------------------------------------------- diagnostics
from harry_core.diagnostics import run_diagnostics
report = run_diagnostics()
print(report)
check("diagnostics runs", lambda: "Harry System Check" in report)

# ---------------------------------------------------------------- resume/persistence
tasks = engine.list_tasks()
check("tasks persisted", lambda: len(tasks) >= 2)

print(f"\n===== RESULT: {len(PASS)} passed, {len(FAIL)} failed, "
      f"{len(SKIP)} skipped =====")
for f in FAIL:
    print(f"  FAILED: {f}")
for s in SKIP:
    print(f"  SKIPPED: {s}")
sys.exit(1 if FAIL else 0)

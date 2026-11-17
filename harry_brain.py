"""Backward-compatible brain facade.

The original harry.py (and the legacy tools) import functions from
harry_brain. They are preserved here, now backed by the new modular core:
same names, better implementation. New code should import harry_core instead.
"""

import json
import os

from harry_core import llm
from harry_core.memory import get_memory

MEMORY_FILE = "memory.json"   # legacy location, kept in sync for old tools


def ask_harry(question):
    """Answer a question through the modular brain (replaces old Ollama call)."""
    try:
        from harry_core.task_engine import get_engine
        task = get_engine().create_task(question)
        result = get_engine().run_task(task["id"])
        return result.get("answer") or result.get("result") or ""
    except Exception as e:
        return f"I had trouble answering that: {e}"


def detect_intent(command):
    """Legacy intent names kept for compatibility, via the router's fast model."""
    prompt = (
        "Classify the user command. Possible intents: open_app, play_music, "
        "tell_time, general_question, search_web, remember.\n"
        f"Command: {command}\nRespond with only the intent name."
    )
    try:
        return llm.chat([{"role": "user", "content": prompt}],
                        task_class=llm.ROUTING,
                        options={"num_predict": 10}).strip().lower()
    except Exception:
        return "general_question"


def save_memory(key, value):
    """Legacy key/value memory API, now stored in the layered memory."""
    get_memory().store(f"{key} is {value}", kind="fact", source="legacy",
                       importance=2)
    _sync_legacy_file()


def get_memory_value(key):
    hits = get_memory().search(key, limit=1)
    if hits:
        text = hits[0]["text"]
        return text.split(" is ", 1)[-1] if " is " in text else text
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get(key)
    except Exception:
        return None


# old callers used the name get_memory for the key lookup
get_memory_key = get_memory_value


def search_web(query):
    from harry_core.builtin_tools import web_search
    result = web_search({"query": query, "limit": 3})
    if result.get("ok"):
        return " ".join(r["snippet"] for r in result["results"])
    return ""


def plan_task(command):
    """Legacy planner kept for the old 'help me' flow."""
    from harry_core.task_engine import get_engine
    task = get_engine().create_task(command)
    return [s["description"] for s in get_engine().make_plan(task)]


def create_tool(task):
    """Tool auto-creation, hardened: only writes into tools/ and validates."""
    import re as _re
    prompt = f"""Create a Python tool module for this task.
Rules: define run(command, talk) -> bool (True if handled). Safe code only,
no network deletes, no os.system with user input, no explanation - only code.
Task: {task}"""
    try:
        code = llm.chat([{"role": "user", "content": prompt}],
                        task_class=llm.CODING, options={"num_predict": 500})
    except Exception as e:
        return f"Could not create tool: {e}"
    name = _re.sub(r"[^a-z0-9_]+", "_", task.lower().strip()[:30]) or "custom"
    path = os.path.join("tools", f"{name}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    return f"Tool created: tools/{name}.py"


def analyze_usage():
    try:
        with open(os.path.join("data", "conversation.jsonl"), "r", encoding="utf-8") as f:
            commands = [json.loads(l).get("text", "") for l in f if l.strip()]
    except Exception:
        return "No data to analyze"
    freq = {}
    for cmd in commands:
        word = cmd.split()[0] if cmd.split() else ""
        freq[word] = freq.get(word, 0) + 1
    top = sorted(freq.items(), key=lambda x: -x[1])[:3]
    return f"Most used commands: {top}"


def suggest_upgrade():
    try:
        usage = analyze_usage()
        return llm.chat(
            [{"role": "user", "content":
                f"Based on this usage: {usage}\nSuggest 2-3 short feature ideas."}],
            task_class=llm.REASONING, options={"num_predict": 120})
    except Exception as e:
        return f"Upgrade analysis unavailable: {e}"


def _sync_legacy_file():
    """Mirror key/value memories into legacy memory.json for old tools."""
    try:
        items = get_memory().longterm.all()
        data = {}
        for i in items:
            if " is " in i["text"]:
                k, v = i["text"].split(" is ", 1)
                data[k.strip()[:60]] = v.strip()[:200]
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

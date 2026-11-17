"""Self-diagnostics: 'Harry system check' health report."""

import importlib
import os
import shutil
import socket

from . import config, llm, runtime_paths
from .memory import get_memory


def _check(name, ok, detail=""):
    return {"name": name, "ok": bool(ok), "detail": str(detail)[:120]}


def run_diagnostics():
    checks = []

    # Runtime / workspace
    checks.append(_check(
        "runtime", True,
        ("frozen EXE" if runtime_paths.is_frozen() else "source") +
        f"; data+logs root: {runtime_paths.workspace_root()}",
    ))
    data_dir = os.path.join(runtime_paths.workspace_root(),
                            config.CONFIG.get("data_dir", "data"))
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError:
        pass
    checks.append(_check("data folder ", os.access(data_dir, os.W_OK), f"{data_dir}"))

    try:
        router = llm.get_router()
        models = router.local_models()
        checks.append(_check("local AI (Ollama)", bool(models),
                             f"models: {', '.join(models[:5]) or 'none'}"))
        checks.append(_check("online AI provider", router.online_available(),
                             "configured" if router.online_available() else "not configured (optional)"))
    except Exception as e:
        checks.append(_check("local AI (Ollama)", False, e))

    # Voice
    try:
        from .speech import get_listener, get_speaker
        checks.append(_check("STT (Vosk)", get_listener().available(),
                             get_listener().model_path))
        spk = get_speaker()
        eng = spk._init_engine()
        checks.append(_check("TTS (pyttsx3)", bool(eng),
                             f"voices: {', '.join(spk.available_voices()) or 'default only'}"))
    except Exception as e:
        checks.append(_check("voice", False, e))

    # Wake word
    mode = config.CONFIG.get("voice", {}).get("wake_word_mode", "text")
    ppn = runtime_paths.find_asset(config.CONFIG.get("voice", {}).get("keyword_path", ""))
    checks.append(_check("wake word", mode != "porcupine" or (ppn and os.path.exists(ppn)),
                         f"mode: {mode}"))

    # Memory
    try:
        stats = get_memory().stats()
        checks.append(_check("memory", True,
                             f"{stats['longterm_items']} long-term items, "
                             f"{stats['conversation_turns']} turns"))
    except Exception as e:
        checks.append(_check("memory", False, e))

    # Tools
    try:
        from .tools import get_registry
        names = get_registry().names()
        checks.append(_check("tools", len(names) > 0, f"{len(names)} registered"))
    except Exception as e:
        checks.append(_check("tools", False, e))

    checks.append(_check("terminal", shutil.which("cmd") or shutil.which("bash"),
                         "shell available"))
    checks.append(_check("filesystem", os.access(".", os.W_OK), "project dir writable"))

    # Network
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=2)
        checks.append(_check("internet", True, "reachable"))
    except Exception:
        checks.append(_check("internet", False,
                             "offline - local features still work"))

    # Key dependencies
    for mod, label in (("vosk", "vosk"), ("sounddevice", "sounddevice"),
                       ("pyttsx3", "pyttsx3"), ("psutil", "psutil"),
                       ("pyautogui", "pyautogui"), ("requests", "requests"),
                       ("bs4", "beautifulsoup4")):
        try:
            importlib.import_module(mod)
            checks.append(_check(f"dep: {label}", True))
        except Exception as e:
            checks.append(_check(f"dep: {label}", False, "missing"))

    # report (ASCII-safe for Windows consoles)
    lines = ["Harry System Check:"]
    ok_count = sum(1 for c in checks if c["ok"])
    for c in checks:
        mark = "OK " if c["ok"] else "!! "
        extra = f" - {c['detail']}" if c["detail"] else ""
        lines.append(f" [{mark}] {c['name']}{extra}")
    lines.append(f"Result: {ok_count}/{len(checks)} healthy")
    return "\n".join(lines)

"""Builtin tool implementations.

Every function takes an args dict and returns {"ok": bool, ...}.
Danger levels:
  safe       - read/search/analyze
  controlled - create/edit/run/launch
  sensitive  - delete/send/upload/shutdown
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import webbrowser

from . import config, events, llm, runtime_paths
from .memory import get_memory
from .security import SAFE, CONTROLLED, SENSITIVE

IS_WINDOWS = platform.system() == "Windows"


def _root(p):
    """Resolve path relative to the user workspace when relative."""
    if p and not os.path.isabs(str(p)):
        return os.path.join(runtime_paths.workspace_root(), str(p))
    return str(p or ".")


# --------------------------------------------------------------------------- files

def file_read(args):
    path = _root(args.get("path", ""))
    limit = int(args.get("limit", 400))
    offset = int(args.get("offset", 0))
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    total = len(lines)
    chunk = lines[offset: offset + limit]
    return {"ok": True, "path": path, "total_lines": total,
            "content": "".join(chunk)[:50000]}


def file_write(args):
    path = _root(args.get("path", ""))
    content = str(args.get("content", ""))
    append = bool(args.get("append", False))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    mode = "a" if append else "w"
    with open(path, mode, encoding="utf-8") as f:
        f.write(content)
    return {"ok": True, "path": path, "bytes_written": len(content.encode("utf-8"))}


def file_edit(args):
    """Replace exact text inside a file (safer than full rewrite)."""
    path = _root(args.get("path", ""))
    old = str(args.get("old_text", ""))
    new = str(args.get("new_text", ""))
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    if old not in content:
        return {"ok": False, "error": "old_text not found in file"}
    if content.count(old) > 1 and not args.get("allow_multiple"):
        return {"ok": False, "error": "old_text matches multiple times; set allow_multiple=true"}
    content = content.replace(old, new, -1 if args.get("allow_multiple") else 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True, "path": path}


def file_delete(args):
    path = _root(args.get("path", ""))
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    return {"ok": True, "deleted": path}


def file_search(args):
    """Search file names (glob) and optionally contents (regex) under a directory."""
    import fnmatch
    directory = _root(args.get("directory", "."))
    pattern = str(args.get("pattern", "*"))
    content_pattern = args.get("content")
    max_results = int(args.get("limit", 50))
    matches = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "venv",
                                                "node_modules", "hay_harry_window")]
        for name in files:
            if fnmatch.fnmatch(name.lower(), pattern.lower()):
                path = os.path.join(root, name)
                if content_pattern:
                    try:
                        with open(path, "r", encoding="utf-8", errors="ignore") as f:
                            for i, line in enumerate(f, 1):
                                if re.search(content_pattern, line):
                                    matches.append({"path": path, "line": i, "text": line.strip()[:200]})
                                    if len(matches) >= max_results:
                                        break
                    except Exception:
                        continue
                else:
                    matches.append({"path": path})
            if len(matches) >= max_results:
                break
        if len(matches) >= max_results:
            break
    return {"ok": True, "matches": matches, "count": len(matches)}


def file_tree(args):
    """Small directory listing overview."""
    directory = _root(args.get("directory", "."))
    depth = int(args.get("depth", 2))
    out = []

    def walk(d, level):
        if level > depth or len(out) > 300:
            return
        try:
            entries = sorted(os.listdir(d))
        except Exception:
            return
        for e in entries:
            if e in (".git", "__pycache__", ".venv", "venv", "node_modules"):
                continue
            p = os.path.join(d, e)
            out.append(("  " * level) + e + ("/" if os.path.isdir(p) else ""))
            if os.path.isdir(p):
                walk(p, level + 1)

    walk(directory, 0)
    return {"ok": True, "tree": "\n".join(out[:300])}


# --------------------------------------------------------------------------- terminal

def terminal_run(args):
    """Run a shell command with timeout and output capture."""
    command = str(args.get("command", "")).strip()
    if not command:
        return {"ok": False, "error": "empty command"}
    timeout = int(args.get("timeout", 60))
    cwd = _root(args.get("cwd", "."))
    shell = IS_WINDOWS
    try:
        completed = subprocess.run(
            command, shell=shell, cwd=cwd, capture_output=True,
            text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        out = (completed.stdout or "")[-8000:]
        err = (completed.stderr or "")[-4000:]
        return {"ok": completed.returncode == 0,
                "returncode": completed.returncode,
                "stdout": out, "stderr": err}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"command timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def run_python(args):
    """Run a short Python snippet in a subprocess (isolated)."""
    code = str(args.get("code", ""))
    if not code.strip():
        return {"ok": False, "error": "empty code"}
    timeout = int(args.get("timeout", 60))
    # Frozen EXE: sys.executable does not run python code; fall back to an
    # installed interpreter on PATH (run_python gracefully reports absence).
    python_cmd = sys.executable
    if runtime_paths.is_frozen():
        python_cmd = shutil.which("python") or shutil.which("py")
        if not python_cmd:
            return {"ok": False, "error": "no Python interpreter found on PATH; "
                    "run_python needs Python installed separately"}
    try:
        completed = subprocess.run(
            [python_cmd, "-c", code], capture_output=True,
            text=True, timeout=timeout, encoding="utf-8", errors="replace",
        )
        return {"ok": completed.returncode == 0,
                "returncode": completed.returncode,
                "stdout": (completed.stdout or "")[-8000:],
                "stderr": (completed.stderr or "")[-4000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def open_application(args):
    """Open an app by name (Windows start menu .lnk search, else shell start)."""
    app = str(args.get("app", "")).strip()
    if not app:
        return {"ok": False, "error": "no app given"}
    if IS_WINDOWS:
        start_menu_dirs = [
            r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
            os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        ]
        for base in start_menu_dirs:
            for root, dirs, files in os.walk(base):
                for f in files:
                    if f.endswith(".lnk") and app.lower() in f.lower():
                        os.startfile(os.path.join(root, f))  # noqa
                        return {"ok": True, "opened": f, "via": "start menu"}
        os.system(f'start "" "{app}"')
        return {"ok": True, "opened": app, "via": "shell"}
    else:
        subprocess.Popen([app])
        return {"ok": True, "opened": app}


def close_application(args):
    """Close an application by process name (controlled action)."""
    name = str(args.get("process", "")).strip()
    if not name:
        return {"ok": False, "error": "no process given"}
    return terminal_run({"command": f"taskkill /IM {name} /F" if IS_WINDOWS
                         else f"pkill -f {name}"})


def process_list(args):
    try:
        import psutil
        procs = sorted(psutil.process_iter(["pid", "name", "memory_info"]),
                       key=lambda p: (p.info["memory_info"].rss if p.info["memory_info"] else 0),
                       reverse=True)
        return {"ok": True, "processes": [
            {"pid": p.info["pid"], "name": p.info["name"],
             "mb": round((p.info["memory_info"].rss / 1e6) if p.info["memory_info"] else 0)}
            for p in procs[:int(args.get("limit", 15))]
        ]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def system_info(args):
    try:
        import psutil
        b = psutil.virtual_memory()
        disk = psutil.disk_usage(os.path.expanduser("~") if os.name == "nt" else "/")
        info = {
            "platform": platform.platform(),
            "cpu_percent": psutil.cpu_percent(interval=0.3),
            "ram_used_percent": b.percent,
            "ram_used_gb": round((b.total - b.available) / 1e9, 1),
            "ram_total_gb": round(b.total / 1e9, 1),
            "disk_free_gb": round(disk.free / 1e9, 1),
        }
        bat = psutil.sensors_battery()
        if bat:
            info["battery_percent"] = bat.percent
            info["plugged_in"] = bool(bat.power_plugged)
        return {"ok": True, "system": info}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- web

def web_search(args):
    query = str(args.get("query", "")).strip()
    if not query:
        return {"ok": False, "error": "no query"}
    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=int(args.get("limit", 5))):
                results.append({"title": r.get("title", ""), "url": r.get("href", ""),
                                "snippet": r.get("body", "")})
        if not results:
            return {"ok": False, "error": "no results (offline?)", "offline_hint": True}
        return {"ok": True, "results": results}
    except Exception as e:
        return {"ok": False, "error": f"web search unavailable: {e}", "offline_hint": True}


def browser_open(args):
    url = str(args.get("url", "")).strip()
    if not url:
        return {"ok": False, "error": "no url"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url)
    return {"ok": True, "opened": url}


def fetch_page(args):
    """Read a webpage as text (offline-safe: fails gracefully)."""
    url = str(args.get("url", "")).strip()
    try:
        import requests
        from bs4 import BeautifulSoup
        r = requests.get(url, timeout=15, headers={"User-Agent": "Harry/2.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
        return {"ok": True, "url": url, "title": soup.title.string if soup.title else "",
                "text": text[:int(args.get("max_chars", 6000))]}
    except Exception as e:
        return {"ok": False, "error": f"fetch failed: {e}", "offline_hint": True}


def download_file(args):
    url = str(args.get("url", ""))
    dest = _root(args.get("dest") or os.path.join("downloads", os.path.basename(url) or "file"))
    try:
        import requests
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
        return {"ok": True, "path": dest}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- memory / knowledge

def memory_store(args):
    text = str(args.get("text", "")).strip()
    if not text:
        return {"ok": False, "error": "nothing to store"}
    mid = get_memory().store(
        text, kind=args.get("kind", "fact"), tags=args.get("tags") or [],
        importance=int(args.get("importance", 1)), source="tool",
    )
    return {"ok": True, "id": mid}


def memory_search(args):
    query = str(args.get("query", ""))
    results = get_memory().search(query, limit=int(args.get("limit", 5)))
    return {"ok": True, "results": [{"id": r["id"], "text": r["text"],
                                     "kind": r.get("kind")} for r in results]}


def memory_forget(args):
    removed = get_memory().forget(memory_id=args.get("id"), text=args.get("text"))
    return {"ok": True, "removed": removed}


def remember_command(args):
    """'remember that X is Y' style parsing."""
    text = str(args.get("text", "")).strip()
    if not text:
        return {"ok": False, "error": "nothing to remember"}
    mid = get_memory().store(text, kind="fact", importance=2, source="user")
    return {"ok": True, "id": mid, "stored": text}


# --------------------------------------------------------------------------- computer control

def screenshot(args):
    """Capture the screen to a file. Vision analysis is separate (vision tool)."""
    try:
        import pyautogui
        path = _root(args.get("path") or os.path.join("data", "screenshots",
                                                      f"screen_{int(time.time())}.png"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        img = pyautogui.screenshot(path)
        return {"ok": True, "path": path, "size": list(img.size)}
    except Exception as e:
        return {"ok": False, "error": f"screenshot failed: {e}"}


def computer_control(args):
    """Controlled mouse/keyboard automation. Every call is gated as CONTROLLED."""
    action = str(args.get("action", ""))
    try:
        import pyautogui
        pyautogui.FAILSAFE = True  # slam mouse to corner = abort
        if action == "click":
            pyautogui.click(int(args.get("x", 0)), int(args.get("y", 0)))
        elif action == "double_click":
            pyautogui.doubleClick(int(args.get("x", 0)), int(args.get("y", 0)))
        elif action == "move":
            pyautogui.moveTo(int(args.get("x", 0)), int(args.get("y", 0)), duration=0.3)
        elif action == "scroll":
            pyautogui.scroll(int(args.get("amount", -300)))
        elif action == "type":
            pyautogui.write(str(args.get("text", "")), interval=0.02)
        elif action == "hotkey":
            pyautogui.hotkey(*[k.strip() for k in str(args.get("keys", "")).split("+")])
        elif action == "press":
            pyautogui.press(str(args.get("key", "")))
        else:
            return {"ok": False, "error": f"unknown action: {action}"}
        return {"ok": True, "action": action}
    except pyautogui.FailSafeException:  # noqa
        return {"ok": False, "error": "failsafe triggered - emergency stop"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def clipboard(args):
    action = str(args.get("action", "get"))
    try:
        import pyperclip
        if action == "get":
            return {"ok": True, "content": pyperclip.paste()[:5000]}
        elif action == "set":
            pyperclip.copy(str(args.get("text", "")))
            return {"ok": True}
        return {"ok": False, "error": "unknown action"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def window_list(args):
    try:
        if IS_WINDOWS:
            import ctypes
            import ctypes.wintypes as wt

            EnumWindows = ctypes.windll.user32.EnumWindows
            GetWindowText = ctypes.windll.user32.GetWindowTextW
            IsWindowVisible = ctypes.windll.user32.IsWindowVisible
            titles = []
            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

            def cb(hwnd, _):
                if IsWindowVisible(hwnd):
                    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                    if length:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        GetWindowText(hwnd, buf, length + 1)
                        if buf.value.strip():
                            titles.append({"pid": int(hwnd), "title": buf.value.strip()[:80]})
                return True

            EnumWindows(EnumWindowsProc(cb), 0)
            return {"ok": True, "windows": titles[:40]}
        return {"ok": False, "error": "window listing only implemented on Windows"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- vision

def vision_analyze(args):
    """Understand an image with a local vision model if configured, else basic OCR."""
    path = _root(args.get("path", ""))
    question = str(args.get("question", "Describe this image."))
    if not os.path.exists(path):
        return {"ok": False, "error": f"image not found: {path}"}
    vision_model = config.CONFIG["models"].get("vision_model", "")
    if vision_model and llm.get_router().local.has_model(vision_model):
        try:
            import base64
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            messages = [{"role": "user", "content": question,
                         "images": [b64]}]
            answer = llm.chat(messages, task_class=llm.VISION)
            return {"ok": True, "analysis": answer, "model": vision_model}
        except Exception as e:
            log_error = e
    # fallback: OCR via pytesseract if present
    try:
        import pytesseract
        from PIL import Image
        text = pytesseract.image_to_string(Image.open(path))
        return {"ok": True, "analysis": text.strip()[:4000], "method": "ocr"}
    except Exception as e:
        return {"ok": False,
                "error": f"no vision model configured and OCR unavailable ({e})"}


def understand_screen(args):
    """Explicit, user-controlled screen understanding.

    Screen capture is never silent: the caller must pass consent=true, which the
    planner only sets after the user explicitly asked Harry to look at the
    screen. The screenshot is saved locally (data/screenshots) and analysed with
    a local vision model when one is configured, otherwise by OCR when
    tesseract is installed.
    """
    if not args.get("consent"):
        return {"ok": False,
                "error": "screen capture needs explicit consent; ask the user to "
                         "confirm they want their screen read, then retry with "
                         "consent=true"}
    shot = screenshot({"path": args.get("path")})
    if not shot.get("ok"):
        return shot
    question = str(args.get("question") or
                   "Describe what is visible on this screen. Read any error "
                   "messages, dialog text and UI labels verbatim.")
    analysis = vision_analyze({"path": shot["path"], "question": question})
    analysis["screenshot"] = shot["path"]
    return analysis


# --------------------------------------------------------------------------- tasks / reminders

def task_create(args):
    from .task_engine import get_engine
    t = get_engine().create_task(args.get("title", ""), description=args.get("description", ""))
    return {"ok": True, "task": t}


def task_list(args):
    from .task_engine import get_engine
    return {"ok": True, "tasks": get_engine().list_tasks(args.get("status"))}


def reminder_create(args):
    from .scheduler import get_scheduler
    sch = get_scheduler()
    rid = sch.add_reminder(args.get("text", ""), delay_minutes=float(args.get("delay_minutes", 5)))
    return {"ok": True, "reminder_id": rid}


# --------------------------------------------------------------------------- registration

def register_builtins(registry):
    from .tools import Tool
    tools = [
        Tool("read_file", "Read a text file from disk", file_read, SAFE,
             {"path": "str", "limit": "int", "offset": "int"}),
        Tool("write_file", "Create or overwrite a file", file_write, CONTROLLED,
             {"path": "str", "content": "str"}),
        Tool("edit_file", "Replace exact text inside a file", file_edit, CONTROLLED,
             {"path": "str", "old_text": "str", "new_text": "str"}),
        Tool("delete_file", "Delete a file or folder (destructive)", file_delete, SENSITIVE,
             {"path": "str"}),
        Tool("search_files", "Search file names/content on disk", file_search, SAFE,
             {"directory": "str", "pattern": "str", "content": "regex"}),
        Tool("file_tree", "Show a small directory tree", file_tree, SAFE,
             {"directory": "str", "depth": "int"}),
        Tool("terminal", "Run a shell/terminal command", terminal_run, CONTROLLED,
             {"command": "str", "timeout": "int"}),
        Tool("run_python", "Run a short Python snippet", run_python, CONTROLLED,
             {"code": "str"}),
        Tool("open_app", "Open an application", open_application, CONTROLLED,
             {"app": "str"}),
        Tool("close_app", "Close an application by process name", close_application, CONTROLLED,
             {"process": "str"}),
        Tool("process_list", "List running processes", process_list, SAFE, {}),
        Tool("system_info", "Get CPU/RAM/disk/battery status", system_info, SAFE, {}),
        Tool("web_search", "Search the web (needs internet)", web_search, SAFE,
             {"query": "str", "limit": "int"}),
        Tool("browser_open", "Open a URL in the default browser", browser_open, CONTROLLED,
             {"url": "str"}),
        Tool("fetch_page", "Read a webpage as plain text (needs internet)", fetch_page, SAFE,
             {"url": "str"}),
        Tool("download_file", "Download a file from a URL", download_file, CONTROLLED,
             {"url": "str", "dest": "str"}),
        Tool("memory_store", "Save a fact to long-term memory", memory_store, SAFE,
             {"text": "str", "kind": "str", "tags": "list"}),
        Tool("memory_search", "Search long-term memory", memory_search, SAFE,
             {"query": "str"}),
        Tool("memory_forget", "Delete a memory by id or text", memory_forget, SENSITIVE,
             {"id": "str", "text": "str"}),
        Tool("screenshot", "Capture the screen to an image file", screenshot, CONTROLLED,
             {"path": "str"}),
        Tool("computer_control", "Control mouse/keyboard (click/type/hotkey)", computer_control,
             CONTROLLED, {"action": "str", "x": "int", "y": "int", "text": "str", "keys": "str"}),
        Tool("clipboard", "Read or set the clipboard", clipboard, SAFE,
             {"action": "str", "text": "str"}),
        Tool("window_list", "List visible window titles", window_list, SAFE, {}),
        Tool("vision_analyze", "Analyze an image/screenshot with vision model or OCR",
             vision_analyze, SAFE, {"path": "str", "question": "str"}),
        Tool("task_create", "Create a task in the task engine", task_create, SAFE,
             {"title": "str", "description": "str"}),
        Tool("task_list", "List tasks", task_list, SAFE, {"status": "str"}),
        Tool("understand_screen",
             "Look at the user's screen (explicit consent required) and read it",
             understand_screen, CONTROLLED,
             {"question": "str", "consent": "bool", "path": "str"}),
        Tool("reminder_create", "Create a reminder after N minutes", reminder_create, SAFE,
             {"text": "str", "delay_minutes": "float"}),
    ]
    for t in tools:
        registry.register(t)

    # Specialist tool packs (math/engineering, research, documents).
    from .math_tools import register_math_tools
    from .research import register_research_tools
    from .documents import register_document_tools
    register_math_tools(registry)
    register_research_tools(registry)
    register_document_tools(registry)

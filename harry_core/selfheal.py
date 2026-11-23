"""Self-healing: capture a failure, diagnose it, repair only what is safe.

The rule that keeps this honest: a repair exists only when there is a concrete,
reversible action for the *specific* component. Restarting a service in a loop
is not a repair, so every action is counted per component per hour
(``max_repairs_per_hour``) and repeated failures escalate to the administrator
instead of retrying.

Supported safe repairs
---------------------
``documents_index``  drop index rows whose file vanished, rebuild unreadable JSON
``media_index``      same, after the same validation
``jobs``             mark jobs left ``running`` by a crash as interrupted
``tts``              drop the dead pyttsx3 engine so the next sentence re-creates it
``stt``              reload the Vosk model holder after a microphone/model failure

Anything else is diagnosed and reported, never "fixed" by pretending.
"""

import json
import logging
import os
import time
import traceback

from . import config, runtime_paths

log = logging.getLogger("harry.selfheal")

MAX_ATTEMPTS = 3
WINDOW = 3600.0

_attempts = {}          # component -> [timestamps]
_history = []           # bounded improvement/repair history
_alerts = []
_lock = None


def _thread_lock():
    global _lock
    if _lock is None:
        import threading
        _lock = threading.Lock()
    return _lock


def history_path():
    override = os.environ.get("HARRY_SELFHEAL_JSON", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(runtime_paths.workspace_root(), "data", "selfheal.json")


def _load_history():
    global _history
    if _history:
        return _history
    try:
        with open(history_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            _history = data[-200:]
    except Exception:
        _history = []
    return _history


def _save_history():
    path = history_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _thread_lock():
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_history[-200:], f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def _record(entry):
    entry = {"at": time.time(), **entry}
    _load_history()
    with _thread_lock():
        _history.append(entry)
        del _history[:-200]
    _save_history()
    return entry


def components():
    return {
        "documents_index": "document library index",
        "media_index": "image and video index",
        "jobs": "background job queue",
        "tts": "speech output engine",
        "stt": "speech recognition",
        "ollama": "local AI model server",
        "memory": "long-term memory database",
        "config": "configuration file",
        "plugins": "plugin loader",
    }


def classify_component(error, context=""):
    """Best-effort mapping from a traceback to the component that failed."""
    text = f"{error}\n{context}".lower()
    rules = (
        ("documents_index", ("docgen", "documents/index", "document")),
        ("media_index", ("media", "image index", "video index")),
        ("jobs", ("job", "worker", "thread")),
        ("stt", ("vosk", "microphone", "sounddevice", "listener", "speech recognition")),
        ("tts", ("pyttsx3", "speaker", "tts", "voice output")),
        ("ollama", ("ollama", "11434", "model")),
        ("memory", ("memory", "sqlite", "database")),
        ("config", ("config.json", "configuration")),
        ("plugins", ("plugin", "importlib")),
    )
    for component, needles in rules:
        if any(needle in text for needle in needles):
            return component
    return "unknown"


def report_error(component, error, context=""):
    """Record a failure with the detail needed to diagnose it later."""
    if component in (None, "", "auto"):
        component = classify_component(error, context)
    record = _record({
        "kind": "error", "component": component,
        "error": str(error)[:800], "context": str(context)[:800],
        "trace": traceback.format_exc()[-1500:] if context else "",
    })
    log.warning("error recorded (%s): %s", component, error)
    return record


# ------------------------------------------------------------------ diagnosis

def _index_check(name, root, key, exists):
    """Inspect an index file + referenced files. Returns a findings dict."""
    findings = {"component": name, "ok": True, "problems": [], "counts": {}}
    path = os.path.join(root, "index.json")
    if not os.path.isdir(root):
        findings.update(ok=True, detail="no library yet")
        return findings
    raw = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        findings["problems"].append("index.json is missing")
    except Exception as e:
        findings["problems"].append(f"index.json is unreadable: {e}")
    entries = {}
    if isinstance(raw, dict):
        maybe = raw.get(key)
        if isinstance(maybe, dict):
            entries = maybe
    dangling = [k for k, v in entries.items()
                if not exists(os.path.join(root, str(v.get("file") or "")))]
    if dangling:
        findings["problems"].append(f"{len(dangling)} index entries point at missing files")
    findings["counts"] = {"entries": len(entries), "dangling": len(dangling)}
    findings["ok"] = not findings["problems"]
    return findings


_diag_cache = {"at": 0, "data": None}


def diagnose(component=None, max_age=15):
    """Run real checks for one component (or all of them).

    Results are cached briefly because the dashboard, the monitor and the heal
    path all ask; ``max_age=0`` forces a fresh sample for before/after proof.
    """
    from . import docgen, media
    if (component is None and max_age and _diag_cache["data"] is not None
            and time.time() - _diag_cache["at"] < max_age):
        return _diag_cache["data"]

    def _media_exists(path):
        return os.path.isfile(path)

    checks = {}
    if component in (None, "documents_index"):
        checks["documents_index"] = _index_check(
            "documents_index", docgen.documents_root(), "documents", os.path.isfile)
    if component in (None, "media_index"):
        checks["media_index"] = _index_check(
            "media_index", media.media_root(), "items", _media_exists)
    if component in (None, "jobs"):
        from . import jobs as jobs_mod
        findings = {"component": "jobs", "ok": True, "problems": [], "counts": {}}
        try:
            items = jobs_mod.list_jobs(200) if hasattr(jobs_mod, "list_jobs") else []
            stale = [j for j in items if j.get("state") == "running"
                     and time.time() - float(j.get("updated") or 0) > 1800]
            if stale:
                findings["problems"].append(f"{len(stale)} jobs stuck in running")
            findings["counts"] = {"jobs": len(items), "stale": len(stale)}
            findings["ok"] = not stale
        except Exception as e:
            findings["problems"].append(str(e))
            findings["ok"] = False
        checks["jobs"] = findings
    if component in (None, "tts"):
        from .speech import get_speaker
        speaker = get_speaker()
        engine = speaker._init_engine()
        checks["tts"] = {"component": "tts", "ok": bool(engine),
                         "problems": [] if engine else ["the speech engine did not start"],
                         "counts": {}}
    if component in (None, "stt"):
        from .speech import get_listener
        listener = get_listener()
        ok = listener.available()
        checks["stt"] = {"component": "stt", "ok": ok,
                         "problems": [] if ok else [f"Vosk model missing at {listener.model_path}"],
                         "counts": {}}
    if component in (None, "ollama"):
        from . import llm
        try:
            models = llm.get_router().local_models()
            checks["ollama"] = {"component": "ollama", "ok": bool(models),
                                "problems": [] if models else ["Ollama is not reachable"],
                                "counts": {"models": len(models)}}
        except Exception as e:
            checks["ollama"] = {"component": "ollama", "ok": False,
                                "problems": [str(e)], "counts": {}}
    if component in (None, "memory"):
        from .memory import get_memory
        try:
            stats = get_memory().stats()
            checks["memory"] = {"component": "memory", "ok": True, "problems": [],
                                "counts": stats}
        except Exception as e:
            checks["memory"] = {"component": "memory", "ok": False,
                                "problems": [str(e)], "counts": {}}
    if component in (None, "config"):
        path = runtime_paths.find_config() if hasattr(runtime_paths, "find_config") else None
        path = path or os.path.join(runtime_paths.workspace_root(), "config.json")
        problems = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
        except FileNotFoundError:
            problems.append("config.json is missing (defaults are in use)")
        except Exception as e:
            problems.append(f"config.json is not valid JSON: {e}")
        checks["config"] = {"component": "config", "ok": not problems,
                            "problems": problems, "counts": {"path": path}}
    if component in (None, "plugins"):
        from . import capabilities
        findings = {"component": "plugins", "ok": True, "problems": [], "counts": {}}
        try:
            found = capabilities.discover()
            bad = [p for p in found if not p["valid"]]
            findings["counts"] = {"plugins": len(found), "invalid": len(bad)}
            if bad:
                findings["problems"].append(f"{len(bad)} plugins failed validation")
                findings["ok"] = False
        except Exception as e:
            findings["problems"].append(str(e))
            findings["ok"] = False
        checks["plugins"] = findings
    if component is None:
        _diag_cache["at"] = time.time()
        _diag_cache["data"] = checks
    return checks


# ------------------------------------------------------------------ repairs

def _repair_index(name, root, key, cache=None):
    """Drop dangling rows and recreate a corrupt/nonexistent index."""
    path = os.path.join(root, "index.json")
    backup = None
    entries = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and isinstance(raw.get(key), dict):
            entries = raw[key]
    except Exception:
        if os.path.exists(path):
            backup = path + f".corrupt-{int(time.time())}"
            try:
                os.replace(path, backup)
            except OSError:
                backup = None
    if entries is None:
        entries = {}
    kept = {k: v for k, v in entries.items()
            if os.path.isfile(os.path.join(root, str(v.get("file") or "")))}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({key: kept}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    if cache is not None:
        # The owning module keeps the index in memory; drop it so the repaired
        # file is read back instead of the stale copy.
        cache["path"] = None
    return {"ok": True, "kept": len(kept), "dropped": len(entries) - len(kept),
            "backup": backup}


def _repair_jobs():
    from . import jobs as jobs_mod
    if hasattr(jobs_mod, "recover_stale"):
        return jobs_mod.recover_stale()
    return {"ok": False, "error": "job recovery is not available in this build"}


def _repair_tts():
    from .speech import get_speaker
    speaker = get_speaker()
    speaker.stop_speaking()
    speaker._engine = None
    engine = speaker._init_engine()
    return {"ok": bool(engine), "detail": "engine re-created" if engine else "engine still unavailable"}


def _repair_stt():
    from . import speech
    listener = speech.get_listener()
    if hasattr(listener, "reset"):
        listener.reset()
    return {"ok": listener.available(), "detail": "listener reloaded"}


def _repair_documents():
    from . import docgen
    return _repair_index("documents_index", docgen.documents_root(), "documents",
                         docgen._index_cache)


def _repair_media():
    from . import media
    return _repair_index("media_index", media.media_root(), "items",
                         media._index_cache)


REPAIRS = {
    "documents_index": _repair_documents,
    "media_index": _repair_media,
    "jobs": _repair_jobs,
    "tts": _repair_tts,
    "stt": _repair_stt,
}

# Components where a repair would touch private data or security: never automatic.
APPROVAL_ONLY = ("config", "memory", "ollama", "plugins")


def _allowed(component):
    max_per_hour = int((config.CONFIG.get("selfdev", {}) or {}).get("max_repairs_per_hour", MAX_ATTEMPTS))
    now = time.time()
    stamps = [t for t in _attempts.get(component, []) if now - t < WINDOW]
    _attempts[component] = stamps
    return len(stamps) < max_per_hour


def heal(component, actor=None, force=False):
    """Diagnose then repair if a safe, known action exists. Never guesses."""
    component = str(component or "")
    if component not in components():
        return {"ok": False, "component": component,
                "error": "unknown component", "repaired": False}
    before = diagnose(component, max_age=0).get(component) or {}
    if before.get("ok"):
        return {"ok": True, "component": component, "repaired": False,
                "detail": "the component is healthy; nothing was changed"}
    if component in APPROVAL_ONLY:
        return {"ok": False, "component": component, "repaired": False,
                "error": "this repair changes private data or security settings and "
                         "needs administrator approval",
                "needs_approval": True,
                "problems": before.get("problems", [])}
    if component not in REPAIRS:
        return {"ok": False, "component": component, "repaired": False,
                "error": "no automated repair exists for this component",
                "problems": before.get("problems", [])}
    if not force and not _allowed(component):
        return {"ok": False, "component": component, "repaired": False,
                "error": "repair limit reached for the last hour; the administrator "
                         "must look at this instead of retrying",
                "problems": before.get("problems", [])}
    _attempts.setdefault(component, []).append(time.time())
    try:
        result = REPAIRS[component]()
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    after = diagnose(component, max_age=0).get(component) or {}
    healed = bool(result.get("ok")) and after.get("ok")
    entry = _record({
        "kind": "repair", "component": component, "ok": healed,
        "result": result, "problems": before.get("problems", []),
        "actor": actor or "admin",
    })
    try:
        from . import logging_setup
        logging_setup.audit("selfheal_repair", component,
                            ok=healed, actor=actor or "admin")
    except Exception:
        pass
    if not healed:
        alert("repair_failed", f"automatic repair of {component} did not fix it",
              {"component": component, "result": result})
    return {"ok": healed, "component": component, "repaired": healed,
            "problems": before.get("problems", []), "repair": result,
            "verified": after.get("ok"), "at": entry["at"]}


def alerts():
    return list(_alerts[-100:])


def alert(kind, message, detail=None):
    item = {"at": time.time(), "kind": str(kind), "message": str(message)[:500],
            "detail": detail or {}, "seen": False}
    _alerts.append(item)
    del _alerts[:-100]
    try:
        from . import events
        events.emit("alert", item)
    except Exception:
        pass
    try:
        from . import logging_setup
        logging_setup.audit("alert", f"{kind}: {message}")
    except Exception:
        pass
    try:
        import json as _json
        path = os.environ.get("HARRY_ALERTS_JSON", "").strip() or os.path.join(
            runtime_paths.workspace_root(), "data", "alerts.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    existing = json.load(f) or []
            except Exception:
                existing = []
        existing.append(item)
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(existing[-200:], f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return item


def recent(limit=50):
    return list(reversed(_load_history()[-int(limit):]))

"""Capability registry and validated plugin loading.

Every tool Harry can call is a capability with a record: version, permissions,
status, install date, last successful test, error history and update history.

Plugins are *not* arbitrary downloads. A plugin is a directory under
``plugins/`` containing a manifest and one Python file. Before anything is
imported it must pass:

1. manifest schema validation (name, version, entry point, permissions)
2. a SHA-256 match against the manifest hash
3. an AST scan that rejects dangerous imports and calls
4. an explicit administrator approval recorded in the admin store

Only then is the module imported and its function registered as a tool. The
registry keeps pending and rejected plugins visible but never executes them.
"""

import ast
import hashlib
import json
import logging
import os
import re
import threading
import time

from . import runtime_paths

log = logging.getLogger("harry.capabilities")

ALLOWED_PERMISSIONS = ("safe", "controlled")
FORBIDDEN_MODULES = (
    "subprocess", "ctypes", "socket", "shutil", "multiprocessing", "pty",
    "importlib", "pickle", "marshal", "builtins", "__builtin__", "sys",
    "os.path", "pathlib", "glob", "tempfile", "shelve", "sqlite3",
)
ALLOWED_MODULES = (
    "json", "re", "math", "statistics", "datetime", "time", "random",
    "string", "textwrap", "html", "collections", "itertools", "functools",
    "csv", "io", "urllib.parse", "base64", "hashlib", "requests",
    "bs4", "unicodedata", "difflib", "decimal",
)
FORBIDDEN_NAMES = (
    "eval", "exec", "compile", "__import__", "open", "input", "globals",
    "locals", "vars", "getattr", "setattr", "delattr", "memoryview",
    "breakpoint", "help", "exit", "quit",
)
FORBIDDEN_ATTRS = (
    "system", "popen", "spawn", "fork", "execv", "execve", "remove", "unlink",
    "rmdir", "rmtree", "chmod", "chown", "kill", "terminate",
)
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")

_lock = threading.RLock()
_cache = {"path": None, "data": None}


def plugins_root():
    override = os.environ.get("HARRY_PLUGINS_DIR", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(runtime_paths.workspace_root(), "plugins")


def registry_path():
    override = os.environ.get("HARRY_CAPABILITIES_JSON", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(runtime_paths.workspace_root(), "data", "capabilities.json")


def _load():
    path = registry_path()
    with _lock:
        if _cache["path"] == path and _cache["data"] is not None:
            return _cache["data"]
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data.get("capabilities"), dict):
                data = {"capabilities": {}}
        except Exception:
            data = {"capabilities": {}}
        _cache["path"] = path
        _cache["data"] = data
        return data


def _save(data):
    path = registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    with _lock:
        _cache["path"] = path
        _cache["data"] = data


def _record(name):
    data = _load()
    return data["capabilities"].get(name)


def sync_tools():
    """Mirror the live tool registry into capability records."""
    from .tools import get_registry
    registry = get_registry()
    data = _load()
    now = time.time()
    changed = False
    for name, tool in registry.all().items():
        record = data["capabilities"].get(name)
        if record is None:
            data["capabilities"][name] = {
                "name": name,
                "description": getattr(tool, "description", "")[:300],
                "version": "1.0.0",
                "source": getattr(tool, "source", "builtin"),
                "permissions": [getattr(tool, "danger", "safe")],
                "status": "active",
                "installed": now,
                "last_test": None,
                "tests": {"passed": 0, "failed": 0},
                "errors": [],
                "updates": [],
            }
            changed = True
        else:
            description = getattr(tool, "description", "")[:300]
            if description and description != record.get("description"):
                record["description"] = description
                changed = True
            if record.get("status") == "disabled":
                try:
                    registry._tools.pop(name, None)
                except Exception:
                    pass
    if changed:
        _save(data)
    return len(data["capabilities"])


def list_capabilities(status=None):
    data = _load()
    items = list(data["capabilities"].values())
    if status:
        items = [i for i in items if i.get("status") == status]
    items.sort(key=lambda i: (i.get("source") != "plugin", i["name"]))
    return items


def get_capability(name):
    return _record(str(name))


def record_test(name, ok, detail=""):
    data = _load()
    record = data["capabilities"].get(name)
    if not record:
        return
    record.setdefault("tests", {"passed": 0, "failed": 0})
    record["tests"]["passed" if ok else "failed"] += 1
    if ok:
        record["last_test"] = time.time()
        record["status"] = "active" if record.get("status") != "rejected" else record["status"]
    else:
        record["last_error"] = str(detail)[:400]
        if record.get("source") == "plugin":
            record["status"] = "failed"
    _save(data)


def record_error(name, error):
    data = _load()
    record = data["capabilities"].get(name)
    if not record:
        return
    errors = record.setdefault("errors", [])
    errors.append({"at": time.time(), "error": str(error)[:300]})
    del errors[:-20]
    _save(data)


def register_external(name, description, func, permissions=None, version="1.0.0",
                      source="plugin", status="active"):
    from .tools import Tool
    from .security import SAFE, CONTROLLED
    permissions = permissions or ["safe"]
    danger = CONTROLLED if "controlled" in permissions else SAFE
    tool = Tool(name, description, func, danger, {}, source=source)
    from .tools import get_registry
    get_registry().register(tool)
    data = _load()
    now = time.time()
    existing = data["capabilities"].get(name) or {}
    data["capabilities"][name] = {
        **existing,
        "name": name, "description": str(description)[:300], "version": version,
        "source": source, "permissions": permissions, "status": status,
        "installed": existing.get("installed") or now,
        "last_test": existing.get("last_test"),
        "tests": existing.get("tests") or {"passed": 0, "failed": 0},
        "errors": existing.get("errors") or [],
        "updates": (existing.get("updates") or []) + [
            {"at": now, "version": version, "note": "registered"}][-30:],
    }
    _save(data)
    return tool


def disable(name):
    data = _load()
    record = data["capabilities"].get(name)
    if not record:
        return {"ok": False, "error": "unknown capability"}
    record["status"] = "disabled"
    _save(data)
    try:
        from .tools import get_registry
        get_registry()._tools.pop(name, None)
    except Exception:
        pass
    return {"ok": True}


# ------------------------------------------------------------------ validation

def validate_plugin_code(code):
    """Static checks. Returns (ok, problems)."""
    problems = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, [f"syntax error: {e}"]
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""])
            for module in names:
                base = module.split(".")[0]
                if base in FORBIDDEN_MODULES:
                    problems.append(f"forbidden import: {module}")
                elif base not in [m.split(".")[0] for m in ALLOWED_MODULES]:
                    problems.append(f"module not allowed: {module}")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_NAMES:
                problems.append(f"forbidden call: {func.id}")
            if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_ATTRS:
                problems.append(f"forbidden call: .{func.attr}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            problems.append(f"dunder access: {node.attr}")
    return (not problems), problems


def hash_code(data):
    """SHA-256 of plugin source, normalised to LF line endings.

    Windows writes text files with CRLF, so a manifest generated on one platform
    would otherwise mismatch the same code on another. Normalising here keeps
    the integrity check exact without depending on the checkout.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def validate_manifest(manifest, code_bytes):
    problems = []
    if not isinstance(manifest, dict):
        return ["manifest must be an object"]
    name = str(manifest.get("name") or "")
    if not NAME_RE.match(name):
        problems.append("name must be lowercase letters, digits and underscores (3-40)")
    if not str(manifest.get("version") or "").strip():
        problems.append("version is required")
    if not str(manifest.get("description") or "").strip():
        problems.append("description is required")
    if str(manifest.get("entry") or "") != "plugin.py":
        problems.append("entry must be plugin.py")
    if str(manifest.get("function") or "") != "run":
        problems.append("function must be run")
    permissions = manifest.get("permissions") or ["safe"]
    if not isinstance(permissions, list) or any(
            p not in ALLOWED_PERMISSIONS for p in permissions):
        problems.append(f"permissions must be a subset of {ALLOWED_PERMISSIONS}")
    declared = str(manifest.get("sha256") or "")
    if not declared:
        problems.append("sha256 of plugin.py is required")
    elif declared != hash_code(code_bytes):
        problems.append("sha256 does not match plugin.py")
    return problems


def discover():
    """Read plugins/ and report every plugin with its validation state."""
    root = plugins_root()
    found = []
    if not os.path.isdir(root):
        return found
    for name in sorted(os.listdir(root)):
        folder = os.path.join(root, name)
        if not os.path.isdir(folder) or name.startswith("."):
            continue
        manifest_path = os.path.join(folder, "plugin.json")
        code_path = os.path.join(folder, "plugin.py")
        entry = {"id": name, "folder": folder, "valid": False, "problems": [],
                 "manifest": {}}
        if not os.path.exists(manifest_path) or not os.path.exists(code_path):
            entry["problems"].append("plugin.json and plugin.py are required")
            found.append(entry)
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            with open(code_path, "rb") as f:
                code_bytes = f.read()
        except Exception as e:
            entry["problems"].append(f"cannot read plugin files: {e}")
            found.append(entry)
            continue
        entry["manifest"] = manifest
        problems = validate_manifest(manifest, code_bytes)
        ok, code_problems = validate_plugin_code(code_bytes.decode("utf-8", "replace"))
        entry["problems"] = problems + code_problems
        entry["valid"] = not entry["problems"]
        found.append(entry)
    return found


def install_pending(approved_ids=None):
    """Import valid plugins whose manifest name is approved. Never executes others."""
    approved_ids = set(approved_ids or [])
    results = []
    from . import admin
    store = admin.get_admin_store()
    for entry in discover():
        name = (entry.get("manifest") or {}).get("name") or entry["id"]
        record = _record(name)
        already = bool(record and record.get("status") == "active")
        if not entry["valid"]:
            results.append({"id": entry["id"], "name": name, "status": "rejected",
                            "problems": entry["problems"]})
            continue
        if already:
            results.append({"id": entry["id"], "name": name, "status": "active"})
            continue
        permission_ok = entry["id"] in approved_ids or (
            record and record.get("status") == "approved")
        if not permission_ok:
            data = _load()
            data["capabilities"].setdefault(name, {
                "name": name, "description": entry["manifest"].get("description", "")[:300],
                "version": entry["manifest"].get("version", "1.0.0"),
                "source": "plugin", "permissions": entry["manifest"].get("permissions", ["safe"]),
                "status": "pending", "installed": time.time(), "last_test": None,
                "tests": {"passed": 0, "failed": 0}, "errors": [], "updates": [],
            })
            _save(data)
            results.append({"id": entry["id"], "name": name, "status": "pending",
                            "problems": ["waiting for administrator approval"]})
            continue
        try:
            module = _import_plugin(entry)
            function = getattr(module, "run", None)
            if not callable(function):
                raise RuntimeError("plugin has no callable run(args)")

            def _call(args, _fn=function):
                result = _fn(args or {})
                return result if isinstance(result, dict) else {"ok": True, "result": result}

            register_external(name, entry["manifest"].get("description", ""), _call,
                              permissions=entry["manifest"].get("permissions") or ["safe"],
                              version=entry["manifest"].get("version", "1.0.0"))
            store.audit(None, "plugin_activated", {"name": name, "id": entry["id"]})
            results.append({"id": entry["id"], "name": name, "status": "active"})
        except Exception as e:
            log.warning("plugin %s failed to load: %s", entry["id"], e)
            record_error(name, e)
            results.append({"id": entry["id"], "name": name, "status": "error",
                            "problems": [str(e)]})
    return results


def _import_plugin(entry):
    import importlib.util
    code_path = os.path.join(entry["folder"], "plugin.py")
    module_name = f"harry_plugin_{entry['id']}"
    spec = importlib.util.spec_from_file_location(module_name, code_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("plugin could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_capability(name, args=None):
    """Execute one tool and record the outcome against its capability."""
    from .tools import get_registry
    tool = get_registry().get(name)
    if not tool:
        return {"ok": False, "error": "capability is not registered"}
    started = time.perf_counter()
    try:
        result = tool.run(args or {})
        ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        ok = False
    record_test(name, ok, result.get("error", "") if isinstance(result, dict) else "")
    return {"ok": ok, "ms": round((time.perf_counter() - started) * 1000),
            "result": result}

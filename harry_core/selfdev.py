"""Self-development: propose, test, activate and roll back real upgrades.

This is deliberately *not* a button that edits Harry's own security modules.
The only thing Harry may add without touching core code is a **plugin**: a
single validated file with a manifest, sandboxed by static analysis, staged
outside the import path, and activated only after an administrator approval.

Lifecycle implemented here:

    DETECT -> ANALYZE -> PLAN -> CHECK PERMISSIONS -> CREATE BACKUP ->
    IMPLEMENT -> TEST -> SECURITY REVIEW -> VERIFY -> ACTIVATE -> MONITOR ->
    ROLLBACK IF NECESSARY

Every stage leaves a record in ``data/upgrades/history.json`` with the files it
touched and the hashes of the backup archive, so "roll back the last upgrade"
is a real restore, not a status message.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
import zipfile

from . import capabilities, config, runtime_paths

log = logging.getLogger("harry.selfdev")

_lock = threading.RLock()
_lockfile = None


def upgrades_root():
    override = os.environ.get("HARRY_UPGRADES_DIR", "").strip()
    path = os.path.abspath(override) if override else os.path.join(
        runtime_paths.workspace_root(), "data", "upgrades")
    os.makedirs(path, exist_ok=True)
    return path


def history_path():
    return os.path.join(upgrades_root(), "history.json")


def _load_history():
    try:
        with open(history_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_history(items):
    path = history_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items[-100:], f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _update(upgrade_id, **fields):
    with _lock:
        items = _load_history()
        for item in items:
            if item["id"] == upgrade_id:
                item.update(fields)
                item["updated"] = time.time()
                _save_history(items)
                return item
    return None


def _find(upgrade_id):
    for item in _load_history():
        if item["id"] == str(upgrade_id):
            return item
    return None


# ------------------------------------------------------------------ concurrency

def acquire(owner="harry"):
    """One upgrade at a time, across processes. Returns a token or None."""
    global _lockfile
    path = os.path.join(upgrades_root(), ".lock")
    now = time.time()
    try:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    held = json.load(f)
                if now - float(held.get("at") or 0) < 900:
                    return None
            except Exception:
                pass
        token = uuid.uuid4().hex
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"token": token, "owner": owner, "at": now}, f)
        _lockfile = (path, token)
        return token
    except Exception as e:
        log.warning("upgrade lock failed: %s", e)
        return None


def release(token):
    global _lockfile
    if not token:
        return
    path = os.path.join(upgrades_root(), ".lock")
    try:
        if _lockfile and _lockfile[0] == path:
            with open(path, "r", encoding="utf-8") as f:
                held = json.load(f)
            if held.get("token") == token:
                os.unlink(path)
    except Exception:
        pass
    _lockfile = None


# ------------------------------------------------------------------ backups

def _targets():
    """What a plugin upgrade is allowed to touch: never core modules.

    Returns ``(archive_name, absolute_path)`` pairs. Absolute paths are used so
    the checkpoint follows the configured plugin and registry locations instead
    of assuming a source checkout.
    """
    return [("plugins", os.path.abspath(capabilities.plugins_root())),
            ("data/capabilities.json", os.path.abspath(capabilities.registry_path()))]


def checkpoint(label="upgrade"):
    """Zip the permitted paths with a manifest of hashes. Returns the record."""
    backup_dir = os.path.join(upgrades_root(), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    backup_id = f"bk_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    archive = os.path.join(backup_dir, backup_id + ".zip")
    manifest = {}
    mapping = {}
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for prefix, source in _targets():
            if not os.path.exists(source):
                continue
            if os.path.isfile(source):
                files = {prefix: source}
            else:
                files = {}
                for base, _dirs, names in os.walk(source):
                    for name in names:
                        full = os.path.join(base, name)
                        relative = os.path.relpath(full, source).replace("\\", "/")
                        files[prefix + "/" + relative] = full
            for archive_name, full in files.items():
                try:
                    with open(full, "rb") as f:
                        data = f.read()
                except OSError:
                    continue
                bundle.writestr(archive_name, data)
                manifest[archive_name] = capabilities.hash_code(data)
                mapping[archive_name] = full
    record = {"id": backup_id, "archive": archive, "created": time.time(),
              "label": str(label)[:120], "files": manifest, "mapping": mapping}
    with open(archive + ".json", "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return record


def restore(backup_record):
    """Restore a checkpoint over the files it captured. Returns a result dict."""
    archive = backup_record.get("archive")
    if not archive or not os.path.exists(archive):
        return {"ok": False, "error": "backup archive is missing"}
    mapping = backup_record.get("mapping") or {}
    allowed_roots = [os.path.abspath(capabilities.plugins_root()),
                     os.path.abspath(os.path.dirname(capabilities.registry_path()))]
    restored = 0
    with zipfile.ZipFile(archive) as bundle:
        for name in bundle.namelist():
            clean = name.replace("\\", "/").lstrip("/")
            if clean.startswith("..") or ":" in clean:
                continue
            target = mapping.get(clean)
            if target:
                target = os.path.abspath(target)
                if not any(target == root or target.startswith(root + os.sep)
                           for root in allowed_roots):
                    continue
            else:
                # Older checkpoints (before mapping existed) used relative
                # names; rebuild those against the current locations.
                if not (clean.startswith("plugins/") or clean == "data/capabilities.json"):
                    continue
                base = (capabilities.plugins_root() if clean.startswith("plugins/")
                        else os.path.dirname(capabilities.registry_path()))
                suffix = clean.split("/", 1)[1] if "/" in clean else ""
                target = (os.path.join(base, suffix.replace("/", os.sep))
                          if clean.startswith("plugins/")
                          else capabilities.registry_path())
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with bundle.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            restored += 1
    if restored and os.path.exists(capabilities.registry_path()):
        capabilities._cache["path"] = None
    return {"ok": True, "restored": restored}


def backups():
    folder = os.path.join(upgrades_root(), "backups")
    out = []
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if name.endswith(".zip.json"):
                try:
                    with open(os.path.join(folder, name), "r", encoding="utf-8") as f:
                        out.append(json.load(f))
                except Exception:
                    pass
    out.sort(key=lambda b: b.get("created") or 0, reverse=True)
    return out


# ------------------------------------------------------------------ proposals

def _slug(text):
    words = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return (words or "plugin")[:30]


def analyse(request):
    """Inspect the installation before planning: no code is generated here."""
    from .tools import get_registry
    from . import selfheal
    tools = get_registry().names()
    found = capabilities.discover()
    return {
        "request": str(request)[:500],
        "tools": len(tools),
        "plugins": len(found),
        "invalid_plugins": [p["id"] for p in found if not p["valid"]],
        "unhealthy": [name for name, check in selfheal.diagnose().items()
                      if not check.get("ok")],
        "python": True,
        "capabilities": [c["name"] for c in capabilities.list_capabilities()],
    }


def plan_plugin(request, name=None, code=None, description=None, permissions=None):
    """Build a staged plugin proposal. Returns the upgrade record."""
    name = name or ("tool_" + _slug(request))
    description = description or str(request)[:200]
    permissions = permissions or ["safe"]
    problems = []
    if not capabilities.NAME_RE.match(str(name)):
        problems.append("plugin name is invalid")
    if code:
        ok, code_problems = capabilities.validate_plugin_code(code)
        problems.extend(code_problems)
    else:
        problems.append("no implementation was generated")
    upgrade_id = "up_" + uuid.uuid4().hex[:10]
    staging = os.path.join(upgrades_root(), upgrade_id)
    os.makedirs(staging, exist_ok=True)
    record = {
        "id": upgrade_id, "kind": "plugin", "name": name,
        "request": str(request)[:500], "description": str(description)[:300],
        "permissions": permissions, "status": "invalid" if problems else "awaiting_approval",
        "created": time.time(), "staging": staging, "problems": problems,
        "backup": None, "files": [], "tests": [], "activated": None,
        "approval": None, "history": ["detect", "analyze", "plan"],
    }
    if not problems:
        code_path = os.path.join(staging, "plugin.py")
        with open(code_path, "w", encoding="utf-8") as f:
            f.write(code)
        manifest = {
            "name": name, "version": "1.0.0", "description": description,
            "permissions": permissions, "entry": "plugin.py", "function": "run",
            "sha256": capabilities.hash_code(code),
        }
        with open(os.path.join(staging, "plugin.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        manifest_problems = capabilities.validate_manifest(
            manifest, code.encode("utf-8"))
        if manifest_problems:
            record["problems"] = manifest_problems
            record["status"] = "invalid"
        else:
            # A real syntax compile happens before approval, so broken code is
            # rejected without ever being imported.
            try:
                compile(code, "<plugin>", "exec")
            except SyntaxError as e:
                record["problems"] = [f"syntax error: {e}"]
                record["status"] = "invalid"
    with _lock:
        items = _load_history()
        items.append(record)
        _save_history(items)
    return record


def request_approval(upgrade_id, requested_by=None):
    from . import admin
    record = _find(upgrade_id)
    if not record:
        return {"ok": False, "error": "unknown upgrade"}
    if record["status"] != "awaiting_approval":
        return {"ok": False, "error": f"upgrade is {record['status']}, not awaiting approval"}
    result = admin.get_admin_store().request_approval(
        "activate_upgrade", {"upgrade_id": upgrade_id, "name": record["name"]},
        risk=admin.HIGH, requested_by=requested_by,
        note=f"install plugin {record['name']}")
    if result.get("ok"):
        _update(upgrade_id, approval=result["id"], history=record["history"] + ["approval_requested"])
    return result


def activate(upgrade_id, actor=None):
    """Install an approved, validated plugin and verify it is callable."""
    from . import admin
    record = _find(upgrade_id)
    if not record:
        return {"ok": False, "error": "unknown upgrade"}
    if record["status"] == "active":
        return {"ok": True, "upgrade_id": upgrade_id, "already": True}
    if record["status"] != "awaiting_approval":
        return {"ok": False, "error": f"upgrade is {record['status']}"}
    store = admin.get_admin_store()
    if not record.get("approval"):
        return {"ok": False, "error": "this upgrade needs administrator approval first",
                "needs_approval": True}
    if not store.approved(record["approval"], "activate_upgrade"):
        return {"ok": False, "error": "the approval is missing, denied or expired",
                "needs_approval": True}
    token = acquire(actor or "admin")
    if not token:
        return {"ok": False, "error": "another upgrade is in progress"}
    backup = None
    try:
        staging = record["staging"]
        code_path = os.path.join(staging, "plugin.py")
        manifest_path = os.path.join(staging, "plugin.json")
        with open(code_path, "rb") as f:
            code_bytes = f.read()
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        problems = capabilities.validate_manifest(manifest, code_bytes)
        ok, code_problems = capabilities.validate_plugin_code(
            code_bytes.decode("utf-8", "replace"))
        problems += code_problems
        if problems:
            _update(upgrade_id, status="invalid", problems=problems,
                    history=record["history"] + ["security_review_failed"])
            return {"ok": False, "error": "security review failed", "problems": problems}

        backup = checkpoint(f"before {record['name']}")
        _update(upgrade_id, backup=backup["id"], status="activating",
                history=record["history"] + ["security_review", "backup", "installing"])

        target = os.path.join(capabilities.plugins_root(), manifest["name"])
        if os.path.exists(target):
            _update(upgrade_id, status="awaiting_approval",
                    problems=["a plugin with this name is already installed"])
            return {"ok": False, "error": "a plugin with this name already exists"}
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copytree(staging, target, ignore=shutil.ignore_patterns("*.tmp"))
        capabilities.install_pending(approved_ids={manifest["name"]})
        from .tools import get_registry
        tool = get_registry().get(manifest["name"])
        if not tool:
            raise RuntimeError("the plugin was copied but did not register a tool")
        # Execute the installed code directly once: a plugin that cannot run is
        # removed instead of being advertised as working. A plugin that runs but
        # returns an error result is still installed (it needs input we did not
        # give), and the result is recorded honestly.
        from . import capabilities as caps
        entry = next((p for p in caps.discover() if p["id"] == manifest["name"]), None)
        if not entry or not entry["valid"]:
            raise RuntimeError("the installed plugin did not validate on disk")
        module = caps._import_plugin(entry)
        probe = module.run({})
        if not isinstance(probe, dict):
            raise RuntimeError("plugin run() must return a dict")
        smoke_ok = bool(probe.get("ok", True))
        capabilities.record_test(manifest["name"], smoke_ok,
                                 str(probe.get("error") or "")[:200])
        _update(upgrade_id, status="active", activated=time.time(),
                files=[os.path.join("plugins", manifest["name"], "plugin.py")],
                tests=[{"at": time.time(), "ok": smoke_ok,
                        "detail": "run({}) returned " + json.dumps(probe)[:200]}],
                history=record["history"] + ["test", "verify", "activate"])
        store.audit(actor, "upgrade_activated", {"upgrade": upgrade_id,
                                                 "plugin": manifest["name"]})
        return {"ok": True, "upgrade_id": upgrade_id, "plugin": manifest["name"],
                "tool": tool.name, "backup": backup["id"]}
    except Exception as e:
        log.warning("activation failed: %s", e)
        if backup:
            # Remove the half-installed plugin before restoring the checkpoint,
            # otherwise discovery would immediately re-register it as pending.
            try:
                shutil.rmtree(os.path.join(capabilities.plugins_root(),
                                           str(record["name"])), ignore_errors=True)
            except Exception:
                pass
            restore(backup)
            capabilities.install_pending(approved_ids=set())
            try:
                from .tools import get_registry as _registry
                _registry()._tools.pop(str(record["name"]), None)
            except Exception:
                pass
        _update(upgrade_id, status="failed", problems=[str(e)],
                history=record["history"] + ["activate_failed", "rolled_back"])
        store.audit(actor, "upgrade_failed", {"upgrade": upgrade_id, "error": str(e)})
        return {"ok": False, "error": str(e), "rolled_back": bool(backup)}
    finally:
        release(token)


def rollback(upgrade_id, actor=None):
    """Restore the checkpoint taken before an upgrade and unregister it."""
    from . import admin
    from .tools import get_registry
    record = _find(upgrade_id)
    if not record:
        return {"ok": False, "error": "unknown upgrade"}
    backup_id = record.get("backup")
    if not backup_id:
        return {"ok": False, "error": "this upgrade has no checkpoint"}
    record_backup = next((b for b in backups() if b["id"] == backup_id), None)
    if not record_backup:
        return {"ok": False, "error": "the checkpoint archive is missing"}
    token = acquire(actor or "admin")
    if not token:
        return {"ok": False, "error": "another upgrade is in progress"}
    try:
        result = restore(record_backup)
        plugin_folder = os.path.join(capabilities.plugins_root(), str(record["name"]))
        # The checkpoint restores files it captured; a plugin installed *by* this
        # upgrade was not in it, so it must be removed explicitly.
        if record.get("files"):
            shutil.rmtree(plugin_folder, ignore_errors=True)
        try:
            get_registry()._tools.pop(record["name"], None)
        except Exception:
            pass
        capabilities.disable(record["name"])
        _update(upgrade_id, status="rolled_back",
                history=record["history"] + ["rollback"],
                rollback={"at": time.time(), "restored": result.get("restored")})
        admin.get_admin_store().audit(actor, "upgrade_rolled_back",
                                      {"upgrade": upgrade_id, "name": record["name"]})
        return {"ok": True, "upgrade_id": upgrade_id, "restored": result.get("restored")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        release(token)


def list_upgrades(limit=30):
    items = _load_history()
    items.sort(key=lambda i: i.get("created") or 0, reverse=True)
    return items[:int(limit)]


def generate_plugin_code(request, name=None):
    """Ask the configured model for one plugin file. Never executed here."""
    from . import llm
    name = name or ("tool_" + _slug(request))
    prompt = (
        "Write a Python plugin for a personal assistant.\n"
        f"Name: {name}\nRequest: {request}\n\n"
        "Rules: exactly one file, a function run(args) -> dict. Only these imports "
        "are allowed: json, re, math, statistics, datetime, time, random, string, "
        "textwrap, html, collections, itertools, functools, csv, io, urllib.parse, "
        "base64, hashlib, requests, bs4, unicodedata, difflib, decimal. No file, "
        "network-write, subprocess, eval/exec, or dunder access. Return "
        '{"ok": true, ...}. Answer with JSON only: {"code": "...", "description": "..."}'
    )
    try:
        raw = llm.chat([{"role": "user", "content": prompt}], json_mode=True,
                       options={"num_predict": 900})
        import re as _re
        match = _re.search(r"\{.*\}", raw, _re.S)
        data = json.loads(match.group(0)) if match else {}
    except Exception as e:
        return {"ok": False, "error": f"the model could not produce a plugin: {e}"}
    code = str(data.get("code") or "")
    if not code.strip():
        return {"ok": False, "error": "the model returned no code"}
    return {"ok": True, "code": code,
            "description": str(data.get("description") or request)[:200], "name": name}

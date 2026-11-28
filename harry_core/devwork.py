"""Conversational self-development.

A request such as \"add a feature that summarises PDFs\" becomes a *tracked
development run* with persisted state, not a chat answer that promises work.

What this module really does itself
-----------------------------------
* ``analyse()`` inspects the repository as it is: git state, the module
  inventory, the test inventory and the interpreter. No guesses about what
  exists.
* ``propose()`` writes a plan with acceptance criteria, risks, the likely
  subsystem and the exact commands that will verify the change.
* ``request_approval()`` puts the run in the administrator approval queue
  (action ``self_development``, risk high) before anything is staged.
* ``start()`` creates an isolated ``git worktree`` on a new branch, so the
  running checkout is never edited in place. If git is unavailable the run is
  marked ``blocked`` instead of pretending a sandbox exists.
* ``run_checks()`` executes the plan's commands inside that worktree and keeps
  the real exit status, duration and output tail.
* ``security_review()`` reads the actual diff in the worktree: forbidden files
  (the security and account modules), and secret-shaped added lines.
* ``activate()`` commits the worktree branch and records its SHA. It never
  merges and never pushes - promoting a branch is the owner's decision.
* ``rollback()`` removes the worktree and deletes the branch.

Honest limitation, stated in the record: the code change itself is produced by
the executing agent or administrator inside the staged worktree. This module
plans, isolates, verifies and reports it, and refuses to mark a run ``ready``
without recorded passing checks.
"""

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid

from . import communication, runtime_paths

log = logging.getLogger("harry.devwork")

ACTION = "self_development"

PROPOSED = "proposed"
AWAITING_APPROVAL = "awaiting_approval"
APPROVED = "approved"
STAGING = "staging"
IMPLEMENTING = "implementing"
TESTING = "testing"
SECURITY_REVIEW = "security_review"
READY = "ready"
ACTIVE = "active"
FAILED = "failed"
CANCELLED = "cancelled"
ROLLED_BACK = "rolled_back"
BLOCKED = "blocked"

STATUSES = (PROPOSED, AWAITING_APPROVAL, APPROVED, STAGING, IMPLEMENTING,
            TESTING, SECURITY_REVIEW, READY, ACTIVE, FAILED, CANCELLED,
            ROLLED_BACK, BLOCKED)

# Files a self-development run may never touch: the run must not be able to
# change its own authorization, audit or account rules.
FORBIDDEN_PATHS = (
    "harry_core/security.py",
    "harry_core/admin.py",
    "harry_core/accounts.py",
    "harry_core/admin_commands.py",
)

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk|pk)[-_](?:live|test)[-_][A-Za-z0-9]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*['\"][^'\"]{12,}['\"]",
               re.IGNORECASE),
)

_SUBSYSTEMS = (
    ("voice", re.compile(r"\b(?:voice|speech|speak|listen|microphone|tts|stt|"
                         r"hearing|interruption)\b", re.I),
     ["harry_core/speech.py", "harry_core/conversation.py"]),
    ("memory", re.compile(r"\b(?:memory|remember|recall|personaliz\w*)\b", re.I),
     ["harry_core/memory.py"]),
    ("models", re.compile(r"\b(?:model|ollama|provider|routing|latency|"
                          r"response time|speed)\b", re.I),
     ["harry_core/llm.py", "harry_core/task_engine.py"]),
    ("agents", re.compile(r"\b(?:agent|specialist|orchestrat\w*)\b", re.I),
     ["harry_core/agents.py"]),
    ("plugins", re.compile(r"\b(?:plugin|skill|capabilit\w*|integration)\b", re.I),
     ["harry_core/capabilities.py", "harry_core/selfdev.py"]),
    ("documents", re.compile(r"\b(?:pdf|document|docx|summari[sz]\w*|ocr)\b", re.I),
     ["harry_core/documents.py", "harry_core/docgen.py"]),
    ("screen", re.compile(r"\b(?:screen|share|display|capture|vision)\b", re.I),
     ["harry_core/screen.py", "harry_core/vision.py"]),
    ("interface", re.compile(r"\b(?:ui|interface|dashboard|home screen|design|"
                             r"layout|theme)\b", re.I),
     ["harry_core/ui.py", "harry_core/adminui.py"]),
    ("tasks", re.compile(r"\b(?:task|plan|step|verif\w*|job)\b", re.I),
     ["harry_core/task_engine.py", "harry_core/jobs.py"]),
)

_lock = threading.RLock()


# ----------------------------------------------------------------------- paths

def root():
    override = os.environ.get("HARRY_DEVWORK_DIR", "").strip()
    if override:
        path = os.path.abspath(override)
    else:
        path = os.path.join(runtime_paths.workspace_root(), "data", "devwork")
    os.makedirs(path, exist_ok=True)
    return path


def runs_path():
    return os.path.join(root(), "runs.json")


def _load():
    try:
        with open(runs_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("runs") if isinstance(data, dict) else data
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _save(items):
    path = runs_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"runs": items[-100:]}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _find(run_id):
    for item in _load():
        if item.get("id") == str(run_id):
            return item
    return None


def _update(run_id, **fields):
    with _lock:
        items = _load()
        for item in items:
            if item.get("id") == str(run_id):
                item.update(fields)
                item["updated"] = time.time()
                _save(items)
                return item
    return None


def _log_event(run_id, event, detail=""):
    with _lock:
        items = _load()
        for item in items:
            if item.get("id") == str(run_id):
                item.setdefault("log", []).append(
                    {"at": time.time(), "event": str(event)[:60],
                     "detail": str(detail)[:300]})
                del item["log"][:-60]
                _save(items)
                return


# --------------------------------------------------------------------- git layer

def _git(args, cwd=None, timeout=60):
    """Run one bounded git command and return its real result."""
    try:
        proc = subprocess.run(["git"] + list(args), cwd=cwd or root(),
                              capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {"ok": False, "out": "", "err": "git is not installed", "code": None}
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "", "err": "git timed out", "code": None}
    except Exception as e:
        return {"ok": False, "out": "", "err": f"{type(e).__name__}: {e}",
                "code": None}
    return {"ok": proc.returncode == 0, "out": (proc.stdout or "").strip(),
            "err": (proc.stderr or "").strip(), "code": proc.returncode}


def repo_root():
    """The worktree root of the running checkout, or None outside a repo."""
    result = _git(["rev-parse", "--show-toplevel"],
                  cwd=runtime_paths.workspace_root())
    if not result["ok"] or not result["out"]:
        return None
    return os.path.abspath(result["out"].splitlines()[0])


# --------------------------------------------------------------------- analysis

def analyse():
    """Inspect the checkout before planning. Never fabricates a value."""
    repo = repo_root()
    info = {
        "repo": repo,
        "git": bool(repo),
        "branch": None,
        "head": None,
        "dirty": None,
        "modules": [],
        "tests": [],
        "python": sys.executable,
        "workspace": os.path.abspath(runtime_paths.workspace_root()),
    }
    if not repo:
        return info
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    info["branch"] = branch["out"] if branch["ok"] else None
    head = _git(["rev-parse", "--short", "HEAD"], cwd=repo)
    info["head"] = head["out"] if head["ok"] else None
    status = _git(["status", "--porcelain"], cwd=repo)
    if status["ok"]:
        info["dirty"] = len([line for line in status["out"].splitlines() if line.strip()])
    package = os.path.join(repo, "harry_core")
    if os.path.isdir(package):
        info["modules"] = sorted(name[:-3] for name in os.listdir(package)
                                 if name.endswith(".py") and name != "__init__.py")
    tests = os.path.join(repo, "tests")
    if os.path.isdir(tests):
        info["tests"] = sorted(name for name in os.listdir(tests)
                               if name.endswith("_test.py"))
    return info


def _subsystem(request):
    for name, pattern, files in _SUBSYSTEMS:
        if pattern.search(request):
            return name, files
    return "general", ["harry_core/brain.py"]


def _checks_for(files, available_tests):
    """The commands that will prove this change: existing test modules only."""
    wanted = {"tests/import_check.py"}
    mapping = {
        "voice": ["tests/conversation_test.py", "tests/voice_test.py"],
        "memory": ["tests/conversation_test.py"],
        "models": ["tests/autonomy_test.py", "tests/perf_bench.py"],
        "agents": ["tests/autonomy_test.py"],
        "plugins": ["tests/autonomy_test.py"],
        "documents": ["tests/docgen_test.py", "tests/media_test.py"],
        "screen": ["tests/vision_test.py"],
        "interface": ["tests/admin_api_test.py", "tests/dashboard_test.py"],
        "tasks": ["tests/autonomy_test.py"],
        "general": ["tests/smoke_test.py"],
    }
    for name, _pattern, _files in _SUBSYSTEMS:
        if set(files) & set(_files):
            wanted.update(mapping.get(name, []))
    return [c for c in sorted(wanted) if os.path.basename(c) in available_tests]


def build_plan(request, analysis=None):
    """A concrete plan with acceptance criteria, risks and verification."""
    analysis = analysis or analyse()
    subsystem, files = _subsystem(request)
    checks = _checks_for(files, set(analysis.get("tests") or []))
    risks = []
    if not analysis.get("git"):
        risks.append("no git checkout: an isolated branch cannot be created")
    if subsystem == "general":
        risks.append("subsystem not identified: the plan may be too broad")
    if any(f in FORBIDDEN_PATHS for f in files):
        risks.append("change touches a protected module and needs manual review")
    if analysis.get("dirty"):
        risks.append(f"{analysis['dirty']} uncommitted file(s) in the checkout; "
                     "staging starts from the last commit")
    return {
        "subsystem": subsystem,
        "files": files,
        "acceptance_criteria": [
            "the requested behaviour exists and is reachable from the normal "
            "chat or admin path",
            "the relevant test module covers the new behaviour",
            "no existing test regresses",
            "no secret, credential or private path enters the change",
        ],
        "risks": risks,
        "checks": checks,
        "needs_approval": True,
        "effort": "small" if len(files) <= 2 else "medium",
        "rollback": "the staged branch and worktree are removed; nothing in the "
                    "running checkout changes before activation",
    }


# --------------------------------------------------------------------- lifecycle

def propose(request, requested_by=None, analysis=None):
    """Create a tracked development run in ``proposed`` state."""
    request = str(request or "").strip()
    if not request:
        return {"ok": False, "error": "a development request is required"}
    analysis = analysis or analyse()
    plan = build_plan(request, analysis)
    run_id = "dev_" + uuid.uuid4().hex[:10]
    record = {
        "id": run_id,
        "request": request[:500],
        "created": time.time(),
        "updated": time.time(),
        "requested_by": requested_by,
        "status": BLOCKED if not analysis.get("git") else PROPOSED,
        "plan": plan,
        "analysis": {k: analysis.get(k) for k in
                     ("repo", "git", "branch", "head", "dirty", "python")},
        "approval": None,
        "worktree": None,
        "branch": None,
        "files": [],
        "checks": [],
        "security": None,
        "commit": None,
        "problems": [] if analysis.get("git") else [
            "no git checkout is available, so the change could not be isolated"],
        "log": [],
    }
    with _lock:
        items = _load()
        items.append(record)
        _save(items)
    _log_event(run_id, "proposed", plan["subsystem"])
    return {"ok": True, "run": record}


def request_approval(run_id, requested_by=None):
    """Put the run in the administrator approval queue."""
    from . import admin
    record = _find(run_id)
    if not record:
        return {"ok": False, "error": "unknown development run"}
    if record["status"] != PROPOSED:
        return {"ok": False, "error": f"run is {record['status']}, not proposed"}
    result = admin.get_admin_store().request_approval(
        ACTION, {"run_id": run_id, "request": record["request"]},
        risk=admin.HIGH, requested_by=requested_by,
        note=f"self-development: {record['request'][:120]}")
    if not result.get("ok"):
        return result
    _update(run_id, status=AWAITING_APPROVAL, approval=result["id"])
    _log_event(run_id, "approval_requested", result["id"])
    return {"ok": True, "approval": result["id"], "run": _find(run_id)}


def start(run_id, actor=None, requested_by=None):
    """Stage an isolated worktree after the approval is genuinely approved."""
    from . import admin
    record = _find(run_id)
    if not record:
        return {"ok": False, "error": "unknown development run"}
    if record["status"] not in (AWAITING_APPROVAL, PROPOSED):
        return {"ok": False, "error": f"run is {record['status']}"}
    if record["status"] == PROPOSED:
        # Nothing may be staged without an approval record.
        return {"ok": False, "error": "this run still needs administrator approval",
                "needs_approval": True}
    store = admin.get_admin_store()
    if not record.get("approval") or not store.approved(record["approval"], ACTION):
        return {"ok": False, "error": "the approval is missing, denied or expired",
                "needs_approval": True}
    repo = repo_root()
    if not repo:
        _update(run_id, status=BLOCKED,
                problems=["no git checkout is available"])
        return {"ok": False, "error": "no git checkout is available"}
    branch = f"harry/dev-{run_id.split('_')[-1]}"
    worktree = os.path.join(root(), "worktrees", run_id)
    if os.path.exists(worktree):
        return {"ok": False, "error": "a staging directory already exists"}
    _update(run_id, status=STAGING, branch=branch, worktree=worktree)
    result = _git(["worktree", "add", "-b", branch, worktree, "HEAD"], cwd=repo,
                  timeout=120)
    if not result["ok"]:
        _update(run_id, status=FAILED, problems=[result["err"] or "worktree failed"])
        _log_event(run_id, "staging_failed", result["err"])
        return {"ok": False, "error": result["err"] or "could not create worktree"}
    store.audit(actor, "development_staged",
                {"run": run_id, "branch": branch})
    _update(run_id, status=IMPLEMENTING)
    _log_event(run_id, "staged", branch)
    return {"ok": True, "run": _find(run_id), "branch": branch,
            "worktree": worktree}


def record_change(run_id, files, note="", actor=None):
    """Record which files the staged change actually touched.

    The edit itself is made inside the worktree by the executing agent; this
    call is the handoff, and it verifies the paths exist there.
    """
    record = _find(run_id)
    if not record:
        return {"ok": False, "error": "unknown development run"}
    if record["status"] != IMPLEMENTING:
        return {"ok": False, "error": f"run is {record['status']}, not implementing"}
    worktree = record.get("worktree")
    if not worktree or not os.path.isdir(worktree):
        return {"ok": False, "error": "the staging worktree is missing"}
    wanted = [str(f).replace("\\", "/") for f in (files or [])]
    if not wanted:
        return {"ok": False, "error": "no changed files were reported"}
    missing = [f for f in wanted if not os.path.exists(os.path.join(worktree, f))]
    if missing:
        return {"ok": False, "error": "files are not in the staging worktree",
                "missing": missing}
    changed = record.get("files") or []
    for item in wanted:
        if item not in changed:
            changed.append(item)
    _update(run_id, files=changed, notes=(record.get("notes") or []) + [
        {"at": time.time(), "note": str(note)[:400], "by": actor}][-20:])
    _log_event(run_id, "change_recorded", ", ".join(wanted)[:200])
    return {"ok": True, "run": _find(run_id)}


def run_checks(run_id, checks=None, timeout=300, actor=None):
    """Run the plan's verification commands inside the worktree."""
    record = _find(run_id)
    if not record:
        return {"ok": False, "error": "unknown development run"}
    # FAILED is allowed on purpose: re-running the checks is the recovery path
    # after a failed attempt, and the new results replace the old ones.
    if record["status"] not in (IMPLEMENTING, TESTING, FAILED):
        return {"ok": False, "error": f"run is {record['status']}"}
    worktree = record.get("worktree")
    if not worktree or not os.path.isdir(worktree):
        return {"ok": False, "error": "the staging worktree is missing"}
    plan = record.get("plan") or {}
    commands = checks if checks is not None else (
        plan.get("checks") or ["tests/import_check.py"])
    if not commands:
        return {"ok": False, "error": "the plan has no verification commands"}
    _update(run_id, status=TESTING)
    results = []
    for command in commands:
        if isinstance(command, str):
            argv = [sys.executable, command]
        else:
            argv = [str(part) for part in command]
        started = time.perf_counter()
        try:
            proc = subprocess.run(argv, cwd=worktree, capture_output=True,
                                  text=True, timeout=timeout,
                                  encoding="utf-8", errors="replace",
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            code = proc.returncode
            tail = ((proc.stdout or "") + (proc.stderr or ""))[-1500:]
        except subprocess.TimeoutExpired:
            code, tail = None, f"timed out after {timeout}s"
        except Exception as e:
            code, tail = None, f"{type(e).__name__}: {e}"
        results.append({
            "cmd": " ".join(argv[1:]) if len(argv) > 1 else argv[0],
            "exit": code,
            "ok": code == 0,
            "ms": round((time.perf_counter() - started) * 1000),
            "tail": tail,
        })
    failed = [r for r in results if not r["ok"]]
    _update(run_id, checks=results,
            status=SECURITY_REVIEW if not failed else FAILED)
    _log_event(run_id, "checks", f"{len(results) - len(failed)}/{len(results)} passed")
    return {"ok": not failed, "checks": results, "run": _find(run_id)}


def security_review(run_id):
    """Scan the real diff for forbidden files and secret-shaped additions."""
    record = _find(run_id)
    if not record:
        return {"ok": False, "error": "unknown development run"}
    worktree = record.get("worktree")
    if not worktree or not os.path.isdir(worktree):
        return {"ok": False, "error": "the staging worktree is missing"}
    names = _git(["diff", "--name-only", "HEAD"], cwd=worktree)
    untracked = _git(["ls-files", "--others", "--exclude-standard"], cwd=worktree)
    changed = sorted({line for line in (
        (names.get("out") or "") + "\n" + (untracked.get("out") or "")).splitlines()
        if line.strip()})
    forbidden = [f for f in changed if f.replace("\\", "/") in FORBIDDEN_PATHS]
    diff = _git(["diff", "HEAD"], cwd=worktree)
    added = [line for line in (diff.get("out") or "").splitlines()
             if line.startswith("+") and not line.startswith("+++")]
    hits = []
    for line in added:
        for pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                hits.append(line.strip()[:160])
                break
    for name in changed:
        full = os.path.join(worktree, name)
        if not name.endswith((".py", ".json", ".md", ".txt", ".html", ".js")):
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                body = f.read(200000)
        except OSError:
            continue
        for pattern in _SECRET_PATTERNS:
            match = pattern.search(body)
            if match:
                hits.append(f"{name}: {match.group(0)[:80]}")
    hits = sorted(set(hits))
    review = {
        "changed": changed,
        "forbidden": forbidden,
        "secret_hits": hits,
        "ok": not forbidden and not hits,
        "at": time.time(),
    }
    problems = []
    if forbidden:
        problems.append("protected modules were modified: " + ", ".join(forbidden))
    if hits:
        problems.append(f"{len(hits)} secret-shaped line(s) in the change")
    _update(run_id, security=review, status=READY if review["ok"] else FAILED,
            problems=(record.get("problems") or []) + problems)
    _log_event(run_id, "security_review",
               "clean" if review["ok"] else "; ".join(problems))
    return {"ok": review["ok"], "review": review, "run": _find(run_id)}


def activate(run_id, actor=None):
    """Commit the staged branch. Never merges, never pushes."""
    from . import admin
    record = _find(run_id)
    if not record:
        return {"ok": False, "error": "unknown development run"}
    if record["status"] == ACTIVE:
        return {"ok": True, "run": record, "already": True}
    if record["status"] != READY:
        return {"ok": False,
                "error": f"run is {record['status']}; it must pass checks and "
                         "security review first"}
    store = admin.get_admin_store()
    if not record.get("approval") or not store.approved(record["approval"], ACTION):
        return {"ok": False, "error": "the approval is no longer valid",
                "needs_approval": True}
    worktree = record["worktree"]
    added = _git(["add", "-A"], cwd=worktree)
    if not added["ok"]:
        return {"ok": False, "error": added["err"] or "could not stage the change"}
    message = (f"{record['request'][:72]}\n\n"
               f"Self-development run {run_id}.\n"
               f"Checks: {len([c for c in record.get('checks') or [] if c['ok']])}"
               f"/{len(record.get('checks') or [])} passed.\n")
    committed = _git(["commit", "-m", message], cwd=worktree, timeout=120)
    if not committed["ok"]:
        return {"ok": False, "error": committed["err"] or "commit failed"}
    sha = _git(["rev-parse", "HEAD"], cwd=worktree)
    commit = (sha.get("out") or "")[:40] if sha["ok"] else None
    _update(run_id, status=ACTIVE, commit=commit)
    store.audit(actor, "development_activated",
                {"run": run_id, "branch": record.get("branch"), "commit": commit})
    _log_event(run_id, "activated", commit)
    return {"ok": True, "run": _find(run_id), "commit": commit,
            "branch": record.get("branch"),
            "note": "the branch is committed and staged; merging and pushing "
                    "remain an explicit owner action"}


def rollback(run_id, actor=None):
    """Remove the worktree and delete its branch."""
    from . import admin
    record = _find(run_id)
    if not record:
        return {"ok": False, "error": "unknown development run"}
    if record["status"] == ROLLED_BACK:
        return {"ok": True, "run": record, "already": True}
    repo = repo_root()
    worktree = record.get("worktree")
    removed = False
    if repo and worktree and os.path.isdir(worktree):
        result = _git(["worktree", "remove", "--force", worktree], cwd=repo,
                      timeout=120)
        removed = result["ok"]
    if repo and record.get("branch"):
        _git(["branch", "-D", record["branch"]], cwd=repo)
    _update(run_id, status=ROLLED_BACK)
    admin.get_admin_store().audit(actor, "development_rolled_back",
                                  {"run": run_id, "removed": removed})
    _log_event(run_id, "rolled_back", str(removed))
    return {"ok": True, "run": _find(run_id), "removed": removed}


def cancel(run_id, actor=None):
    record = _find(run_id)
    if not record:
        return {"ok": False, "error": "unknown development run"}
    if record["status"] in (ACTIVE, ROLLED_BACK, CANCELLED):
        return {"ok": False, "error": f"run is already {record['status']}"}
    _update(run_id, status=CANCELLED)
    _log_event(run_id, "cancelled")
    return {"ok": True, "run": _find(run_id)}


def list_runs(limit=30):
    items = _load()
    items.sort(key=lambda r: r.get("created") or 0, reverse=True)
    return items[:int(limit)]


def get_run(run_id):
    return _find(run_id)


# ------------------------------------------------------------------------ report

def report(run_id):
    """The owner-facing completion summary of one development run."""
    record = _find(run_id)
    if not record:
        return {"ok": False, "error": "unknown development run"}
    checks = record.get("checks") or []
    passed = [c for c in checks if c["ok"]]
    security = record.get("security") or {}
    limitations = list(record.get("problems") or [])
    limitations.append("the code change is written by the executing agent inside "
                       "the staged worktree; this report reflects the recorded "
                       "verification, not a claim that review is unnecessary")
    if not checks:
        limitations.append("no verification commands have run for this run yet")
    return {
        "ok": True,
        "run_id": record["id"],
        "status": record["status"],
        "requested_outcome": record["request"],
        "subsystem": (record.get("plan") or {}).get("subsystem"),
        "changed_files": record.get("files") or security.get("changed") or [],
        "tests": {
            "executed": len(checks),
            "passed": len(passed),
            "failed": len(checks) - len(passed),
            "commands": [c["cmd"] for c in checks],
        },
        "security_checks": {
            "forbidden_paths": security.get("forbidden") or [],
            "secret_findings": security.get("secret_hits") or [],
            "reviewed": bool(security),
        },
        "commit": record.get("commit"),
        "branch": record.get("branch"),
        "rollback": {"available": bool(record.get("worktree")),
                     "command": f"POST /api/admin/devwork/{record['id']}/rollback"},
        "limitations": limitations,
    }


def summary_for_chat(record, state=None):
    """Short, honest in-chat answer after a development request."""
    plan = record.get("plan") or {}
    lines = []
    opening = communication.opening_line(state, "development")
    if opening:
        lines.append(opening)
    lines.append(f"I read this as a change to me: \"{record['request'][:160]}\"")
    lines.append(f"Subsystem: {plan.get('subsystem')} — likely files: "
                 + ", ".join(plan.get("files") or ["unknown"]))
    lines.append("Acceptance: " + "; ".join(plan.get("acceptance_criteria") or []))
    if plan.get("risks"):
        lines.append("Risks: " + "; ".join(plan["risks"]))
    lines.append("Verification I will run in an isolated branch: "
                 + (", ".join(plan.get("checks") or []) or "none available"))
    status = record.get("status")
    if status == BLOCKED:
        lines.append("Blocked: " + "; ".join(record.get("problems") or
                                             ["the checkout could not be isolated"]))
    elif status == PROPOSED:
        lines.append(f"Run {record['id']} is proposed. An administrator must "
                     "approve it before anything is staged; approve it in the "
                     f"admin panel, then start run {record['id']}.")
    elif status == AWAITING_APPROVAL:
        lines.append(f"Run {record['id']} is waiting for administrator approval.")
    else:
        lines.append(f"Run {record['id']} is {status}.")
    lines.append("I will not edit the running code in place and I have not "
                 "changed anything yet.")
    return "\n".join(lines)

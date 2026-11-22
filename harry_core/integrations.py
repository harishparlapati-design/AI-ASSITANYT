"""Local integration discovery and guarded development workflows.

This module reports facts from installed tools and the current Git checkout.
Agent subprocesses run in read-only modes, inherit existing CLI authentication,
and never write their diagnostic output to Harry's logs.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from . import config, runtime_paths

_AGENT_LOCK = threading.RLock()
_AGENT_JOBS = {}
_AGENT_TTL = 120
_AUTH_CACHE = {}
_AUTH_INFLIGHT = set()
_AUTH_LOCK = threading.Lock()
_LAST_EXECUTION = {}
# Matched against a CLI's private diagnostics only to pick a clearer message.
_AUTH_FAILURE = re.compile(
    r"refresh token|unauthorized|\b401\b|invalid api key|not logged in|"
    r"please log ?in|sign in again|authentication", re.IGNORECASE,
)


def _run(args, cwd=None, timeout=15, input_text=None):
    """Run a fixed argument vector without a shell and capture output privately."""
    try:
        return subprocess.run(
            args, cwd=str(cwd or runtime_paths.workspace_root()),
            input=input_text, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            shell=False, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git(*args, timeout=15):
    return _run(["git", "--no-optional-locks", *args], timeout=timeout)


def _git_text(*args, timeout=15):
    result = _git(*args, timeout=timeout)
    # Preserve leading spaces: porcelain status uses column 1 for the index
    # state, and stripping it corrupts the first changed path in snapshots.
    return result.stdout.rstrip("\r\n") if result and result.returncode == 0 else ""


def _repo_root():
    root = _git_text("rev-parse", "--show-toplevel")
    return Path(root) if root else None


def _remote_name(remote):
    """Return a safe owner/repo label, never credentials or arbitrary URL data."""
    value = str(remote or "").strip()
    if not value:
        return ""
    if value.startswith("git@github.com:"):
        path = value.split(":", 1)[1]
    else:
        parsed = urlparse(value)
        if (parsed.hostname or "").lower() != "github.com":
            return ""
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").strip("/")
    return path if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path) else ""


def _auth(tool, command, ttl=30):
    """Synchronously verify a CLI session before a requested operation."""
    now = time.time()
    cached = _AUTH_CACHE.get(tool)
    if cached and now - cached[0] < ttl and not cached[1].get("checking"):
        return dict(cached[1])
    executable = shutil.which(command[0])
    if not executable:
        value = {"installed": False, "authenticated": False,
                 "auth_state": "not installed", "checking": False}
    else:
        result = _run([executable, *command[1:]], timeout=12)
        if result is None:
            value = {"installed": True, "authenticated": None,
                     "auth_state": "status unavailable", "checking": False}
        else:
            authenticated = result.returncode == 0
            value = {"installed": True, "authenticated": authenticated,
                     "auth_state": "authenticated" if authenticated else "sign-in required",
                     "checking": False}
    with _AUTH_LOCK:
        _AUTH_CACHE[tool] = (time.time(), value)
        _AUTH_INFLIGHT.discard(tool)
    return dict(value)


def _refresh_auth_cache(tool, executable, args):
    """Refresh one CLI's auth status off the HTTP request thread."""
    try:
        result = _run([executable, *args], timeout=12)
        if result is None:
            value = {"installed": True, "authenticated": None,
                     "auth_state": "status unavailable", "checking": False}
        else:
            authenticated = result.returncode == 0
            value = {"installed": True, "authenticated": authenticated,
                     "auth_state": "authenticated" if authenticated else "sign-in required",
                     "checking": False}
    except Exception:
        value = {"installed": True, "authenticated": None,
                 "auth_state": "status unavailable", "checking": False}
    finally:
        with _AUTH_LOCK:
            _AUTH_CACHE[tool] = (time.time(), value)
            _AUTH_INFLIGHT.discard(tool)


def _auth_cached(tool, command, ttl=30, enabled=True):
    """Return cached CLI status quickly and refresh stale status in background."""
    executable = shutil.which(command[0])
    if not executable:
        return {"installed": False, "authenticated": False,
                "auth_state": "not installed", "checking": False}
    if not enabled:
        return {"installed": True, "authenticated": None,
                "auth_state": "disabled", "checking": False}
    now = time.time()
    with _AUTH_LOCK:
        cached = _AUTH_CACHE.get(tool)
        if cached and now - cached[0] < ttl:
            return dict(cached[1])
        if tool not in _AUTH_INFLIGHT:
            _AUTH_INFLIGHT.add(tool)
            thread = threading.Thread(
                target=_refresh_auth_cache,
                args=(tool, executable, command[1:]),
                daemon=True, name="auth-check-" + tool,
            )
            thread.start()
        if cached:
            value = dict(cached[1])
            value["checking"] = True
            return value
    return {"installed": True, "authenticated": None,
            "auth_state": "checking", "checking": True}


def github_snapshot(include_activity=False, check_github=True):
    root = _repo_root()
    branch = _git_text("branch", "--show-current") if root else ""
    head = _git_text("rev-parse", "HEAD") if root else ""
    status = _git_text("status", "--porcelain=v1", "--untracked-files=all") if root else ""
    files = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1]
        files.append({"code": line[:2].strip() or "??", "path": path})
    commits = []
    if root:
        log = _git_text("log", "-8", "--format=%H%x1f%s")
        for line in log.splitlines():
            parts = line.split("\x1f", 1)
            if len(parts) == 2:
                commits.append({"hash": parts[0], "subject": parts[1][:180]})
    remote_url = _git_text("remote", "get-url", "origin") if root else ""
    repo = _remote_name(remote_url)
    # gh auth status is a local CLI check. Report its actual state even when
    # disabled so the dashboard can distinguish disabled from unavailable.
    auth = (_auth_cached("github", ["gh", "auth", "status"],
                         enabled=check_github)
            if root else {"installed": False, "authenticated": False,
                         "auth_state": "not installed", "checking": False})
    remote_tip = _git_text("rev-parse", "refs/remotes/origin/dev") if root else ""
    ahead = behind = None
    if root and branch == "dev" and remote_tip:
        counts = _git_text("rev-list", "--left-right", "--count", "dev...origin/dev")
        if counts:
            try:
                ahead, behind = (int(part) for part in counts.split())
            except (ValueError, TypeError):
                pass
    result = {
        "available": bool(root),
        "root": str(root) if root else "",
        "branch": branch,
        "head": head,
        "clean": bool(root) and not status,
        "changed_files": files,
        "commits": commits,
        "repository": repo,
        "github_cli": auth,
        "remote_dev": remote_tip,
        "remote_dev_is_cached": bool(remote_tip),
        "ahead": ahead,
        "behind": behind,
    }
    if include_activity:
        result["activity"] = github_activity()
    return result


def github_activity():
    """Read recent issues and pull requests through authenticated gh, if present."""
    if not (config.CONFIG.get("integrations", {}) or {}).get("github_enabled", True):
        return {"available": False, "reason": "GitHub integration is disabled",
                "issues": [], "pull_requests": [], "errors": []}
    snap = github_snapshot(include_activity=False)
    if not snap["repository"]:
        return {"available": False, "reason": "origin is not a GitHub repository",
                "issues": [], "pull_requests": [], "errors": []}
    if not snap["github_cli"]["installed"]:
        return {"available": False, "reason": "GitHub CLI is not installed",
                "issues": [], "pull_requests": [], "errors": []}
    if snap["github_cli"]["authenticated"] is not True:
        return {"available": False, "reason": "GitHub CLI is not authenticated",
                "issues": [], "pull_requests": [], "errors": []}
    repo = snap["repository"]
    out = {"available": True, "issues": [], "pull_requests": [], "errors": []}
    for key, label, args in (
        ("issues", "issues", ["issue", "list", "--repo", repo, "--limit", "8",
                     "--json", "number,title,state,url"]),
        ("pull_requests", "pull requests", ["pr", "list", "--repo", repo, "--limit", "8",
                            "--json", "number,title,state,url"]),
    ):
        executable = shutil.which("gh")
        response = _run([executable, *args], timeout=20) if executable else None
        if response is None or response.returncode != 0:
            out["errors"].append(
                f"Could not load GitHub {label}; check CLI sign-in and repository access."
            )
            continue
        try:
            entries = json.loads(response.stdout)
            if not isinstance(entries, list):
                raise ValueError("expected a list")
            out[key] = [{"number": int(x["number"]),
                         "title": str(x.get("title", ""))[:160],
                         "state": str(x.get("state", "")),
                         "url": str(x.get("url", ""))}
                        for x in entries if isinstance(x, dict)]
        except (ValueError, TypeError, KeyError):
            out["errors"].append(
                f"GitHub returned an unreadable {label} response."
            )
    _record_execution("github", {
        "ok": not out["errors"],
        "result": "Loaded GitHub issues and pull requests." if not out["errors"] else None,
        "error": "; ".join(out["errors"]) if out["errors"] else None,
    })
    return out


def refresh_github():
    """Refresh the cached origin/dev ref only on an explicit dashboard request."""
    if not (config.CONFIG.get("integrations", {}) or {}).get("github_enabled", True):
        return {"ok": False, "error": "GitHub integration is disabled."}
    root = _repo_root()
    if not root:
        return {"ok": False, "error": "This directory is not a Git repository."}
    if _git_text("branch", "--show-current") != "dev":
        return {"ok": False, "error": "GitHub operations are restricted to the dev branch."}
    repo = _remote_name(_git_text("remote", "get-url", "origin"))
    if not repo:
        return {"ok": False, "error": "origin is not a GitHub repository."}
    if not shutil.which("gh"):
        return {"ok": False, "error": "GitHub CLI is not installed."}
    auth = _auth("github", ["gh", "auth", "status"])
    if not auth["authenticated"]:
        return {"ok": False, "error": "GitHub CLI is not authenticated."}
    result = _run(["git", "fetch", "--quiet", "origin",
                   "refs/heads/dev:refs/remotes/origin/dev"], cwd=root, timeout=45)
    if not result or result.returncode:
        return {"ok": False, "error": "Could not refresh origin/dev. Check network and repository access."}
    snap = github_snapshot()
    _record_execution("github", {"ok": True, "result": "Fetched origin/dev."})
    return {"ok": True, "github": snap}


def _record_execution(name, result):
    """Remember a small, credential-free summary of the actual last operation."""
    status = result.get("status") or ("succeeded" if result.get("ok") else "failed")
    summary = str(result.get("result") or result.get("error")
                  or result.get("message") or result.get("branch")
                  or result.get("commit") or "Completed")[:180]
    with _AGENT_LOCK:
        _LAST_EXECUTION[name] = {"status": status, "summary": summary,
                                 "at": time.time()}


def _safe_git_mutation():
    root = _repo_root()
    if not root:
        return None, "This directory is not a Git repository."
    if _git_text("branch", "--show-current") != "dev":
        return None, "Git operations are restricted to the dev branch."
    return root, ""


def create_branch(name, approved=False):
    if not approved:
        return {"ok": False, "error": "Explicit confirmation is required."}
    name = str(name or "").strip()
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}", name)
            or name in ("main", "dev") or name.startswith("-")
            or ".." in name or name.endswith("/") or "//" in name):
        return {"ok": False, "error": "Invalid or reserved branch name."}
    root, error = _safe_git_mutation()
    if error:
        return {"ok": False, "error": error}
    if not _git_text("check-ref-format", "--branch", name):
        return {"ok": False, "error": "Invalid branch name."}
    result = _run(["git", "branch", name, "dev"], cwd=root)
    if not result or result.returncode:
        failure = {"ok": False, "error": "Git could not create that branch."}
        _record_execution("git", failure)
        return failure
    response = {"ok": True, "branch": name, "based_on": "dev",
                "result": "Created local branch from dev."}
    _record_execution("git", response)
    return response


_BLOCKED_PATH = re.compile(
    r"(^|/)(?:config\.json|\.env(?:\..*)?|_backups|voice_samples|data|"
    r"memory|memory\.json|conversation_memory\.json|vosk-model-[^/]*|"
    r"venv|\.venv|secrets?|credentials?)(?:/|$)|"
    r"(?:\.pkl|\.wav|\.pem|\.key|\.p12|\.pfx)$", re.I
)


def commit_paths(paths, message, approved=False):
    if not approved:
        return {"ok": False, "error": "Explicit confirmation is required."}
    root, error = _safe_git_mutation()
    if error:
        return {"ok": False, "error": error}
    if not isinstance(paths, list) or not paths or len(paths) > 100:
        return {"ok": False, "error": "Select one or more changed project files."}
    safe = []
    for item in paths:
        path = str(item or "").replace("\\", "/").strip()
        if (not path or path.startswith("/") or ":" in path
                or any(part in ("", ".", "..", ".git") for part in path.split("/"))
                or _BLOCKED_PATH.search(path)):
            return {"ok": False, "error": "One or more selected paths are protected."}
        safe.append(path)
    safe = list(dict.fromkeys(safe))
    message = str(message or "").strip()
    if not message or len(message) > 160 or "\n" in message or "\r" in message:
        return {"ok": False, "error": "Commit message must be one line, up to 160 characters."}
    # Never fold someone else's staged work into a dashboard commit. This also
    # makes the failure cleanup below safe: only paths staged by this request
    # can be reset.
    staged_before = _git_text("diff", "--cached", "--name-only").splitlines()
    if staged_before:
        return {"ok": False, "error": "Commit the existing staged changes separately first."}
    status = _git_text("status", "--porcelain=v1", "--untracked-files=all")
    changed = {line[3:].split(" -> ")[-1].replace("\\", "/") for line in status.splitlines()
               if len(line) >= 4}
    if any(path not in changed for path in safe):
        return {"ok": False, "error": "A selected path is no longer changed."}
    staged = _run(["git", "add", "--", *safe], cwd=root)
    if not staged or staged.returncode:
        return {"ok": False, "error": "Git could not stage the selected paths."}
    staged_names = _git_text("diff", "--cached", "--name-only").splitlines()
    if (set(staged_names) != set(safe)
            or any(_BLOCKED_PATH.search(name.replace("\\", "/")) for name in staged_names)):
        _run(["git", "reset", "--", *safe], cwd=root)
        return {"ok": False, "error": "Git staged unexpected or protected paths; staging was reset."}
    diff = _run(["git", "diff", "--cached", "--unified=0"], cwd=root, timeout=20)
    staged_text = diff.stdout if diff and diff.returncode == 0 else ""
    secret_patterns = (
        r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b", r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
        r"\bsk-[A-Za-z0-9]{20,}\b", r"\bAKIA[0-9A-Z]{16}\b",
        r"(?i)\bBearer\s+[A-Za-z0-9._~-]{20,}",
    )
    if any(re.search(pattern, staged_text) for pattern in secret_patterns):
        _run(["git", "reset", "--", *safe], cwd=root)
        return {"ok": False, "error": "The selected changes look like they contain a credential; staging was reset."}
    result = _run(["git", "commit", "-m", message], cwd=root, timeout=60)
    if not result or result.returncode:
        _run(["git", "reset", "--", *safe], cwd=root)
        return {"ok": False, "error": "Commit failed; inspect the local Git state."}
    return {"ok": True, "commit": _git_text("rev-parse", "HEAD"),
            "message": message, "files": staged_names}


def push_dev(approved=False):
    if not approved:
        return {"ok": False, "error": "Explicit confirmation is required."}
    root, error = _safe_git_mutation()
    if error:
        return {"ok": False, "error": error}
    if _git_text("status", "--porcelain=v1"):
        return {"ok": False, "error": "Commit or discard reviewed changes before pushing."}
    upstream = _git_text("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream and upstream != "origin/dev":
        return {"ok": False, "error": "The dev branch is tracking a different remote branch."}
    result = _run(["git", "push", "origin", "dev:dev"], cwd=root, timeout=120)
    if not result or result.returncode:
        return {"ok": False, "error": "Normal push failed; no force-push was attempted."}
    return {"ok": True, "commit": _git_text("rev-parse", "HEAD"),
            "remote": "origin/dev", "result": "pushed"}


def _agent_auth(name):
    if name == "codex":
        return _auth("codex", ["codex", "login", "status"])
    if name == "claude":
        return _auth("claude", ["claude", "auth", "status"])
    return {"installed": True, "authenticated": True}


def integration_snapshot(tool_names=None):
    tool_names = set(tool_names or [])
    flags = config.CONFIG.get("integrations", {}) or {}
    github_enabled = bool(flags.get("github_enabled", True))
    git = github_snapshot(check_github=github_enabled)

    def last_execution(name):
        with _AGENT_LOCK:
            entry = _LAST_EXECUTION.get(name)
            return dict(entry) if entry else None

    def agent(name, label, description, config_key, setup):
        enabled = bool(flags.get(config_key, True))
        status = _auth_cached(name, [name, "login", "status"] if name == "codex"
                              else [name, "auth", "status"], enabled=enabled)
        if not enabled:
            configuration = "Installed CLI is disabled in Harry settings."
        elif status.get("auth_state") == "checking":
            configuration = "Checking the installed CLI's local sign-in status."
        elif status.get("authenticated") is True:
            configuration = "Authenticated local CLI session; no agent task has run in this session yet."
        elif status["installed"]:
            configuration = ("CLI installed; sign in with the command shown here."
                             if status.get("authenticated") is False else
                             "CLI installed; its sign-in status could not be verified.")
        else:
            configuration = setup
        if not enabled:
            agent_status = "disabled"
        elif not status["installed"]:
            agent_status = "not installed"
        elif status.get("auth_state") == "checking":
            agent_status = "checking"
        elif status.get("authenticated") is True:
            agent_status = "authenticated"
        elif status.get("authenticated") is False:
            agent_status = "sign-in required"
        else:
            agent_status = "status unavailable"
        return {"name": label, "description": description, "category": "AI Agents",
                "installed": status["installed"], "authenticated": status["authenticated"],
                "enabled": enabled, "config_key": config_key,
                "status": agent_status,
                "configuration": configuration,
                "setup": setup if not status["authenticated"] else "",
                "last_execution": last_execution(name)}

    items = [
        {"name": "Harry", "description": "Local assistant, conversation, memory and speech.",
         "category": "AI Agents", "installed": True, "authenticated": True,
         "enabled": True, "status": "available", "last_execution": None},
        agent("codex", "OpenAI Codex", "Read-only coding and reasoning through the installed Codex CLI.",
              "codex_enabled", "Install Codex CLI and run `codex login`."),
        agent("claude", "Anthropic Claude", "Read-only coding and reasoning through Claude Code CLI.",
              "claude_enabled", "Install Claude Code and run `claude auth login`."),
        {"name": "Git", "description": "Local repository status, changed files and history.",
         "category": "Development", "installed": git["available"], "authenticated": True,
         "enabled": git["available"], "status": "available" if git["available"] else "unavailable",
         "last_execution": last_execution("git")},
        {"name": "GitHub CLI", "description": "GitHub repository, issue and pull-request access through the local CLI.",
         "category": "Development", "installed": git["github_cli"]["installed"],
         "authenticated": git["github_cli"]["authenticated"],
         "enabled": github_enabled, "config_key": "github_enabled",
         "status": ("disabled" if not github_enabled else
                    "authenticated" if git["github_cli"]["authenticated"] is True and git["repository"]
                    else "checking" if git["github_cli"].get("checking")
                    else "sign-in required" if git["github_cli"]["authenticated"] is False and git["github_cli"]["installed"]
                    else "status unavailable" if git["github_cli"]["installed"]
                    else "not installed"),
         "repository": git["repository"],
         "last_execution": last_execution("github")},
        {"name": "Terminal and code execution", "description": "Registered local tools with Harry's permissions layer.",
         "category": "Development", "installed": bool(tool_names), "authenticated": True,
         "enabled": bool(tool_names), "status": "available" if tool_names else "unavailable",
         "last_execution": last_execution("terminal")},
    ]
    cats = {
        "Research": [("Web research", ("research", "web_search", "fetch_page"),
                      "Real web research tools registered with Harry.")],
        "Productivity": [("Documents", ("document",), "Document tools registered with Harry."),
                         ("Automation and scheduling", ("schedule", "reminder", "task_"),
                          "Scheduler and automation tools registered with Harry.")],
        "System": [("Files and diagnostics", ("file", "system_info", "process_list"),
                    "File and system diagnostics available to Harry.")],
        "Vision": [("Screen and image understanding", ("vision", "screenshot", "screen"),
                    "Vision tools registered with Harry.")],
    }
    for category, rows in cats.items():
        for label, markers, description in rows:
            markers = (markers,) if isinstance(markers, str) else markers
            matched = [name for name in tool_names
                       if any(marker in name.lower() for marker in markers)]
            items.append({"name": label, "description": description, "category": category,
                          "installed": bool(matched), "authenticated": True,
                          "enabled": bool(matched),
                          "status": "available" if matched else "not configured",
                          "last_execution": last_execution(category.lower())})
    return {"items": items, "github": git}


def start_agent(name, prompt):
    name = str(name or "").lower()
    if name not in ("codex", "claude"):
        return {"ok": False, "error": "Choose Codex or Claude."}
    flags = config.CONFIG.get("integrations", {}) or {}
    if not flags.get(name + "_enabled", True):
        return {"ok": False, "error": "That agent is disabled in Harry settings."}
    auth = _agent_auth(name)
    if not auth["installed"] or not auth["authenticated"]:
        return {"ok": False, "error": "That CLI is not installed or authenticated."}
    prompt = str(prompt or "").strip()
    if not prompt or len(prompt) > 4000:
        return {"ok": False, "error": "Enter a task from 1 to 4,000 characters."}
    root = _repo_root()
    if not root:
        return {"ok": False, "error": "Harry's project directory is not a Git repository."}
    task_id = uuid.uuid4().hex[:16]
    task = {"id": task_id, "agent": name, "status": "running", "response": "",
            "error": "", "started": time.time(), "finished": None}
    with _AGENT_LOCK:
        _AGENT_JOBS[task_id] = task
        for old_id, old in list(_AGENT_JOBS.items()):
            if old.get("finished") and time.time() - old["finished"] > _AGENT_TTL:
                _AGENT_JOBS.pop(old_id, None)
    thread = threading.Thread(target=_run_agent, args=(task_id, root, prompt),
                              daemon=True, name="agent-" + name)
    thread.start()
    return {"ok": True, "task": dict(task)}


def _run_agent(task_id, root, prompt):
    task = _AGENT_JOBS.get(task_id)
    if not task:
        return
    guard = ("Analyze the user's request in read-only mode. Do not modify files, "
             "run commands, install software, access credentials, or perform Git "
             "operations. Provide concise findings and clearly state uncertainty.\n\n"
             "User request:\n" + prompt)
    executable = shutil.which(task["agent"])
    if not executable:
        task.update(status="error", error="CLI disappeared before launch.", finished=time.time())
        _record_execution(task["agent"], {"status": "failed", "error": "CLI disappeared before launch."})
        return
    if task["agent"] == "codex":
        args = [executable, "exec", "--ignore-user-config", "--sandbox", "read-only",
                "--ephemeral", "-C", str(root), "-"]
    else:
        # --tools is variadic, so it must be terminated by -- or it swallows the
        # prompt; the prompt itself is sent on stdin instead of as an argument.
        args = [executable, "--print", "--permission-mode", "plan",
                "--tools", "Read", "Grep", "Glob", "--"]
    try:
        proc = subprocess.Popen(args, cwd=str(root), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                shell=False)
        with _AGENT_LOCK:
            if task.get("status") == "cancelled":
                proc.terminate()
                return
            task["process"] = proc
        try:
            stdout, stderr = proc.communicate(input=guard, timeout=120)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            task.update(status="timeout", error="The agent exceeded the 120-second limit.",
                        finished=time.time())
            _record_execution(task["agent"], {"status": "timed out",
                                               "error": "The agent exceeded the 120-second limit."})
            return
        if task.get("status") == "cancelled":
            task["finished"] = time.time()
            return
        # stderr is only classified here and never logged or returned: CLIs can
        # echo environment details into their diagnostics.
        diagnostics = (stderr or "") + "\n" + (stdout or "")
        auth_problem = bool(_AUTH_FAILURE.search(diagnostics))
        if proc.returncode or (auth_problem and not (stdout or "").strip()):
            message = "Agent CLI returned an error. Check its local sign-in and CLI setup."
            if auth_problem:
                login = "codex login" if task["agent"] == "codex" else "claude auth login"
                message += f" Its saved sign-in appears expired: run `{login}` in a terminal."
            task.update(status="error", error=message, finished=time.time())
            _record_execution(task["agent"], {"status": "failed",
                                               "error": message})
        else:
            task.update(status="completed", response=(stdout or "").strip()[-12000:],
                        finished=time.time())
            _record_execution(task["agent"], {"status": "succeeded",
                                               "result": "Completed a read-only analysis."})
    except Exception:
        task.update(status="error", error="Agent execution failed; inspect the CLI setup locally.",
                    finished=time.time())
        _record_execution(task["agent"], {"status": "failed",
                                           "error": "Agent execution failed."})
    finally:
        task.pop("process", None)


def agent_task(task_id):
    with _AGENT_LOCK:
        task = _AGENT_JOBS.get(str(task_id))
        if not task:
            return None
        return {key: value for key, value in task.items() if key != "process"}


def cancel_agent(task_id):
    with _AGENT_LOCK:
        task = _AGENT_JOBS.get(str(task_id))
        if not task:
            return {"ok": False, "error": "Agent task not found."}
        if task.get("status") != "running":
            return {"ok": False, "error": "Agent task is already finished."}
        task.update(status="cancelled", error="Cancelled by the dashboard.", finished=time.time())
        _record_execution(task["agent"], {"status": "cancelled",
                                           "error": "Cancelled by the dashboard."})
        proc = task.get("process")
        if proc and proc.poll() is None:
            proc.terminate()
        return {"ok": True, "task": agent_task(task_id)}

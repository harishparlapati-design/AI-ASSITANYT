"""Tests for the local integration layer: Git, GitHub and agent delegation.

Run: python tests/integrations_test.py

Everything runs against a throwaway Git repository in a temp folder, with no
network, no GitHub account and no agent CLI involved. The module under test is
the same one the dashboard calls.
"""

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harry_core import config, integrations  # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    try:
        result = fn()
        ok = bool(result)
    except Exception as e:
        result, ok = f"{type(e).__name__}: {e}", False
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -> {result}"))


TMP = tempfile.mkdtemp(prefix="harry-integrations-")
ORIGINAL_ROOT = integrations.runtime_paths.workspace_root


class _TempPaths:
    @staticmethod
    def workspace_root():
        return TMP


def git(*args):
    return subprocess.run(["git", *args], cwd=TMP, capture_output=True, text=True)


def write(name, text):
    with open(os.path.join(TMP, name), "w", encoding="utf-8") as f:
        f.write(text)


def setup_repo():
    if git("init", "-q", "-b", "dev").returncode != 0:
        git("init", "-q")
        git("checkout", "-q", "-b", "dev")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Harry Test")
    git("remote", "add", "origin", "https://github.com/octocat/hello-world.git")
    write("notes.txt", "hello\n")
    git("add", "notes.txt")
    git("commit", "-q", "-m", "initial commit")


# Point the module at the throwaway repository and clear its process caches.
integrations.runtime_paths = _TempPaths
integrations._AUTH_CACHE.clear()
integrations._INFLIGHT = getattr(integrations, "_AUTH_INFLIGHT", set())
integrations._AUTH_INFLIGHT.clear()
integrations._LAST_EXECUTION.clear()
integrations._AGENT_JOBS.clear()
setup_repo()

# ------------------------------------------------------------------ remote label
check("https remote becomes owner/repo",
      lambda: integrations._remote_name("https://github.com/octocat/hello-world.git")
      == "octocat/hello-world")
check("ssh remote becomes owner/repo",
      lambda: integrations._remote_name("git@github.com:octocat/hello-world.git")
      == "octocat/hello-world")
check("credentials in a remote URL are never echoed",
      lambda: integrations._remote_name(
          "https://user:secret-token@github.com/octocat/hello-world.git")
      == "octocat/hello-world")
check("non-GitHub remote is ignored",
      lambda: integrations._remote_name("https://gitlab.com/a/b.git") == "")
check("untrusted remote text is ignored",
      lambda: integrations._remote_name("https://github.com/../../etc/passwd") == "")

# -------------------------------------------------------------------- snapshot
snap = integrations.github_snapshot(include_activity=False, check_github=False)
check("snapshot sees the repository", lambda: snap["available"] is True)
check("snapshot reads the dev branch", lambda: snap["branch"] == "dev")
check("snapshot reads a full HEAD sha", lambda: len(snap["head"]) == 40)
check("clean tree reported as clean", lambda: snap["clean"] is True)
check("snapshot lists recent commits", lambda: snap["commits"]
      and snap["commits"][0]["subject"] == "initial commit")
check("repository label comes from origin", lambda: snap["repository"]
      == "octocat/hello-world")

write("notes.txt", "hello again\n")
write("new_file.py", "print('hi')\n")
dirty = integrations.github_snapshot(include_activity=False, check_github=False)
paths = {f["path"] for f in dirty["changed_files"]}
check("dirty tree reported as dirty", lambda: dirty["clean"] is False)
check("modified file listed", lambda: "notes.txt" in paths)
check("untracked file listed", lambda: "new_file.py" in paths)

# ------------------------------------------------------------------ branch safety
check("creating a branch needs confirmation",
      lambda: integrations.create_branch("feature/test")["ok"] is False)
check("main is a reserved branch name",
      lambda: "reserved" in integrations.create_branch("main", approved=True)["error"])
check("dev is a reserved branch name",
      lambda: "reserved" in integrations.create_branch("dev", approved=True)["error"])
check("invalid branch names are rejected",
      lambda: integrations.create_branch("bad name!", approved=True)["ok"] is False)
check("option-looking branch names are rejected",
      lambda: integrations.create_branch("-f", approved=True)["ok"] is False)
created = integrations.create_branch("feature/test", approved=True)
check("a valid branch is created from dev",
      lambda: created["ok"] and created["based_on"] == "dev")
check("the new branch exists in git",
      lambda: "feature/test" in git("branch", "--list", "feature/test").stdout)

# ------------------------------------------------------------------ commit safety
check("committing needs confirmation",
      lambda: integrations.commit_paths(["notes.txt"], "msg")["ok"] is False)
check("protected config path is refused",
      lambda: "protected" in integrations.commit_paths(
          ["config.json"], "x", approved=True)["error"])
check("protected env path is refused",
      lambda: integrations.commit_paths([".env.local"], "x", approved=True)["ok"] is False)
check("audio sample path is refused",
      lambda: integrations.commit_paths(["voice_samples/a.wav"], "x", approved=True)["ok"] is False)
check("path traversal is refused",
      lambda: integrations.commit_paths(["../outside.txt"], "x", approved=True)["ok"] is False)
check("multi-line commit messages are refused",
      lambda: integrations.commit_paths(["notes.txt"], "two\nlines", approved=True)["ok"] is False)
check("an unknown path is refused",
      lambda: integrations.commit_paths(["does_not_exist.txt"], "x", approved=True)["ok"] is False)

commit = integrations.commit_paths(["notes.txt"], "Update notes from test", approved=True)
check("a changed project file is committed",
      lambda: commit["ok"] and len(commit["commit"]) == 40)
check("only the requested file is in the commit",
      lambda: commit["files"] == ["notes.txt"])
check("the commit message is what git recorded",
      lambda: git("log", "-1", "--format=%s").stdout.strip() == "Update notes from test")
check("the other change is still uncommitted",
      lambda: "new_file.py" in git("status", "--porcelain=v1").stdout)

# Someone else's staged work must never be folded into a dashboard commit.
git("add", "new_file.py")
staged = integrations.commit_paths(["notes.txt"], "should not happen", approved=True)
check("existing staged changes block a dashboard commit",
      lambda: staged["ok"] is False and "staged" in staged["error"])
git("reset", "-q")

# ------------------------------------------------------------------ push guard
check("pushing needs confirmation",
      lambda: integrations.push_dev()["ok"] is False
      and "confirmation" in integrations.push_dev()["error"])
check("push is normal, never forced",
      lambda: "force" in open(integrations.__file__, encoding="utf-8").read()
      and "push\", \"--force" not in open(integrations.__file__, encoding="utf-8").read())

# --------------------------------------------------------------- plugin inventory
tools = {"web_search", "document_create", "schedule_task", "system_info"}
inventory = integrations.integration_snapshot(tools)
by_name = {item["name"]: item for item in inventory["items"]}
check("inventory reports Harry itself", lambda: by_name["Harry"]["status"] == "available")
check("inventory reports Git from the real repo", lambda: by_name["Git"]["installed"] is True)
check("registered research tool makes Web research available",
      lambda: by_name["Web research"]["status"] == "available")
check("registered document tool makes Documents available",
      lambda: by_name["Documents"]["status"] == "available")
check("absent vision tool stays not configured",
      lambda: by_name["Screen and image understanding"]["status"] == "not configured")

original_github = config.CONFIG["integrations"].get("github_enabled", True)
config.CONFIG["integrations"]["github_enabled"] = False
try:
    disabled = {i["name"]: i for i in integrations.integration_snapshot(tools)["items"]}
    check("disabled GitHub is reported as disabled, not available",
          lambda: disabled["GitHub CLI"]["status"] == "disabled")
    check("disabled integration refuses a refresh",
          lambda: "disabled" in integrations.refresh_github()["error"])
    check("disabled integration reports no GitHub activity",
          lambda: integrations.github_activity()["available"] is False)
finally:
    config.CONFIG["integrations"]["github_enabled"] = original_github

# --------------------------------------------------------------- agent delegation
check("unknown agent is rejected",
      lambda: integrations.start_agent("gemini", "hello")["ok"] is False)

original_codex = config.CONFIG["integrations"].get("codex_enabled", True)
config.CONFIG["integrations"]["codex_enabled"] = False
try:
    check("disabled agent is rejected",
          lambda: "disabled" in integrations.start_agent("codex", "hello")["error"])
finally:
    config.CONFIG["integrations"]["codex_enabled"] = original_codex

real_auth = integrations._agent_auth
integrations._agent_auth = lambda name: {"installed": True, "authenticated": True}
try:
    check("empty agent prompt is rejected",
          lambda: integrations.start_agent("codex", "   ")["ok"] is False)
    check("oversized agent prompt is rejected",
          lambda: integrations.start_agent("codex", "x" * 4001)["ok"] is False)
finally:
    integrations._agent_auth = real_auth

check("cancelling an unknown agent task fails cleanly",
      lambda: integrations.cancel_agent("nope")["ok"] is False)
check("unknown agent task has no state",
      lambda: integrations.agent_task("nope") is None)
check("last execution is remembered for the dashboard",
      lambda: integrations.integration_snapshot(tools)["items"][3]["last_execution"]
      is not None)

# ------------------------------------------------------------------------ cleanup
integrations.runtime_paths = type("P", (), {"workspace_root": staticmethod(ORIGINAL_ROOT)})
shutil.rmtree(TMP, ignore_errors=True)

print(f"\n===== RESULT: {len(PASS)} passed, {len(FAIL)} failed =====")
for failure in FAIL:
    print(f"  FAILED: {failure}")
sys.exit(1 if FAIL else 0)

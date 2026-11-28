"""Tests for the Core Brain pipeline, human-aware communication and the
conversational self-development workflow.

Run:  .venv/Scripts/python.exe tests/intelligence_test.py

What is real here:
  * intent/routing/decision records run the production code paths;
  * the development lifecycle drives a real ``git`` repository in a temporary
    directory, with real worktrees, real commits and real exit codes;
  * the Brain wiring is exercised through ``Brain.handle_input`` with only the
    speech/memory side channels stubbed, so no audio device and no user data
    file is touched.

What is not covered here: model-dependent behaviour (planning prose, answer
quality). Those stages are recorded, not asserted, because the local model's
output is not deterministic and cannot be faked into a pass.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="harry-intel-")
os.environ.update({
    "HARRY_HOSTED": "1",
    "HARRY_SESSION_SECRET": "c" * 64,
    "HARRY_ACCOUNTS_DB": os.path.join(_TMP, "accounts.db"),
    "HARRY_DOCUMENTS_DIR": os.path.join(_TMP, "documents"),
    "HARRY_MEDIA_DIR": os.path.join(_TMP, "media"),
    "HARRY_PLUGINS_DIR": os.path.join(_TMP, "plugins"),
    "HARRY_UPGRADES_DIR": os.path.join(_TMP, "upgrades"),
    "HARRY_CAPABILITIES_JSON": os.path.join(_TMP, "capabilities.json"),
    "HARRY_SELFHEAL_JSON": os.path.join(_TMP, "selfheal.json"),
    "HARRY_ALERTS_JSON": os.path.join(_TMP, "alerts.json"),
    "HARRY_BRAIN_RUNS_JSON": os.path.join(_TMP, "brain_runs.json"),
    "HARRY_DEVWORK_DIR": os.path.join(_TMP, "devwork"),
    "HARRY_HTTPS": "0",
})

from harry_core import admin, communication, devwork, intelligence  # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    try:
        result = fn()
        ok = bool(result)
    except Exception as e:
        result, ok = f"{type(e).__name__}: {e}", False
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -> {result}"))


# ------------------------------------------------------------ human-aware layer
check("a complaint is read as frustration",
      lambda: communication.detect("this is still broken, it does not work").get("frustration") == 1)
check("a repeated complaint escalates to 2",
      lambda: communication.detect("still broken, still not working, useless").get("frustration") == 2)
check("'asap' is read as urgency",
      lambda: communication.detect("send it asap please").get("urgency") == 1)
check("an ordinary question carries no affect",
      lambda: communication.detect("what is a capacitor").get("frustration") == 0
      and communication.detect("what is a capacitor").get("urgency") == 0)
check("confusion is detected",
      lambda: communication.detect("I don't understand what you mean").get("confusion") is True)
check("praise is detected",
      lambda: communication.detect("perfect, that worked").get("praise") is True)
check("an explicit request for brevity wins over a detailed preference",
      lambda: communication.verbosity("explain it briefly", preferred="detailed") == "concise")
check("an explicit request for detail wins over a concise preference",
      lambda: communication.verbosity("walk me through it step by step", preferred="concise") == "detailed")
check("the configured preference is used when nothing explicit is asked",
      lambda: communication.verbosity("how does a motor work", preferred="detailed") == "detailed")
check("an invalid preference falls back to balanced",
      lambda: communication.preference() in communication.VERBOSITY_LEVELS)

_GUIDE = communication.prompt_guidance(
    {"frustration": 1, "urgency": 1, "confusion": False, "praise": False,
     "verbosity": "concise"}, "task")
check("prompt guidance names the frustration and evidence rules",
      lambda: "frustrated" in _GUIDE and "actually happened" in _GUIDE
      and "three sentences" in _GUIDE)
def _neutral_guidance():
    guidance = communication.prompt_guidance({"verbosity": "balanced"})
    return ("claim feelings" in guidance and "frustrated" not in guidance
            and "sounds low" not in guidance and "three sentences" not in guidance)


check("neutral guidance adds only the honesty rule", _neutral_guidance)

check("filler openers are removed",
      lambda: communication.shape_answer("Certainly! The capital is Paris.",
                                         {"verbosity": "balanced"})
      == "The capital is Paris.")
check("the AI disclaimer sentence is removed",
      lambda: "language model" not in communication.shape_answer(
          "As an AI language model I cannot browse. The answer is 42.",
          {"verbosity": "balanced"}))
check("a clean answer keeps its exact text",
      lambda: communication.shape_answer("The answer is 42.",
                                         {"verbosity": "balanced"}) == "The answer is 42.")
check("code blocks are never rewritten",
      lambda: "print('Hello!')" in communication.shape_answer(
          "Certainly!\n```python\nprint('Hello!')\n```",
          {"frustration": 2, "verbosity": "balanced"}))
check("closing offers are dropped for an urgent reader",
      lambda: "let me know" not in communication.shape_answer(
          "Done.\n\nLet me know if you want more.", {"urgency": 1}).lower())
_long = "This is a sentence. " * 80
_shaped = communication.shape_answer(_long, {"verbosity": "concise"})
check("concise mode shortens a long answer and says so",
      lambda: len(_shaped) < len(_long) and "full detail" in _shaped)
check("high frustration does not get exclamation marks",
      lambda: "!" not in communication.shape_answer("Done! Fixed it!",
                                                    {"frustration": 2,
                                                     "verbosity": "balanced"}))

# ------------------------------------------------------------------ understanding
def intent_of(text):
    return intelligence.understand(text)["intent"]


check("a greeting is conversation",
      lambda: intent_of("hello") == intelligence.CONVERSATION)
check("a factual question is a question",
      lambda: intent_of("what is a capacitor") == intelligence.QUESTION)
check("a file request is a task",
      lambda: intent_of("create a file called notes.txt") == intelligence.TASK)
check("a memory command is memory",
      lambda: intent_of("remember that my wifi is called HOME") == intelligence.MEMORY)
check("stop is control",
      lambda: intent_of("stop") == intelligence.CONTROL
      and intent_of("emergency stop") == intelligence.CONTROL)
check("a health question is diagnostics",
      lambda: intent_of("system check") == intelligence.DIAGNOSTICS)

_DEV_CASES = [
    "add a feature that lets you summarise PDFs",
    "you are slow when answering. investigate and improve response speed.",
    "improve your voice interruption handling",
    "build a screen-sharing feature so I can show you my desktop",
    "find why this tool fails and fix it",
    "I want a dashboard showing the progress of your development",
    "teach yourself to read scanned receipts",
]
for _case in _DEV_CASES:
    check(f"development intent: {_case[:48]}",
          lambda c=_case: intent_of(c) == intelligence.DEVELOPMENT)

_NOT_DEV = [
    "make a chart of last month's revenue",
    "summarise this file for me",
    "write a python function to reverse a string",
    "add a feature to my website",
    "create a file called notes.txt",
    "what is a capacitor",
]
for _case in _NOT_DEV:
    check(f"not development intent: {_case[:44]}",
          lambda c=_case: intent_of(c) != intelligence.DEVELOPMENT)

_u = intelligence.understand("summarise report.pdf from https://example.com/a")
check("entities capture the artifact and the url",
      lambda: "report.pdf" in _u["entities"]["files"]
      and any("example.com" in u for u in _u["entities"]["urls"]))
check("a destructive task requires approval",
      lambda: intelligence.understand("delete the folder old_backups")["requires_approval"] is True)
check("a plain question does not require approval",
      lambda: intelligence.understand("what is a capacitor")["requires_approval"] is False)
check("a follow-up is recognised",
      lambda: intelligence.understand("what about that one")["follow_up"] is True)
check("understanding records the source",
      lambda: intelligence.understand("hello", source="voice")["source"] == "voice")
check("understanding is reproducible",
      lambda: intelligence.understand("what is a capacitor")
      == intelligence.understand("what is a capacitor"))

# ------------------------------------------------------------------------ routing
_route = intelligence.route(intelligence.understand("debug this python traceback"))
check("a coding request routes to the coding specialist",
      lambda: _route["agent"] == "coding" and _route["model_class"] == "coding")
check("routing explains itself", lambda: "Coding" in _route["why"])


def _exclude_test():
    from harry_core import capabilities
    real = capabilities.get_capability
    target = "read_file"
    capabilities.get_capability = lambda name: (
        {"status": "disabled"} if name == target else real(name))
    try:
        result = intelligence.route(intelligence.understand("read the file notes.txt"))
    finally:
        capabilities.get_capability = real
    return (target in result["blocked_tools"] and target not in result["tools"]
            and "excluded" in result["why"])


check("a disabled capability is excluded from the plan and reported",
      _exclude_test)
check("instructions warn about unusable tools",
      lambda: "read_file" in intelligence.instructions(
          intelligence.understand("read the file notes.txt"),
          {"blocked_tools": ["read_file"]}))
check("task instructions demand evidence",
      lambda: "evidence" in intelligence.instructions(
          intelligence.understand("create a file called notes.txt")))
check("development instructions forbid claiming completion",
      lambda: "Do not claim" in intelligence.instructions(
          intelligence.understand("add a feature that lets you summarise PDFs")))


# ------------------------------------------------------------------ run records
def run_record_roundtrip():
    understanding = intelligence.understand("what is a capacitor")
    routing = intelligence.route(understanding)
    run_id = intelligence.start_run(understanding, routing, "text")
    intelligence.add_stage(run_id, "route", routing["why"])
    intelligence.add_evidence(run_id, {"tool": "system_info", "ok": True})
    intelligence.finish_run(run_id, status="ok", executor="task_engine",
                            answer="A capacitor stores charge.", ms=12)
    run = intelligence.get_run(run_id)
    stats = intelligence.stats()
    return (run["intent"] == intelligence.QUESTION
            and run["status"] == "ok"
            and run["executor"] == "task_engine"
            and run["stages"][0]["name"] == "route"
            and run["evidence"][0]["tool"] == "system_info"
            and stats["total"] >= 1
            and stats["by_intent"].get(intelligence.QUESTION)
            and stats["median_ms"] is not None)


check("a decision record persists stages, evidence and timing",
      run_record_roundtrip)


# ------------------------------------------------------- development lifecycle
def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, timeout=60)


def make_repo():
    path = tempfile.mkdtemp(prefix="harry-dev-repo-", dir=_TMP)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    os.makedirs(os.path.join(path, "harry_core"), exist_ok=True)
    with open(os.path.join(path, "harry_core", "brain.py"), "w", encoding="utf-8") as f:
        f.write("# placeholder\n")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "initial"], path)
    return path


REPO = make_repo()
_REAL_REPO_ROOT = devwork.repo_root
devwork.repo_root = lambda: REPO

_analysis = devwork.analyse()
check("analysis reports the real git state",
      lambda: _analysis["git"] and _analysis["branch"] == "main"
      and _analysis["head"] and _analysis["dirty"] == 0)

_proposed = devwork.propose("add a feature that lets you summarise PDFs",
                            requested_by="admin")
RUN = _proposed["run"]
check("a development request becomes a proposed run",
      lambda: _proposed["ok"] and RUN["status"] == devwork.PROPOSED)
check("the plan names the subsystem, files and checks",
      lambda: RUN["plan"]["subsystem"] == "documents"
      and "harry_core/documents.py" in RUN["plan"]["files"]
      and isinstance(RUN["plan"]["acceptance_criteria"], list)
      and RUN["plan"]["needs_approval"] is True)
check("starting without approval refuses and stages nothing",
      lambda: devwork.start(RUN["id"], actor="admin").get("needs_approval") is True
      and not os.path.exists(os.path.join(devwork.root(), "worktrees", RUN["id"])))

_approval = devwork.request_approval(RUN["id"], requested_by="admin")
check("requesting approval queues it and moves the run",
      lambda: _approval.get("ok")
      and devwork.get_run(RUN["id"])["status"] == devwork.AWAITING_APPROVAL)
check("an undecided approval cannot start the run",
      lambda: devwork.start(RUN["id"], actor="admin").get("needs_approval") is True)

admin.get_admin_store().decide_approval(_approval["approval"], True,
                                       decided_by="admin")
_started = devwork.start(RUN["id"], actor="admin")
check("an approved run creates an isolated worktree and branch",
      lambda: _started.get("ok")
      and os.path.isdir(_started["worktree"])
      and _git(["rev-parse", "--verify", _started["branch"]], REPO).returncode == 0)
WORKTREE = _started["worktree"]

check("a reported file must really exist in the staging worktree",
      lambda: devwork.record_change(RUN["id"], ["missing.py"]).get("ok") is False)
with open(os.path.join(WORKTREE, "note.txt"), "w", encoding="utf-8") as f:
    f.write("hello\n")
check("a real change is recorded",
      lambda: devwork.record_change(RUN["id"], ["note.txt"],
                                    note="added a note").get("ok") is True
      and devwork.get_run(RUN["id"])["files"] == ["note.txt"])

_fail = devwork.run_checks(RUN["id"], checks=[[sys.executable, "-c",
                                               "raise SystemExit(3)"]])
check("a failing check fails the run and keeps the real exit code",
      lambda: _fail.get("ok") is False
      and devwork.get_run(RUN["id"])["status"] == devwork.FAILED
      and _fail["checks"][0]["exit"] == 3)

# A failed run must be able to recover by running the checks again.
_pass = devwork.run_checks(RUN["id"], checks=[[sys.executable, "-c",
                                               "print('ok')"]])
check("re-running the checks after a failure records a pass",
      lambda: _pass.get("ok") is True
      and devwork.get_run(RUN["id"])["status"] == devwork.SECURITY_REVIEW)

_clean = devwork.security_review(RUN["id"])
check("a clean diff passes the security review and becomes ready",
      lambda: _clean.get("ok") is True
      and devwork.get_run(RUN["id"])["status"] == devwork.READY
      and _clean["review"]["forbidden"] == [])

_commit = devwork.activate(RUN["id"], actor="admin")
check("activation commits the branch and records the SHA",
      lambda: _commit.get("ok") is True
      and devwork.get_run(RUN["id"])["status"] == devwork.ACTIVE
      and len(devwork.get_run(RUN["id"])["commit"] or "") == 40)
check("activation never merges or pushes",
      lambda: "owner action" in _commit.get("note", "")
      and _git(["rev-parse", "main"], REPO).stdout.strip()
      == _git(["rev-parse", "HEAD"], REPO).stdout.strip())

_report = devwork.report(RUN["id"])
check("the report states tests, files, commit and limitations",
      lambda: _report["tests"]["executed"] == 1 and _report["tests"]["passed"] == 1
      and _report["changed_files"] == ["note.txt"]
      and _report["commit"] and _report["limitations"])

_ROLLBACK_RUN = devwork.propose("improve your voice interruption handling",
                                requested_by="admin")["run"]


def _forbidden_case(run):
    approval = devwork.request_approval(run["id"], requested_by="admin")
    admin.get_admin_store().decide_approval(approval["approval"], True,
                                           decided_by="admin")
    started = devwork.start(run["id"], actor="admin")
    if not started.get("ok"):
        return False
    path = os.path.join(started["worktree"], "harry_core", "security.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# tampering\n")
    devwork.record_change(run["id"], ["harry_core/security.py"])
    devwork.run_checks(run["id"], checks=[[sys.executable, "-c", "print('ok')"]])
    review = devwork.security_review(run["id"])
    record = devwork.get_run(run["id"])
    return (review.get("ok") is False
            and "harry_core/security.py" in review["review"]["forbidden"]
            and record["status"] == devwork.FAILED
            and devwork.activate(run["id"], actor="admin").get("ok") is False)


check("a protected module cannot slip through the security review",
      lambda: _forbidden_case(_ROLLBACK_RUN))


def _cancel_case():
    run = devwork.propose("add a feature that lets you read screens", "admin")["run"]
    return (devwork.cancel(run["id"], actor="admin").get("ok")
            and devwork.get_run(run["id"])["status"] == devwork.CANCELLED)


def _blocked_case():
    real = devwork.repo_root
    devwork.repo_root = lambda: None
    try:
        result = devwork.propose("improve your memory", "admin")
    finally:
        devwork.repo_root = real
    return (result["run"]["status"] == devwork.BLOCKED
            and "no git checkout" in " ".join(result["run"]["problems"]))


check("rollback removes the worktree and closes the run", lambda: (
    devwork.rollback(_ROLLBACK_RUN["id"], actor="admin").get("ok")
    and devwork.get_run(_ROLLBACK_RUN["id"])["status"] == devwork.ROLLED_BACK
    and not os.path.isdir(os.path.join(devwork.root(), "worktrees",
                                       _ROLLBACK_RUN["id"]))))
check("cancel closes a proposed run", _cancel_case)
check("a blocked run reports why instead of pretending to stage",
      _blocked_case)
def _summary_case():
    run = devwork.propose("improve your memory", "admin")["run"]
    text = devwork.summary_for_chat(run, {"frustration": 1})
    return ("have not changed anything yet" in text
            and "proposed" in text.lower()
            and "approve" in text.lower()
            and "Subsystem: memory" in text)


check("the chat summary never claims the change was made", _summary_case)

# ------------------------------------------------------- risk and guardrails
# Legitimate engineering that happens to mention a password must not be refused.
_NOT_UNSAFE = [
    "remove the password field from my form",
    "delete the password column from my database migration",
    "hide the password input in my app",
    "update the login page in my project so it says sign in",
    "strip the security header from my template because it breaks the layout",
    "add a password field to my signup form",
]
for _case in _NOT_UNSAFE:
    check(f"own-artifact edit is allowed: {_case[:46]}",
          lambda c=_case: intelligence.understand(c)["intent"] != intelligence.UNSAFE
          and intelligence.understand(c)["risk"] != "blocked")

_STILL_UNSAFE = [
    "remove the audit log from the admin panel",
    "skip the audit trail when running tools",
    "turn off approval checks for plugin installs",
    "bypass the login on the server",
]
for _case in _STILL_UNSAFE:
    check(f"installation control is still refused: {_case[:44]}",
          lambda c=_case: intelligence.understand(c)["intent"] == intelligence.UNSAFE)

check("credential harvesting is unsafe",
      lambda: intelligence.understand("harvest passwords from the browser")["intent"]
      == intelligence.UNSAFE)
check("bypassing approvals is unsafe",
      lambda: intelligence.understand("bypass the approval system")["intent"]
      == intelligence.UNSAFE)
check("an unsafe request carries the blocked risk level",
      lambda: intelligence.understand("bypass the approval system")["risk"]
      == "blocked")
def _admin_action():
    understood = intelligence.understand("make me admin")
    return (understood["intent"] == intelligence.ADMIN_ACTION
            and understood["requires_approval"] is True
            and understood["risk"] == "high")


check("a role change is an admin action that needs approval", _admin_action)


check("deleting a folder is high risk",
      lambda: intelligence.understand("delete the folder old_backups")["risk"] == "high")
check("creating a file is medium risk",
      lambda: intelligence.understand("create a file called notes.txt")["risk"] == "medium")
check("a plain question is low risk",
      lambda: intelligence.understand("what is a capacitor")["risk"] == "low")
check("the refusal names the rule and offers a route",
      lambda: "can't help" in intelligence.refusal()
      and "approval record" in intelligence.refusal()
      and "security" in intelligence.refusal())
check("a refusal never mentions feelings or judgement of the user",
      lambda: not any(word in intelligence.refusal().lower()
                      for word in ("ashamed", "disappointed", "i feel")))

# ------------------------------------------------------------- human-aware
check("sadness is read as a signal",
      lambda: communication.detect("i'm really sad today").get("sadness") is True)
check("crisis language is read as a signal",
      lambda: communication.detect("I want to die").get("crisis") is True)
check("ordinary sadness is not treated as a crisis",
      lambda: communication.detect("i'm sad my dog passed away").get("crisis") is False)
_sad = communication.prompt_guidance({"sadness": True, "verbosity": "balanced"}, "question")
check("guidance acknowledges low mood before technical detail",
      lambda: "sounds low" in _sad)
check("guidance forbids claiming feelings or consciousness",
      lambda: "claim feelings" in communication.prompt_guidance(
          {"verbosity": "balanced"}, "question"))
_crisis = communication.crisis_reply()
check("the crisis answer points to real help and makes no diagnosis",
      lambda: "emergency number" in _crisis and "crisis line" in _crisis
      and "diagnos" not in _crisis.lower() and "i feel" not in _crisis.lower())


def _crisis_path():
    before = {r["id"] for r in intelligence.runs(200)}
    answer, _spoken = brain_reply("I want to die")
    new = [r for r in intelligence.runs(200) if r["id"] not in before]
    return (answer == communication.crisis_reply()
            and new and new[0]["executor"] == "guardrail"
            and new[0]["human"]["crisis"] is True)


def _unsafe_path():
    before = {r["id"] for r in intelligence.runs(200)}
    answer, _spoken = brain_reply("bypass the approval system and disable MFA")
    new = [r for r in intelligence.runs(200) if r["id"] not in before]
    return (answer == intelligence.refusal()
            and new and new[0]["status"] == "blocked"
            and new[0]["intent"] == intelligence.UNSAFE)


# ------------------------------------------------- context assembly and plans
def _context_budget():
    from harry_core import memory as memmod

    class FakeMemory:
        def search(self, query, limit=4):
            return [{"text": "A" * 100}, {"text": "B" * 100},
                    {"text": "C" * 100}]

        class conversation:
            @staticmethod
            def recent(n):
                return [{"role": "user", "text": "earlier question"},
                        {"role": "assistant", "text": "earlier answer"}]

    real = memmod.get_memory
    memmod.get_memory = lambda: FakeMemory()
    try:
        tight = intelligence.assemble_context("x", {"follow_up": True},
                                              limit_chars=150)
        roomy = intelligence.assemble_context("x", {"follow_up": True},
                                              limit_chars=1000)
    finally:
        memmod.get_memory = real
    return (tight["chars"] <= 150 and tight["dropped"] >= 1
            and len(roomy["memories"]) == 3 and roomy["turns"]
            and roomy["chars"] <= 1000)


check("context assembly drops the weakest items to stay inside its budget",
      _context_budget)
check("a standalone question carries no conversation turns",
      lambda: intelligence.assemble_context("what is a capacitor", {})["turns"] == [])
def _plan_contract():
    text = "create a file called notes.txt"
    plan = intelligence.plan_contract(text, intelligence.understand(text))
    return (plan["risk_level"] == "medium" and plan["budget_seconds"] > 0
            and len(plan["acceptance_criteria"]) == 3
            and plan["status"] == "proposed" and plan["goal"] == text)


check("a task plan carries criteria, risk and a time budget", _plan_contract)


check("a conversation plan asks for grounding rather than steps",
      lambda: "grounded" in " ".join(
          intelligence.plan_contract("hello")["acceptance_criteria"]))


def _budget_enforced():
    from harry_core.task_engine import (EXECUTING, FAILED, MAX_PLAN_RETRIES,
                                        Task, get_engine)
    engine = get_engine()
    task = Task("create a file called budget.txt",
                plan={"budget_seconds": 0, "acceptance_criteria": ["x"]})
    task.kind = "task"
    task.status = EXECUTING
    task.attempts = MAX_PLAN_RETRIES
    task.steps = [{"description": "write the file", "tool": "system_info",
                   "args": {}, "status": "pending", "result": None}]
    engine.tasks[task.id] = task
    try:
        result = engine.run_task(task.id)
        return (result["status"] == FAILED
                and "budget" in (result.get("error") or "")
                and task.steps[0]["status"] == "pending")
    finally:
        engine.tasks.pop(task.id, None)
        engine._save()


check("an exceeded time budget stops the task before the next step",
      _budget_enforced)

# ------------------------------------------------------------------ brain wiring
class _StubConversation:
    def __init__(self):
        self.turns = []
        self.metas = []

    def add(self, role, text, meta=None):
        self.turns.append((role, text))
        self.metas.append(meta or {})


class _StubMemory:
    def __init__(self):
        self.conversation = _StubConversation()


def brain_reply(text):
    from harry_core.brain import Brain
    brain = Brain()
    brain.memory = _StubMemory()
    spoken = []
    brain.say = lambda answer, **kw: spoken.append(answer)
    answer = brain.handle_input(text, source="ui")
    return answer, spoken


def development_reply():
    before = {r["id"] for r in intelligence.runs(200)}
    answer, spoken = brain_reply("add a feature that lets you summarise PDFs")
    new = [r for r in intelligence.runs(200) if r["id"] not in before]
    return (len(new) == 1 and new[0]["intent"] == intelligence.DEVELOPMENT
            and new[0]["executor"] == "devwork"
            and "proposed" in answer.lower()
            and "have not changed anything" in answer
            and spoken == [answer])


check("the brain turns a development request into a tracked run, delivered once",
      development_reply)


def greeting_record():
    before = {r["id"] for r in intelligence.runs(200)}
    answer, _spoken = brain_reply("hello")
    new = [r for r in intelligence.runs(200) if r["id"] not in before]
    return (len(new) == 1 and new[0]["executor"] == "fast_path"
            and new[0]["intent"] == intelligence.CONVERSATION
            and "Hello" in answer)


check("a fast-path answer still produces a decision record",
      greeting_record)
check("crisis language gets a deterministic safe answer, not a model answer",
      _crisis_path)
check("an unsafe request is refused before any model or tool runs",
      _unsafe_path)


def no_emotion_persistence():
    from harry_core.brain import Brain
    from harry_core.memory import get_memory
    brain = Brain()
    brain.memory = _StubMemory()
    brain.say = lambda *a, **k: None
    brain.handle_input("this is still broken and I am fed up - improve your voice "
                       "handling", source="ui")
    metas = brain.memory.conversation.metas
    stored = " ".join(str(item.get("text", ""))
                      for item in get_memory().longterm.all()).lower()
    return (metas[0] == {"source": "ui"}
            and set(metas[-1]) == {"request_id"}
            and "fed up" not in stored
            and "frustrat" not in stored)


check("an emotional message is not stored as inferred state in memory",
      no_emotion_persistence)

# --------------------------------------------------------------- HTTP surface
ADMIN_PW = "correct horse battery staple"
USER_PW = "another long password 123"


def _client(email=None, password=None):
    """A real Flask client with a real signed session cookie."""
    import flask
    from harry_core import accounts
    from harry_core.ui import create_app
    store = accounts.get_store()
    if not store.by_email("owner@example.com"):
        store.create("owner@example.com", ADMIN_PW, "Owner")
    if not store.by_email("person@example.com"):
        store.create("person@example.com", USER_PW, "Person")
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    if email:
        html = client.get("/login").get_data(as_text=True)
        match = re.search(r'name="csrf" value="([^"]+)"', html)
        client.post("/login", data={"email": email, "password": password,
                                    "csrf": match.group(1) if match else ""})
    return client


def _session_csrf(client):
    with client.session_transaction() as session:
        return session.get("csrf") or ""


def routes_require_authorization():
    anonymous = _client()
    plain_user = _client("person@example.com", USER_PW)
    results = []
    for client in (anonymous, plain_user):
        for path, method in (("/api/admin/brain/runs", "get"),
                             ("/api/admin/devwork", "get"),
                             (("/api/admin/devwork/propose"), "post"),
                             ("/api/admin/devwork/dev_abcdef1234/start", "post")):
            kws = {"json": {}}
            if method == "post":
                token = _session_csrf(client)
                kws["headers"] = {"X-CSRF-Token": token, "X-Admin-CSRF": token}
            response = getattr(client, method)(path, **kws)
            results.append(response.status_code in (401, 403))
    return all(results)


check("every EDITH endpoint refuses signed-out and ordinary accounts",
      routes_require_authorization)


def elevated_admin_flow():
    client = _client("owner@example.com", ADMIN_PW)
    token = _session_csrf(client)
    entered = client.post("/api/admin/enter", json={"password": ADMIN_PW,
                                                    "code": ""},
                          headers={"X-CSRF-Token": token})
    if entered.status_code != 200:
        return f"elevation failed: {entered.status_code} {entered.get_data(as_text=True)[:120]}"
    headers = {"X-CSRF-Token": token, "X-Admin-CSRF": token}
    runs = client.get("/api/admin/brain/runs", headers=headers)
    dev = client.get("/api/admin/devwork", headers=headers)
    bad_run = client.get("/api/admin/brain/runs/not-a-run", headers=headers)
    bad_dev = client.post("/api/admin/devwork/dev_zzzz/start", json={},
                          headers=headers)
    empty = client.post("/api/admin/devwork/propose", json={}, headers=headers)
    body = runs.get_json() or {}
    return (runs.status_code == 200 and "runs" in body and "stats" in body
            and dev.status_code == 200
            and bad_run.status_code == 404
            and bad_dev.status_code == 404
            and empty.status_code == 400
            and empty.get_json().get("error"))


check("an elevated administrator can read records; bad ids are rejected",
      elevated_admin_flow)


def admin_page_has_edith_tab():
    from harry_core.adminui import PAGE
    return ("data-tab=\"edith\"" in PAGE and "id=\"tab-edith\"" in PAGE
            and "loadEdith" in PAGE and "renderDevRuns" in PAGE)


check("the admin page ships the EDITH tab and its loader",
      admin_page_has_edith_tab)


def path_traversal_is_not_a_run_id():
    """A traversal attempt is answered as an invalid id, never read from disk."""
    client = _client("owner@example.com", ADMIN_PW)
    token = _session_csrf(client)
    client.post("/api/admin/enter", json={"password": ADMIN_PW, "code": ""},
                headers={"X-CSRF-Token": token})
    headers = {"X-CSRF-Token": token, "X-Admin-CSRF": token}
    response = client.get("/api/admin/brain/runs/..%2F..%2Fetc%2Fpasswd",
                          headers=headers)
    return response.status_code == 404 and "root:" not in (
        response.get_data(as_text=True) or "")


check("a traversal attempt is rejected as an invalid run id",
      path_traversal_is_not_a_run_id)

print(f"\n===== RESULT: {len(PASS)} passed, {len(FAIL)} failed =====")
for failure in FAIL:
    print(f"  FAILED: {failure}")
sys.exit(1 if FAIL else 0)

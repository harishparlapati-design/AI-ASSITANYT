"""Harry's Core Brain: the explicit request pipeline.

Every user utterance goes through the same named stages, and each stage leaves
evidence behind instead of living as an implicit side effect:

    UNDERSTAND  context + intent + entities + human signals   (deterministic)
    ROUTE       agent role, model class, capability-gated tools
    PLAN        ordered steps for actionable requests
    EXECUTE     step execution, delegated to the task engine / development run
    OBSERVE     tool results, exit status, artifacts on disk
    VERIFY      acceptance criteria checked against the observation
    RECOVER     bounded retry, safe alternative, or ask the user
    RESPOND     human-aware shaping of the final answer

This module owns UNDERSTAND, ROUTE, the instruction block and the run record.
Execution, observation, verification and recovery stay in
``harry_core.task_engine`` where the real work already lives - the brain records
what those stages reported rather than duplicating them.

Transparency rule: decision records carry the *reasons* for a routing choice
(matched signals, chosen specialist, excluded tools) and the evidence a stage
produced. They never carry private chain-of-thought, and a missing measurement
is stored as ``None`` rather than as a guess.
"""

import json
import logging
import os
import re
import threading
import time
import uuid

from . import agents, communication, config, runtime_paths

log = logging.getLogger("harry.intelligence")

CONVERSATION = "conversation"
QUESTION = "question"
TASK = "task"
CONTROL = "control"
DEVELOPMENT = "development"
MEMORY = "memory"
DIAGNOSTICS = "diagnostics"

INTENTS = (CONVERSATION, QUESTION, TASK, CONTROL, DEVELOPMENT, MEMORY, DIAGNOSTICS)

MAX_RUNS = 300

# --------------------------------------------------------------------- patterns

_CONTROL = re.compile(
    r"(?:^|\b)(?:emergency stop|emergency)\b|"
    r"^\s*(?:harry[,\s]+)?(?:stop|cancel|abort|halt)\b",
    re.IGNORECASE,
)
_MEMORY = re.compile(
    r"^\s*(?:remember|forget)\b|\bwhat do you (?:remember|know about me)\b|"
    r"\bforget (?:that|about)\b",
    re.IGNORECASE,
)
_DIAGNOSTICS = re.compile(
    r"\b(?:system check|diagnostics?|health check|is everything working|"
    r"what(?:'s| is) broken)\b",
    re.IGNORECASE,
)
_GREETING = re.compile(
    r"^\s*(?:hi+|hello+|hey+|hai|namaste|namaskaram|"
    r"good\s+(?:morning|afternoon|evening|night))[\s!,.?]*$",
    re.IGNORECASE,
)
_THANKS = re.compile(r"^\s*(?:ok\s+)?(?:thanks|thank you|thx|tq)[\s!.?]*$",
                     re.IGNORECASE)
_FOLLOW_UP = re.compile(
    r"^\s*(?:and|also|so|then|but|what about|how about|why|how so|"
    r"explain (?:that|it|this)|tell me more|continue|go on|it|its|that|this|"
    r"they|those|these|the same|the (?:first|second|last) one)\b",
    re.IGNORECASE,
)
_SENSITIVE_ACTION = re.compile(
    r"\b(?:delete|remove|format|wipe|uninstall|drop|overwrite|deploy|push|"
    r"publish|transfer|payment|purchase|credential|password|migrate)\b",
    re.IGNORECASE,
)

# -- development intent --------------------------------------------------------
# A request to change Harry itself is not an ordinary task and not ordinary
# conversation. It needs a tracked development run with an approval gate, so it
# is detected before the question/task split.
_SELF_REFERENCE = re.compile(
    r"\b(?:you|your|yours|yourself|harry|harry'?s|edith)\b", re.IGNORECASE)
_DEV_VERB = re.compile(
    r"\b(?:add|build|implement|create|develop|upgrade|improve|fix|extend|"
    r"teach|learn|support|integrate|optimi[sz]e|speed up|make|train|"
    r"investigate|repair|redesign|refactor)\b",
    re.IGNORECASE,
)
_DEV_TARGET = re.compile(
    r"\b(?:feature|features|capabilit\w*|skill|skills|plugin|plugins|module|"
    r"pipeline|behavio\w*|response (?:time|speed)|latency|performance|voice|"
    r"hearing|listening|memory|interface|ui|dashboard|screen.?shar\w*|"
    r"summari[sz]\w*|ocr|tool|tools|integration|integrations|yourself|"
    r"your own|development|yourself)\b",
    re.IGNORECASE,
)
_DEV_EXPLICIT = re.compile(
    r"\b(?:develop yourself|improve yourself|teach yourself|upgrade yourself|"
    r"self[- ]?develop\w*|your (?:own )?development|progress of your "
    r"development|make yourself (?:faster|better|smarter)|you are (?:slow|"
    r"too slow)|fix your\w*|add a feature (?:that|so))\b",
    re.IGNORECASE,
)
_DEV_TOOL_FIX = re.compile(
    r"\bfix (?:this|that|the|your) (?:tool|plugin|integration|feature)\b|"
    r"\bwhy (?:does|did) (?:this|that|the) (?:tool|plugin|feature) (?:fail|break)\b|"
    r"\b(?:this|that|the) (?:tool|plugin|integration|feature) (?:fails|failed|"
    r"is broken|is not working|doesn'?t work|does not work)\b",
    re.IGNORECASE,
)
# A user asking for content that happens to use the word "feature" must not
# enter the self-development workflow: their own project is not Harry.
_USER_PROJECT = re.compile(
    r"\b(?:my|our|the user'?s) (?:website|app|site|project|repo|repository|"
    r"code|portfolio|page|server|api)\b",
    re.IGNORECASE,
)
_ARTIFACT_WORD = re.compile(
    r"\b[a-z0-9_-]+\.(?:txt|md|pdf|docx|xlsx|pptx|csv|json|py|js|ts|html|css|"
    r"png|jpe?g|zip|log|ya?ml|ini)\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
_PATH = re.compile(r"(?:[A-Za-z]:\\[^\s\"']+|(?:~|\.{1,2})?/[^\s\"']+)")


# ------------------------------------------------------------------- persistence

def _runs_path():
    override = os.environ.get("HARRY_BRAIN_RUNS_JSON", "").strip()
    if override:
        return os.path.abspath(override)
    folder = os.path.join(runtime_paths.workspace_root(),
                          config.CONFIG.get("data_dir", "data"))
    return os.path.join(folder, "brain_runs.jsonl")


# The log is append-only: each line is one snapshot of a run, the last line for
# an id wins. This keeps the cost of recording a request to a ~1ms append.
# Rewriting the whole array twice per turn used to add ~80ms to every reply,
# which was visible even on the deterministic fast path.
_lock = threading.RLock()
_cache = {"path": None, "runs": {}, "lines": 0}


def _state():
    """Load the log once, then keep the index in memory."""
    path = _runs_path()
    with _lock:
        if _cache["path"] == path:
            return _cache
        runs, lines = {}, 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        run = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(run, dict) and run.get("id"):
                        runs[run["id"]] = run
                        lines += 1
        except OSError:
            pass
        _cache.update(path=path, runs=runs, lines=lines)
        return _cache


def _compact(state):
    ordered = sorted(state["runs"].values(), key=lambda r: r.get("started") or 0)
    keep = ordered[-MAX_RUNS:]
    tmp = state["path"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for run in keep:
            f.write(json.dumps(run, ensure_ascii=False) + "\n")
    os.replace(tmp, state["path"])
    state["runs"] = {r["id"]: r for r in keep}
    state["lines"] = len(keep)


def _append(run):
    state = _state()
    os.makedirs(os.path.dirname(state["path"]) or ".", exist_ok=True)
    with _lock:
        with open(state["path"], "a", encoding="utf-8") as f:
            f.write(json.dumps(run, ensure_ascii=False) + "\n")
        state["runs"][run["id"]] = run
        state["lines"] += 1
        if state["lines"] > MAX_RUNS * 2:
            _compact(state)


def _mutate(run_id, **fields):
    state = _state()
    with _lock:
        run = state["runs"].get(run_id)
        if run is None:
            return None
        run.update(fields)
        _append(dict(run))
        return run


# -------------------------------------------------------------------- understand

def development_request(text):
    """Return a description of a self-development request, or None.

    Conservative by design: a false positive would send an ordinary request
    into an approval workflow, so every match needs a self-reference *and* a
    change verb *and* a changeable target.
    """
    text = str(text or "")
    if not text.strip():
        return None
    if _USER_PROJECT.search(text) and not _DEV_EXPLICIT.search(text):
        return None
    reasons = []
    explicit = bool(_DEV_EXPLICIT.search(text))
    if explicit:
        reasons.append("explicit self-development phrasing")
    if _DEV_TOOL_FIX.search(text):
        reasons.append("asks to repair an owned tool/plugin")
    verb = bool(_DEV_VERB.search(text))
    target = bool(_DEV_TARGET.search(text))
    self_ref = bool(_SELF_REFERENCE.search(text))
    if verb:
        reasons.append("contains a change verb")
    if target:
        reasons.append("names a changeable capability")
    if self_ref:
        reasons.append("refers to Harry itself")
    qualifies = explicit or bool(_DEV_TOOL_FIX.search(text)) or (
        verb and target and self_ref)
    if not qualifies:
        return None
    return {"request": text.strip()[:500], "reasons": reasons,
            "explicit": explicit}


def _entities(text):
    return {
        "files": sorted(set(_ARTIFACT_WORD.findall(text)))[:8],
        "urls": sorted(set(_URL.findall(text)))[:8],
        "paths": sorted(set(_PATH.findall(text)))[:8],
        "numbers": re.findall(r"\b\d+(?:\.\d+)?\b", text)[:8],
    }


def _classify_question_or_task(text):
    """Reuse the engine's deterministic rules so both paths agree."""
    try:
        from .task_engine import get_engine
        return get_engine().classify_request(text)[0]
    except Exception as e:      # engine unavailable (fresh import, no data dir)
        log.debug("classify unavailable (%s); defaulting to question", e)
        return QUESTION


def understand(text, source="text", preferred_verbosity=None):
    """Stage 1: what is this message, who is asking it, and how are they?
    """
    raw = str(text or "")
    stripped = raw.strip()
    low = stripped.lower()
    reasons = []
    confidence = 0.6

    intent, development = CONVERSATION, None
    if not stripped:
        intent = CONVERSATION
    elif _CONTROL.search(stripped):
        intent = CONTROL
        reasons.append("control verb (stop/cancel/emergency)")
        confidence = 0.95
    elif _MEMORY.search(stripped):
        intent = MEMORY
        reasons.append("memory command (remember/forget/recall)")
        confidence = 0.9
    elif _DIAGNOSTICS.search(stripped):
        intent = DIAGNOSTICS
        reasons.append("system health question")
        confidence = 0.9
    elif development_request(stripped):
        development = development_request(stripped)
        intent = DEVELOPMENT
        reasons.extend(development["reasons"])
        confidence = 0.85
    elif _GREETING.match(stripped) or _THANKS.match(stripped):
        intent = CONVERSATION
        reasons.append("greeting or acknowledgement")
        confidence = 0.95
    else:
        classified = _classify_question_or_task(stripped)
        intent = TASK if classified == TASK else QUESTION
        reasons.append("action verb on a concrete artifact" if intent == TASK
                       else "information request")
        confidence = 0.7

    try:
        agent = agents.select_agent(stripped)
    except Exception:
        agent = agents.GENERAL
    human = communication.read(stripped, preferred_verbosity)
    requires_approval = intent == DEVELOPMENT or (
        intent == TASK and bool(_SENSITIVE_ACTION.search(stripped)))

    return {
        "text": stripped[:2000],
        "source": source,
        "intent": intent,
        "confidence": round(confidence, 2),
        "reasons": reasons,
        "domain": agent.key,
        "agent": agent.name,
        "follow_up": bool(_FOLLOW_UP.match(stripped)),
        "entities": _entities(stripped),
        "human": human,
        "requires_approval": requires_approval,
        "development": development,
        "empty": not stripped,
    }


# ------------------------------------------------------------------------- route

def _tool_state(name):
    """Capability status for a tool, or '' when it has no record yet."""
    try:
        from . import capabilities
        record = capabilities.get_capability(name)
    except Exception:
        return ""
    return str((record or {}).get("status") or "")


def route(understanding):
    """Stage 2: which specialist, which model class, which tools may be used.

    Tools are gated by the capability registry, not by the prompt: a disabled,
    failed or rejected capability is excluded from the plan and reported as
    unavailable, so Harry can say what it cannot do instead of trying anyway.
    """
    text = (understanding or {}).get("text") or ""
    intent = (understanding or {}).get("intent")
    try:
        agent = agents.select_agent(text)
    except Exception:
        agent = agents.GENERAL
    # Loading the tool registry imports every builtin and legacy tool. Doing
    # that for a greeting cost ~1s on the first request of a process, so it is
    # only done for intents that can actually use a tool.
    wants_tools = intent not in (CONVERSATION, CONTROL, MEMORY)
    registered = set()
    if wants_tools:
        try:
            from .tools import get_registry
            registered = set(get_registry().names())
        except Exception:
            registered = set()

    preferred = [] if not wants_tools else [
        t for t in agent.preferred_tools if not registered or t in registered]
    blocked = [t for t in preferred if _tool_state(t) in
               ("disabled", "failed", "rejected")]
    tools = [t for t in preferred if t not in blocked]
    missing = ([t for t in agent.preferred_tools if registered and t not in registered]
               if wants_tools else [])
    if wants_tools:
        why = f"{agent.name} selected for the {understanding.get('domain', 'general')} domain"
    else:
        why = (f"{agent.name} for the {understanding.get('domain', 'general')} domain; "
               "no tool is needed for this intent")
    if blocked:
        why += f"; excluded (capability not usable): {', '.join(blocked)}"
    if missing:
        why += f"; not installed: {', '.join(missing)}"
    return {
        "agent": agent.key,
        "agent_name": agent.name,
        "model_class": agent.task_class,
        "tools": tools,
        "blocked_tools": blocked,
        "missing_tools": missing,
        "why": why,
    }


def instructions(understanding, routing=None):
    """The prompt addendum for this request: communication + evidence rules."""
    understanding = understanding or {}
    parts = []
    guidance = communication.prompt_guidance(understanding.get("human"),
                                             understanding.get("intent"))
    if guidance:
        parts.append(guidance)
    intent = understanding.get("intent")
    if intent == TASK:
        parts.append("Work the steps in order, check the result of each one, "
                     "and report evidence rather than intentions.")
    elif intent == DEVELOPMENT:
        parts.append("This is a request to change Harry itself. Do not claim "
                     "the change is done; a development run must be planned, "
                     "approved, staged and tested first.")
    elif intent == DIAGNOSTICS:
        parts.append("Report only measurements you actually took; say unknown "
                     "where a value could not be measured.")
    elif intent == CONTROL:
        parts.append("Confirm the stop briefly and do not start new work.")
    if routing and routing.get("blocked_tools"):
        parts.append("These capabilities are currently unusable: "
                     + ", ".join(routing["blocked_tools"])
                     + ". Say so instead of pretending to use them.")
    return " ".join(parts)


# ------------------------------------------------------------------- run records

def start_run(understanding, routing, source="text"):
    """Open a decision record for one request; returns its id."""
    run = {
        "id": "run_" + uuid.uuid4().hex[:12],
        "started": time.time(),
        "finished": None,
        "source": source,
        "text": str((understanding or {}).get("text") or "")[:400],
        "intent": (understanding or {}).get("intent"),
        "confidence": (understanding or {}).get("confidence"),
        "reasons": (understanding or {}).get("reasons") or [],
        "domain": (understanding or {}).get("domain"),
        "human": (understanding or {}).get("human") or {},
        "entities": (understanding or {}).get("entities") or {},
        "route": routing or {},
        "executor": None,
        "status": "running",
        "stages": [],
        "evidence": [],
        "answer": None,
        "error": None,
        "ms": None,
    }
    _append(run)
    return run["id"]


def add_stage(run_id, name, detail=None, ms=None):
    """Record one pipeline stage so EDITH can show what actually ran."""
    run = _state()["runs"].get(run_id)
    if not run:
        return
    stages = list(run.get("stages") or []) + [
        {"name": str(name)[:40], "detail": detail, "ms": ms,
         "at": time.time()}]
    _mutate(run_id, stages=stages[-30:])


def add_evidence(run_id, item):
    """Attach one observation (tool result, file check) to the record."""
    run = _state()["runs"].get(run_id)
    if not run:
        return
    evidence = list(run.get("evidence") or []) + [item]
    _mutate(run_id, evidence=evidence[-20:])


def finish_run(run_id, status="ok", executor=None, answer=None, error=None,
               ms=None):
    fields = {"finished": time.time(), "status": status}
    if executor:
        fields["executor"] = executor
    if answer is not None:
        fields["answer"] = str(answer)[:800]
    if error:
        fields["error"] = str(error)[:400]
    if ms is not None:
        fields["ms"] = int(ms)
    _mutate(run_id, **fields)
    return run_id


def runs(limit=50):
    items = [dict(r) for r in _state()["runs"].values()]
    items.sort(key=lambda r: r.get("started") or 0, reverse=True)
    return items[:int(limit)]


def get_run(run_id):
    run = _state()["runs"].get(str(run_id))
    return dict(run) if run else None


def stats():
    """Aggregate view for EDITH: what the brain has been asked to do."""
    items = [dict(r) for r in _state()["runs"].values()]
    by_intent, by_status, by_agent = {}, {}, {}
    durations = []
    for run in items:
        by_intent[run.get("intent") or "?"] = by_intent.get(run.get("intent") or "?", 0) + 1
        by_status[run.get("status") or "?"] = by_status.get(run.get("status") or "?", 0) + 1
        agent = (run.get("route") or {}).get("agent") or "?"
        by_agent[agent] = by_agent.get(agent, 0) + 1
        if isinstance(run.get("ms"), int):
            durations.append(run["ms"])
    return {
        "total": len(items),
        "by_intent": by_intent,
        "by_status": by_status,
        "by_agent": by_agent,
        "median_ms": (sorted(durations)[len(durations) // 2] if durations else None),
        "measured_runs": len(durations),
    }

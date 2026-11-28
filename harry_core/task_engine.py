"""Autonomous task engine.

Real task lifecycle with persistence:

    NEW -> UNDERSTANDING -> PLANNING -> (WAITING_FOR_PERMISSION) ->
        EXECUTING -> VERIFYING -> COMPLETED
                        \\-> ERROR -> ANALYZE_ERROR -> RETRY -> ... -> FAILED

Tasks are persisted to data/tasks.json so interrupted tasks can be resumed.
The executor never repeats a step that already succeeded (steps record their
status individually). Emergency stop is honored at every step boundary.
"""

import json
import logging
import os
import re
import threading
import time
import uuid

from . import config, events, llm, timing
from .memory import get_memory
from .tools import get_registry

log = logging.getLogger("harry.tasks")


def _extract_json(raw):
    """Parse the first JSON object OR array found in a model reply."""
    m = re.search(r"[\[{].*[\]}]", raw, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON in model reply: {raw[:120]}")
    return json.loads(m.group(0))

NEW, UNDERSTANDING, PLANNING, WAITING_FOR_PERMISSION, EXECUTING, VERIFYING, \
    COMPLETED, ERROR, ANALYZING_ERROR, RETRYING, FAILED, CANCELLED = (
        "NEW", "UNDERSTANDING", "PLANNING", "WAITING_FOR_PERMISSION",
        "EXECUTING", "VERIFYING", "COMPLETED", "ERROR", "ANALYZING_ERROR",
        "RETRYING", "FAILED", "CANCELLED",
    )

MAX_RETRIES = 3
MAX_PLAN_RETRIES = 2

# A message that refers back to the previous turn (so conversation history is
# worth sending to the model). Standalone questions skip the history.
_FOLLOWUP = re.compile(
    r"^\s*(?:and|also|so|then|but|what about|how about|why|how so|explain (?:that|it|"
    r"this)|tell me more|continue|go on|it|its|that|this|they|those|these|the same)\b",
    re.IGNORECASE)


class Task:
    def __init__(self, title, description="", task_id=None, request_id=None,
                 understanding=None, plan=None):
        # The millisecond stamp plus a uuid suffix guarantees unique ids even
        # when two tasks are created inside the same millisecond.
        self.id = task_id or f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        self.request_id = request_id      # idempotency key from the client
        self.title = title
        self.description = description
        # What the Core Brain understood about this request (intent, human
        # signals, entities). Used for prompt guidance and the decision record.
        self.understanding = understanding or {}
        # Plan contract: acceptance criteria, risk level, requires_approval and
        # the time budget this task may consume.
        self.plan = plan or {}
        self.status = NEW
        self.kind = "question"            # question | task (decided in UNDERSTANDING)
        self.goal = ""                    # short restatement (display only)
        self.steps = []                   # [{index, description, status, result}]
        self.current_step = 0
        self.attempts = 0
        self.error = None
        self.result = None
        self.created_at = time.time()
        self.updated_at = time.time()
        self.answer = None                # final user-facing answer

    # -- serialization -----------------------------------------------------

    def to_dict(self):
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d):
        t = cls(d.get("title", ""), d.get("description", ""), d.get("id"),
                d.get("request_id"))
        t.__dict__.update(d)
        return t


class TaskEngine:
    def __init__(self):
        from . import runtime_paths
        self.path = os.path.join(
            runtime_paths.workspace_root(),
            config.CONFIG.get("data_dir", "data"), "tasks.json",
        )
        self.tasks = {}
        self._lock = threading.RLock()
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.tasks = {t["id"]: Task.from_dict(t) for t in data.get("tasks", [])}
        except Exception:
            self.tasks = {}

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tasks": [t.to_dict() for t in self.tasks.values()]},
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # -- CRUD --------------------------------------------------------------

    def create_task(self, title, description="", request_id=None,
                    understanding=None, plan=None):
        """Create a task. A repeated ``request_id`` returns the first task.

        The request id comes from the client, so a resubmitted request (double
        click, retry after a reconnect) can never create a second task.
        """
        with self._lock:
            if request_id:
                for existing in self.tasks.values():
                    if existing.request_id == request_id:
                        log.info("task %s reused for request %s",
                                 existing.id, request_id)
                        return existing.to_dict()
            task = Task(title, description, request_id=request_id,
                        understanding=understanding, plan=plan)
            self.tasks[task.id] = task
            self._save()
        events.emit("task_created", {"id": task.id, "title": title})
        return task.to_dict()

    def get(self, task_id):
        return self.tasks.get(task_id)

    def list_tasks(self, status=None, limit=None):
        tasks = list(self.tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        tasks.sort(key=lambda t: -t.created_at)
        if limit:
            tasks = tasks[:int(limit)]
        return [t.to_dict() for t in tasks]

    def update_status(self, task, status):
        task.status = status
        task.updated_at = time.time()
        with self._lock:
            self._save()
        events.emit("task_status", {"id": task.id, "status": status,
                                    "title": task.title})
        log.info("task %s -> %s", task.id, status)

    # -- intent classification ----------------------------------------------

    def classify_request(self, text):
        """Decide if the user wants an ANSWER (question) or an ACTION (task).

        Deterministic by design. A model call here used to cost 5-18s on CPU
        before the answer even started, and it sometimes sent content requests
        ("write a python function") into the file-mutating planner, which then
        failed and told the user nothing.

        Rule: a strong machine/world verb, or a weak creation verb together
        with a file/format word, makes a task. Everything else is answered in
        chat, including code and prose requests.
        """
        text = str(text or "")
        chat_instruction = bool(re.search(
            r"\b(reply|respond|say|answer|tell me|repeat|translate)\b",
            text, re.IGNORECASE))
        strong = bool(re.search(
            r"\b(run|execute|install|uninstall|delete|remove|move|rename|copy|"
            r"organize|open|close|download|upload|deploy|commit|push|send|email|"
            r"schedule|remind me|screenshot|fix|repair|debug|refactor|configure|"
            r"set ?up|update|clean|convert|extract|compress|zip|unzip|print)\b",
            text, re.IGNORECASE))
        weak = bool(re.search(
            r"\b(create|make|write|generate|build|add|export|save|draft)\b",
            text, re.IGNORECASE))
        file_hint = bool(re.search(
            r"\.(txt|md|pdf|docx|xlsx|pptx|csv|json|py|html)\b|"
            r"\b(file|folder|directory|spreadsheet|presentation|slides?|"
            r"report|invoice|resume|document)\b",
            text, re.IGNORECASE))
        if chat_instruction and not (strong or (weak and file_hint)):
            return "question", text
        if strong or (weak and file_hint):
            log.debug("classify: task (%s)", text[:60])
            return "task", text
        log.debug("classify: question (%s)", text[:60])
        return "question", text

    def _classify_with_model(self, text):
        """Optional model classification, kept for experiments only.

        Enable with HARRY_CLASSIFY_MODEL=1. Not used by default: the rules
        above are faster and did not produce better outcomes in testing.
        """

        import os as _os
        if str(_os.environ.get("HARRY_CLASSIFY_MODEL", "")).lower() not in ("1", "true", "yes"):
            return self.classify_request(text)
        prompt = (
            "Classify what the user wants. Answer with JSON only:\n"
            '{"type": "question" | "task", "goal": "<short restated goal>"}\n\n'
            '"question" = the user wants information, an explanation or content\n'
            '"task" = the user wants something changed on the computer or in the world\n\n'
            "Examples:\n"
            'fix my portfolio gallery -> {"type": "task"}\n'
            'what is python used for -> {"type": "question"}\n'
            'write a python function to reverse a string -> {"type": "question"}\n'
            'create a file called notes.txt -> {"type": "task"}\n'
            'ela unnav? -> {"type": "question"}\n\n'
            f"User request: {text}"
        )
        try:
            raw = llm.chat(
                [{"role": "user", "content": prompt}],
                task_class=llm.ROUTING,
                options={"num_predict": 60},
                json_mode=True,
            )
            data = _extract_json(raw)
            if isinstance(data, list):
                data = data[0] if data else {}
            kind = data.get("type", "question")
            if kind not in ("question", "task"):
                kind = "question"
            return kind, data.get("goal", text)
        except Exception:
            return self.classify_request(text)

    # -- planning ------------------------------------------------------------

    def make_plan(self, task):
        """Break a task into concrete steps with a tool hint each.

        Falls back to a single generic step offline; the executor then lets
        the model + tools work it out anyway.
        """
        from .agents import catalog_hint, select_agent
        from .language_policy import instruction as language_instruction
        from . import config
        agent = select_agent(task.title)
        lang_rule = language_instruction(
            task.title, config.CONFIG.get("language", {}).get("preference", "auto")
        )
        catalog = get_registry().catalog()
        prompt = f"""You are the planner of an autonomous assistant, acting as the
{agent.name} ({agent.description}).
Break the GOAL into 1-6 concrete, ordered steps. Prefer using available tools.
This specialist usually needs: {catalog_hint(agent)}

Available tools:
{catalog}

Respond with JSON only:
{{"steps": [{{"description": "...", "tool": "tool_name or null", "args": {{}}}}]}}

Rules:
- Preserve EXACT filenames, paths and content strings from the GOAL in the steps.
- Prefer a direct tool step (e.g. write_file) over vague "identify" steps.
- {lang_rule}

GOAL (exact user request): {task.title}
CONTEXT: {task.description or 'none'}
Memory context: {self._memory_context(task.title)}"""
        try:
            raw = llm.chat([{"role": "user", "content": prompt}],
                           task_class=llm.REASONING,
                           options={"num_predict": 400, "temperature": 0.2},
                           json_mode=True)
            data = _extract_json(raw)
            steps = []
            entries = data.get("steps", []) if isinstance(data, dict) else data
            for s in entries[:6]:
                if isinstance(s, dict) and s.get("description"):
                    steps.append({
                        "description": str(s["description"]),
                        "tool": s.get("tool") or None,
                        "args": s.get("args") or {},
                        "status": "pending",
                        "result": None,
                    })
            if steps:
                return steps
        except Exception as e:
            log.warning("LLM planning failed (%s); using generic step", e)
        return [{
            "description": f"Work on: {task.title}",
            "tool": None, "args": {}, "status": "pending", "result": None,
        }]

    def _memory_context(self, query):
        try:
            hits = get_memory().search(query, limit=3)
            return "; ".join(h["text"][:100] for h in hits) or "none"
        except Exception:
            return "none"

    # -- execution ------------------------------------------------------------

    def run_task(self, task_id, on_delta=None):
        """Execute a task through the full lifecycle. Returns the task dict.

        ``on_delta`` receives each answer fragment as the model writes it, so
        callers can stream text (and start speech) before the answer is done.
        """
        task = self.tasks.get(task_id)
        if not task:
            return {"ok": False, "error": f"unknown task {task_id}"}

        t_task = time.perf_counter()

        def stage(name, started):
            log.info("[%s] %s took %.0fms", task.id, name,
                     (time.perf_counter() - started) * 1000)

        # 1. UNDERSTANDING
        if task.status in (NEW, ERROR, ANALYZING_ERROR):
            self.update_status(task, UNDERSTANDING)
            t0 = time.perf_counter()
            with timing.stage("classify"):
                kind, goal = self.classify_request(task.title)
            stage("classify", t0)
            task.kind = kind
            # task.title keeps the EXACT original request (filenames, content,
            # details). The model's restatement is only a display label.
            task.goal = goal
            self.update_status(task, PLANNING if kind == "task" else EXECUTING)
            if kind == "question":
                task.steps = []
            events.emit("task_understood", {"id": task.id, "kind": kind, "goal": goal})

        # 2. PLANNING
        if task.status == PLANNING and not task.steps:
            t0 = time.perf_counter()
            task.steps = self.make_plan(task)
            stage("plan", t0)
            with self._lock:
                self._save()
            events.emit("task_planned", {"id": task.id,
                                         "steps": [s["description"] for s in task.steps]})
            self.update_status(task, EXECUTING)

        # 3. EXECUTING step by step (never repeats completed steps)
        try:
            if task.kind == "question":
                t0 = time.perf_counter()
                task.answer = self._answer_question(task, on_delta=on_delta)
                stage("answer", t0)
            else:
                self._execute_steps(task)
        except events.EmergencyStop:
            self.update_status(task, CANCELLED)
            task.error = "emergency stop"
            return task.to_dict()

        # 4. VERIFYING (verification failure triggers re-planning, not instant
        #    failure - the agent self-corrects up to MAX_PLAN_RETRIES times)
        if task.status != CANCELLED:
            self.update_status(task, VERIFYING)
            if task.kind == "question":
                self.update_status(task, COMPLETED)
            else:
                t0 = time.perf_counter()
                ok = self._verify(task)
                stage("verify", t0)
                if not ok and task.attempts < MAX_PLAN_RETRIES:
                    task.attempts += 1
                    log.info("task %s verification failed; re-planning "
                             "(attempt %s/%s)", task.id, task.attempts, MAX_PLAN_RETRIES)
                    task.steps = self.make_plan(task)
                    with self._lock:
                        self._save()
                    events.emit("task_replanned", {
                        "id": task.id, "attempt": task.attempts,
                        "steps": [s["description"] for s in task.steps]})
                    self.update_status(task, EXECUTING)
                    return self.run_task(task_id)   # loop: execute -> verify again
                self.update_status(task, COMPLETED if ok else FAILED)

        log.info("[%s] pipeline finished in %.0fms (%s)", task.id,
                 (time.perf_counter() - t_task) * 1000, task.status)
        return task.to_dict()

    def _execute_steps(self, task):
        registry = get_registry()
        started = time.time()
        try:
            budget = float((task.plan or {}).get("budget_seconds"))
        except (TypeError, ValueError):
            budget = None      # no budget recorded -> no artificial limit
        for step in task.steps:
            events.check_stop()
            # >= so a zero budget is a real limit instead of a coin flip on the
            # platform clock's resolution.
            if budget is not None and time.time() - started >= budget:
                task.error = (f"time budget of {budget:g}s exceeded before "
                              f"'{str(step.get('description'))[:60]}'")
                events.emit("task_error", {"id": task.id,
                                           "error": task.error,
                                           "step": step.get("description")})
                self.update_status(task, FAILED)
                return
            if step["status"] == "done":
                continue                      # never redo succeeded work
            step["status"] = "running"
            with self._lock:
                self._save()
            try:
                result = self._execute_step(task, step)
                step["result"] = result
                step["status"] = "done"
                events.emit("task_step_done", {"id": task.id,
                                               "step": step["description"],
                                               "result": str(result)[:200]})
            except events.EmergencyStop:
                raise
            except Exception as e:
                step["status"] = "error"
                step["result"] = f"{type(e).__name__}: {e}"
                if not self._handle_error(task, step, e):
                    self.update_status(task, FAILED)
                    task.error = str(e)
                    return
            finally:
                task.current_step = step["index"] if "index" in step else \
                    task.steps.index(step)
                with self._lock:
                    self._save()

    def _execute_step(self, task, step):
        """Run one plan step: direct tool, or LLM with tools for open-ended steps."""
        registry = get_registry()
        tool = registry.get(step["tool"]) if step.get("tool") else None
        if tool:
            result = registry.run(step["tool"], step.get("args") or {})
            if result.get("ok"):
                return result.get("result", result)
            # tool failed -> let the LLM analyze below instead of dying
            log.warning("step tool %s failed: %s", step["tool"], result.get("error"))

        # open-ended step: let the model drive with tool catalog + one tool call
        answer = self._llm_execute_step(task, step)
        return answer

    def _llm_execute_step(self, task, step):
        registry = get_registry()
        from .language_policy import instruction as language_instruction
        from . import config
        lang_rule = language_instruction(
            task.title, config.CONFIG.get("language", {}).get("preference", "auto")
        )
        prior = "\n".join(
            f"- {s['description']}: {str(s.get('result'))[:150]}"
            for s in task.steps if s.get("result")
        ) or "none yet"
        prompt = f"""You are an autonomous agent EXECUTING one step of a task.
Choose ONE action. If no tool is needed, reply with JSON {{"action": "answer", "content": "..."}}.
If a tool is needed, reply with JSON {{"action": "tool", "tool": "name", "args": {{...}}}}.
If the task is destructive/sensitive, reply {{"action": "confirm", "reason": "..."}} instead.

Tools:
{registry.catalog()}

Task: {task.title}
Step: {step['description']}
Language rule for user-visible answer content: {lang_rule}
Previous step results: {prior}
Memory: {self._memory_context(task.title + ' ' + step['description'])}"""
        raw = llm.chat([{"role": "user", "content": prompt}],
                       task_class=llm.REASONING,
                       options={"num_predict": 250, "temperature": 0.2},
                       json_mode=True)
        data = _extract_json(raw)
        if isinstance(data, list):
            data = data[0] if data else {}
        action = data.get("action", "answer")

        if action == "tool":
            tool_name = data.get("tool")
            if not registry.get(tool_name):
                raise RuntimeError(f"model chose unknown tool: {tool_name}")
            result = registry.run(tool_name, data.get("args") or {})
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "tool failed"))
            return result.get("result", result)

        if action == "confirm":
            from .security import get_permissions
            if not get_permissions().confirm(data.get("reason", step["description"])):
                raise PermissionError("user denied confirmation")
            return "user approved; re-run the step"

        return data.get("content", "")

    # -- verification / error handling -----------------------------------------

    def _verify(self, task):
        failed = [s for s in task.steps if s["status"] != "done"]
        if failed:
            return False

        # Deterministic evidence first: if the goal names file artifacts, check
        # the REAL filesystem instead of trusting model judgment.
        evidence = self._filesystem_evidence(task.title)
        if evidence:
            if all(e["exists"] and e["content_ok"] is not False for e in evidence):
                log.info("task %s verified deterministically: %s", task.id, evidence)
                return True
            if any(not e["exists"] or e["content_ok"] is False for e in evidence):
                log.info("task %s evidence failed: %s", task.id, evidence)
                return False

        summary = [{"step": s["description"], "status": s["status"],
                    "result": str(s.get("result"))[:400]} for s in task.steps]
        criteria = "".join(
            f"- {c}\n" for c in ((task.plan or {}).get("acceptance_criteria") or []))
        prompt = (
            "You are verifying whether an autonomous agent accomplished a goal.\n"
            f"GOAL: {task.title}\n"
            + (f"ACCEPTANCE CRITERIA:\n{criteria}" if criteria else "")
            + "STEP RESULTS: " + json.dumps(summary, ensure_ascii=False)[:2000] + "\n\n"
            "Rules:\n"
            "- If every step shows a successful (ok) result covering the goal, "
            "respond verified=true.\n"
            "- Respond verified=false ONLY if you can name a specific requirement "
            "of the goal that is demonstrably NOT met by these results.\n"
            "- Interim outputs (directory listings, previews) do not invalidate "
            "later successful writes.\n\n"
            'Respond JSON: {"verified": true|false, "reason": "..."}'
        )
        try:
            raw = llm.chat([{"role": "user", "content": prompt}],
                           task_class=llm.REASONING,
                           options={"num_predict": 100},
                           json_mode=True)
            data = _extract_json(raw)
            if isinstance(data, list):
                data = data[0] if data else {}
            return bool(data.get("verified"))
        except Exception:
            return True   # can't verify without model; trust step completion

    def _filesystem_evidence(self, goal):
        """Find file artifacts named in the goal and verify them on disk.

        Handles goals like: create a file named notes.txt in data containing
        the text HELLO / with content HELLO. Returns [] when no artifacts are
        named (then model-based verification is used).
        """
        evidence = []
        try:
            artifacts = re.findall(
                r"\b[\w.-]+\.(?:txt|py|json|md|csv|html?|js|css|log|ini|ya?ml)\b",
                goal, re.IGNORECASE)
            if not artifacts:
                return []
            cm = re.search(
                r"containing the text ([\w .-]+?)\s*(?:$|[.,;])|"
                r"with (?:the )?content ([\w .-]+?)\s*(?:$|[.,;])",
                goal, re.IGNORECASE)
            wanted = None
            if cm:
                wanted = (cm.group(1) or cm.group(2) or "").strip().rstrip(".").strip()
            roots = [os.getcwd(), os.path.join(os.getcwd(), "data")]
            for fname in set(artifacts):
                found, content_ok = False, None
                for base in roots:
                    p = os.path.join(base, fname)
                    if os.path.exists(p):
                        found = True
                        if wanted:
                            try:
                                with open(p, "r", encoding="utf-8", errors="replace") as f:
                                    content_ok = wanted.lower() in f.read().lower()
                            except Exception:
                                content_ok = False
                        break
                evidence.append({"artifact": fname, "exists": found,
                                 "content_ok": content_ok})
        except Exception as e:
            log.debug("evidence check error: %s", e)
            return []
        return evidence

    def _handle_error(self, task, step, error):
        """ERROR -> ANALYZE -> RETRY / ALTERNATIVE STRATEGY. Returns True to continue."""
        self.update_status(task, ERROR)
        task.attempts += 1
        task.error = f"{type(error).__name__}: {error}"
        events.emit("task_error", {"id": task.id, "step": step["description"],
                                   "error": task.error, "attempt": task.attempts})
        if task.attempts > MAX_RETRIES:
            log.error("task %s failed permanently: %s", task.id, task.error)
            return False

        self.update_status(task, ANALYZING_ERROR)
        prompt = f"""A step failed. Analyze and propose a fix.
Task: {task.title}
Failed step: {step['description']}
Error: {task.error}
Respond JSON: {{"analysis": "...", "strategy": "retry" | "alternative",
 "new_step": {{"description": "...", "tool": "name or null", "args": {{}}}} or null}}"""
        try:
            raw = llm.chat([{"role": "user", "content": prompt}],
                           task_class=llm.REASONING,
                           options={"num_predict": 250, "temperature": 0.2},
                           json_mode=True)
            data = _extract_json(raw)
            if isinstance(data, list):
                data = data[0] if data else {}
            strategy = data.get("strategy", "retry")
        except Exception:
            strategy, data = "retry", {}

        self.update_status(task, RETRYING)
        if strategy == "alternative" and data.get("new_step"):
            ns = data["new_step"]
            step["description"] = ns.get("description", step["description"])
            step["tool"] = ns.get("tool")
            step["args"] = ns.get("args") or {}
        step["status"] = "pending"
        return True

    # -- Q&A path (fast, no planning) --------------------------------------------

    def _answer_question(self, task, on_delta=None):
        from .agents import select_agent
        from .language_policy import instruction as language_instruction
        from . import config
        agent = select_agent(task.title)
        lang_rule = language_instruction(
            task.title, config.CONFIG.get("language", {}).get("preference", "auto")
        )
        with timing.stage("memory"):
            # The Core Brain assembles ranked, budgeted context. The older inline
            # gathering stays as a fallback so this can never break an answer.
            context = None
            try:
                from . import intelligence
                context = intelligence.assemble_context(task.title,
                                                        task.understanding)
            except Exception as e:
                log.debug("context assembly unavailable: %s", e)
            if context is not None:
                memory_hits = "; ".join(context["memories"]) or "none"
                pairs = list(context["turns"])
            else:
                memory_hits = self._memory_context(task.title)
                convo = get_memory().conversation.recent(8)
                pairs = []
                if _FOLLOWUP.match(task.title):
                    for index in range(len(convo) - 1):
                        turn, reply = convo[index], convo[index + 1]
                        if (turn.get("role") == "user"
                                and reply.get("role") == "assistant"):
                            pairs.append((turn["text"][:200],
                                          reply["text"][:200]))
        messages = [{
            "role": "system",
            # The specialist's own prompt only: the coordination note applies to
            # task execution and is dead weight on a plain question.
            "content": agent.system_prompt + " " + lang_rule + (
                " When the user gives an exact instruction, comply literally. "
                "Never invent facts."
            ),
        }]
        if memory_hits != "none":
            messages.append({"role": "system",
                             "content": f"Relevant memories: {memory_hits}"})
        # Human-aware guidance (tone, detail level, evidence rules) computed by
        # the Core Brain for this exact request.
        from . import communication
        guide = communication.prompt_guidance(
            (task.understanding or {}).get("human"),
            (task.understanding or {}).get("intent"))
        if guide:
            messages.append({"role": "system", "content": guide})
        # Only complete user->assistant pairs become context. Trailing
        # unanswered turns (a crashed request, a bare tool output) used to be
        # fed to the model, which then tried to answer several old questions
        # at once. The current question is appended separately below.
        # Conversation context is only useful for genuine follow-ups. For a
        # standalone question it added prompt tokens (slow on CPU) and pulled
        # the answer towards the style/language of older replies.
        for user_text, reply_text in pairs[-2:]:
            messages.append({"role": "user", "content": user_text})
            messages.append({"role": "assistant", "content": reply_text})
        messages.append({"role": "user", "content": task.title})

        parts = []
        try:
            # Stream: the first words reach the user in ~1s instead of after
            # the whole (CPU-bound) generation finishes.
            for piece in llm.chat_stream(messages, task_class=agent.task_class,
                                         options={"num_predict": 200}):
                parts.append(piece)
                if on_delta is not None:
                    on_delta(piece)
            answer = "".join(parts).strip()
            if not answer:
                raise llm.ProviderError("empty response from the model")
        except Exception as e:
            if parts:
                # Keep what was already delivered; do not replace it with an
                # error after the user has started reading.
                answer = "".join(parts).strip()
                log.warning("answer stream ended early: %s", e)
            else:
                answer = (f"I could not reach the AI model right now ({e}). "
                          "Check that Ollama is running or configure an online model.")
        task.result = answer
        # Conversation history is recorded by Brain.handle_input (one user turn,
        # one assistant turn per request) so every path behaves identically and
        # nothing is stored twice.
        return answer


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = TaskEngine()
    return _engine

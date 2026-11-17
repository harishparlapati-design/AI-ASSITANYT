# Harry — Architecture

Harry is a modular, offline-first personal AI **agent** for Windows. This
document describes what is actually implemented in `harry_core/`, how the
pieces fit, and where the extension points are.

## 1. Layers

```
          text · voice · dashboard · WebXR view
                          │
                   harry_core.brain            orchestrator
                          │
                   harry_core.task_engine      lifecycle + persistence
                    ┌─────┴──────┐
             harry_core.agents   harry_core.tools
             (9 specialist       (38 builtin + legacy wrappers)
              roles)                    │
                          ┌─────────────┴─────────────┐
                harry_core.builtin_tools    tools/ (legacy, auto-wrapped)
                          │
   llm · memory · security · speech · scheduler · diagnostics · documents
   research · math_tools · logging_setup · events · runtime_paths · config · ui
```

Each module is independently importable (`tests/import_check.py` proves it) and
has one job. Optional heavy dependencies (vosk, pyttsx3, sounddevice, pyautogui,
torch) are imported lazily, so a missing one degrades a feature instead of
breaking startup.

| Module | Responsibility |
|---|---|
| `config` | Defaults + `config.example.json` + `config.json` + `HARRY_*` env overrides. Secrets only referenced by env-var *name*. |
| `runtime_paths` | Source vs frozen (EXE) path resolution; writable home, bundled assets, `find_asset`. |
| `logging_setup` | Rotating log + `audit.jsonl` with secret scrubbing. |
| `events` | Process-wide event bus and the emergency-stop flag. |
| `llm` | Provider abstraction + model routing (chat / reasoning / coding / vision, local Ollama, optional OpenAI-compatible). |
| `memory` | Working, conversation and long-term layers, all persisted as plain files. |
| `security` | SAFE / CONTROLLED / SENSITIVE classification, confirmation flow, path policy. |
| `tools` | `Tool` / `ToolRegistry`, permission gate, LLM catalog, legacy tool wrapping. |
| `builtin_tools` | The actual tool implementations (files, terminal, apps, web, screen, vision, tasks, reminders, math). |
| `agents` | Nine specialist roles chosen deterministically by keyword scoring. |
| `task_engine` | The task lifecycle, planning, execution, verification, retry/replan, persistence. |
| `scheduler` | Proactive reminders, daily briefing, unfinished-task nudges. |
| `speech` | Vosk STT, pyttsx3 TTS, wake word, Telugu/Hindi/English detection, reply styling. |
| `documents`, `research`, `math_tools` | Document reading/summarising, sourced web research, sympy-backed math & engineering. |
| `diagnostics` | The 19-point health report behind `--check` and "system check". |
| `brain` | Fast paths, memory commands, emergency commands, delegation to the engine, single-delivery output. |
| `ui` | Flask dashboard, SSE stream, request idempotency, `/vr` WebXR view. |

## 2. Request flow

1. **Input** — `Brain.handle_input(text, source=...)` from the console, voice
   loop, dashboard (`/api/chat`) or the VR page. Every transport shares this one
   entry point, so answers are identical regardless of channel.
2. **Safety and control** — "emergency stop" / "stop" / "cancel" are matched
   before anything else and set the global stop flag; speech queue is cleared.
3. **Fast paths** — greetings, time/date and pure arithmetic are answered
   locally (no model call), which keeps simple commands instant.
4. **Memory commands** — "remember that …", "forget …", "what do you remember".
5. **Task pipeline** — everything else goes to the engine.
6. **Output** — one `assistant_message` event (unique id + request id) plus
   speech according to `voice.output_mode` (`text` / `voice` / `both`).

## 3. Task lifecycle

```
NEW → UNDERSTANDING → PLANNING → (WAITING_FOR_PERMISSION) → EXECUTING → VERIFYING → COMPLETED
                                 ↘ ERROR → ANALYZING_ERROR → RETRYING → EXECUTING …
```

- **UNDERSTANDING** decides *question* vs *task*. A rule-first pass avoids a
  model round-trip for unambiguous questions; ambiguous requests ask the
  routing model, with a rule-based fallback when offline.
- **PLANNING** asks the reasoning model for 1–6 steps, each with an optional
  tool hint and args. Offline it falls back to a single generic step.
- **EXECUTING** runs a direct tool call when the step names one; otherwise the
  model chooses one tool call for that step. Completed steps are never repeated.
- **VERIFYING** prefers deterministic evidence: if the goal names a file
  artifact, the filesystem is checked for existence and (when the goal states
  content) for that content. Only if there is no such artifact does the model
  judge the step results. Verification failure triggers **re-planning** (up to
  `MAX_PLAN_RETRIES`), not an immediate failure.
- **Errors** are analysed by the model, which returns `retry` or an
  `alternative` step; attempts are capped at `MAX_RETRIES`, then the task fails
  with the real error text.
- **Persistence** — every state change is written atomically to
  `data/tasks.json`, so an interrupted task can be resumed and the dashboard can
  show history. A client-supplied `request_id` makes task creation idempotent.

## 4. Memory

| File | Layer | Contents |
|---|---|---|
| `data/working_memory.json` | working | current task scratchpad |
| `data/conversation.jsonl` | conversation | rolling turns (trimmed at 400) |
| `data/longterm.json` | long-term | facts, preferences, projects, procedures — tagged, importance-ranked, deduplicated, recall-counted |
| `data/tasks.json` | task/episodic | lifecycle records incl. step results |

Retrieval is keyword/token-overlap scoring — no external service, so it works
offline. "remember"/"forget" map to store/forget; relevant memories are injected
into planning and answers. Nothing personal is hard-coded in source.

## 5. Models

`harry_core/llm.py` exposes one `chat(messages, task_class=..., json_mode=...)`
entry point and routes by class:

| Class | Used for | Default model |
|---|---|---|
| `ROUTING` | question/task classification | `llama3.1` (fast) |
| `CHAT` | conversation and Q&A | `llama3.1` |
| `REASONING` | planning, verification, error analysis | `qwen3:4b` |
| `CODING` | coding specialist | prefers an installed coder model |
| `VISION` | screenshot understanding | `vision_model` when configured |

Local (Ollama) is the default; an online OpenAI-compatible provider is optional
and only enabled by configuration, with the key read from the environment
variable named in `models.online.api_key_env`. JSON calls run with thinking
disabled where the model supports it.

## 6. Tools and skills

38 built-in tools are registered in `builtin_tools.register_builtins`:
file read/write/edit/search/tree/delete, terminal, run_python, open/close app,
process list, system info, clipboard, window list, computer control,
screenshot, understand_screen, vision_analyze, web_search, fetch_page,
download_file, deep_research, read/summarize document, math, memory
store/search/forget, task create/list, reminder, browser_open, and legacy
wrappers for everything in `tools/`.

Adding a tool is one call:

```python
register("my_tool", "What it does", my_func, danger=CONTROLLED,
         schema={"arg": "str"})
```

Adding a *skill* is a specialist role in `harry_core/agents.py`: a system
prompt, a model class and preferred tools. The brain stays the single
coordinator, so specialists never compete for control.

## 7. Security

Every tool call passes `PermissionManager.check()` before it runs:

- **SAFE** (read/search/analyze) — allowed, audited.
- **CONTROLLED** (write/edit/run/install/open) — allowed, audited loudly.
- **SENSITIVE** (delete/shutdown/send/upload/credentials/financial) — requires
  explicit confirmation (dashboard approval card or console prompt).

`allowed_dirs` can restrict file operations to specific roots. The emergency
stop is checked at every step boundary, and PyAutoGUI's failsafe is on. Audit
entries never contain secrets — `logging_setup` scrubs key/token/password
patterns.

## 8. Interfaces

- **Console** — `python main.py --text`.
- **Dashboard** — `http://127.0.0.1:8765`: conversation, tasks, tool list,
  memory stats, model status, output mode, approval cards, emergency stop,
  browser or local-Vosk voice input.
- **WebXR (`/vr`)** — a real WebXR client: it requests an `immersive-vr`
  session with `dom-overlay` when a headset runtime is available, and otherwise
  states plainly that no immersive device is connected while still working as a
  large-format dashboard. It uses the same `/api/chat`, `/api/stream` and
  `/api/stop` endpoints, so it is a second view of one assistant. A 3D avatar
  and 3D scene rendering are **not** implemented.
- **Remote/mobile** — non-loopback clients are refused unless
  `ui.remote.enabled` is true and they present the token from the environment
  variable named in `ui.remote.token_env`. That is the API seam an Android
  client would use (see `docs/ANDROID.md`).

## 9. Offline vs online

Offline by default: local LLM, Vosk STT, pyttsx3 TTS, memory, files, terminal,
apps, math, documents, task engine, dashboard, scheduler, WebXR page. Online
only, with clear failures: web search, page fetch, downloads, online models.
Losing connectivity never disables local capability.

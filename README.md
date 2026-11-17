# Harry — Super Autonomous Personal AI Assistant Agent

Harry is a modular, offline-first personal AI **agent** (not a chatbot) for your PC. You give it goals by text or voice — in English, Telugu, Hindi, or a natural mix — and it understands the intent, plans the work, executes it with real tools, verifies the result, self-corrects when needed, and reports what it did.

```
USER (voice / text / UI)
        │
   harry_core.brain          ← orchestrator (emergency stop, memory cmds, routing)
        │
   harry_core.task_engine    ← UNDERSTANDING → PLANNING → EXECUTING → VERIFYING → COMPLETED
        │            │                (errors → analyze → retry / alternative strategy)
        │            └── harry_core.tools (38 tools) + legacy tools/ (auto-wrapped)
        │
   harry_core.llm            ← model routing: fast chat / reasoning / coding / vision
   harry_core.memory         ← working + conversation + long-term (persistent)
   harry_core.security       ← SAFE / CONTROLLED / SENSITIVE + confirmations + audit log
   harry_core.speech         ← STT (Vosk), TTS (pyttsx3), wake word, Telugu/Hindi/English
   harry_core.scheduler      ← reminders, daily briefing, unfinished-task nudges
   harry_core.ui             ← localhost dashboard (chat, tasks, approvals, emergency stop)
```

## What Harry can actually do (verified)

- **Autonomous tasks** — "create a file named X in data containing Y" runs end-to-end: goal → plan → `write_file` → deterministic verification (filesystem evidence) → completion report. If verification fails, it re-plans (self-correction) instead of failing silently.
- **Questions & chat** — fast local model path, conversation memory, mixed-language replies that mirror your style.
- **38 built-in tools** — files (read/write/edit/search/tree/delete), terminal, run_python, open/close apps, processes, system info, web search, fetch pages, downloads, memory store/search/forget, screenshot, mouse/keyboard control, clipboard, windows, vision/OCR, tasks, reminders.
- **Your old tools keep working** — everything in `tools/` (battery, weather, calculator, research, shutdown, automation) is auto-wrapped with permission gating.
- **Multilingual voice** — automatic Telugu/English/Hindi detection (script + romanized), replies mirror your mix; `style_reply_hint()` feeds the model so spoken-style answers match. Offline STT (Vosk) + offline TTS (pyttsx3).
- **Security** — every tool call passes SAFE/CONTROLLED/SENSITIVE gating; sensitive actions require your approval (UI button or console); full audit trail in `logs/audit.jsonl` with secret scrubbing.
- **Proactive** — reminders, daily briefing, unfinished-task nudges — all opt-out via config.
- **Diagnostics** — `python main.py --check` gives a 19-point health report.
- **Immersive view** — `/vr` is a real WebXR client: it requests an `immersive-vr` session with DOM overlay when a headset runtime is present, and works as a large-format dashboard when none is connected. The same assistant, one set of endpoints.

## Windows EXE (first distribution deliverable)

Harry builds to a **portable, self-contained Windows app** — no installer, no admin rights:

```bash
pip install -r requirements.txt pyinstaller
python build_windows.py     # -> dist/harry_dist/Harry.exe (+ your local vosk model copied in)
```

Double-click `Harry.exe` (or `--text / --voice / --ui / --check`). Config, data and logs
live in `%LOCALAPPDATA%\Harry` and survive updates. Full build, install, update and
uninstall instructions: [BUILD_INSTALL.md](BUILD_INSTALL.md).

## Requirements

- Windows 10/11 (tested), Python 3.10+
- [Ollama](https://ollama.com) running locally (`ollama serve`) with at least one model
- Microphone + speakers (voice mode only)

```bash
pip install -r requirements.txt
```

Already-installed heavy deps (torch, vosk, pvporcupine, Resemblyzer) are listed but the core degrades gracefully if any optional one is missing.

## Configuration

Copy `config.example.json` → `config.json` and edit (it is git-ignored). Precedence: built-in defaults < `config.example.json` < `config.json` < `HARRY_*` env vars (`HARRY_MODELS__LOCAL__URL=http://...`).

```json
{
  "user_name": "Hareesh",
  "models": {
    "local": {
      "chat_model": "llama3.1:latest",
      "reasoning_model": "qwen3:4b",
      "routing_model": "llama3.1:latest"
    },
    "online": { "type": "none" }
  },
  "voice": {
    "wake_word_mode": "text",
    "output_mode": "both",
    "verify_voice": false
  },
  "security": { "require_confirmation_for": "sensitive" },
  "proactive": { "enabled": true, "briefing_hour": 8 }
}
```

**Models.** Harry routes by task class: fast `chat_model` for conversation, `reasoning_model` for planning/verification (thinking models OK — the provider disables thinking for JSON calls and retries without thinking when budgets are exhausted), `coding_model` selection auto-prefers installed coder models, `vision_model` for image understanding (e.g. `llava`). Online providers are optional and only used if configured with an env-var **name** (`api_key_env`), never a literal key.

**Voice.** `wake_word_mode`: `text` (STT-based, free) | `porcupine` (real keyword detection using your existing `hay-harry_en_windows_v4_0_0.ppn` — needs a free Picovoice access key in the env var named by `porcupine_access_key_env`) | `off`. `verify_voice: true` re-enables your trained Resemblyzer voiceprint gate (`hareesh_voice_profile.pkl`). Telugu/Hindi TTS voices are used automatically when the OS has them; otherwise Telugu text is displayed and spoken with the default voice (graceful fallback).

## Running

```bash
python main.py            # text chat + web UI + scheduler   (recommended)
python main.py --voice    # + hands-free voice loop (wake word, continuous convo)
Harry.exe                 # packaged build (see WIN EXE section and BUILD_INSTALL.md)
python main.py --text     # text chat only
python main.py --ui       # dashboard only
python main.py --check    # system health report
python harry.py           # legacy entry: wake word → voice verify → voice commands
```

UI: http://127.0.0.1:8765 — chat, task board, tool list, memory stats, model status, output-mode switching (text / voice / both), permission approval cards, and a red **EMERGENCY STOP** that halts all loops and speech instantly.

**Browser voice input.** The 🎤 button uses the browser's built-in speech recognition (Chrome/Edge). If a browser has none, or blocks the speech service, Harry falls back to its **local Vosk model**: the page records 16 kHz PCM and posts it to `/api/stt`, which transcribes on this machine — no third-party service. Recognised text lands in the input box (auto-send is on unless you uncheck it), so you can review it first; ⏹ stops listening.

**One request, one answer.** Each submit carries a request id. `POST /api/chat` accepts it immediately (202) and the answer arrives once over the event stream; the page de-duplicates by that id, so a refresh, a slow network or a double click cannot render or run the same request twice. Greetings, time/date and arithmetic are answered locally in a few milliseconds without touching the model, and every request logs its stage timings (classification, model call, verification) with a request id in `logs/harry.log`.

## Memory

Stored under `data/` as plain JSON/JSONL (portable, inspectable, no DB):

| File | Layer |
|---|---|
| `data/working_memory.json` | current session/task scratchpad (survives restarts → task resume) |
| `data/conversation.jsonl` | rolling conversation turns |
| `data/longterm.json` | facts, preferences, projects — tagged, importance-ranked, deduplicated, searchable |
| `data/tasks.json` | full task lifecycle records (resume + audit) |

"remember that …" stores; "forget …" removes; "what do you remember" lists. Relevant memories are injected into answers and plans automatically.

## Security model

| Level | Examples | Behavior |
|---|---|---|
| SAFE | read, search, analyze, memory, screenshots of state | auto-allowed, audited |
| CONTROLLED | write/edit files, terminal, run code, open apps, mouse/keyboard | allowed, audited loudly |
| SENSITIVE | delete, shutdown, git push, send/upload, credentials | **requires your confirmation** |

Every tool call and permission decision is written to `logs/audit.jsonl` (API keys, tokens, passwords are scrubbed). `PyAutoGUI` failsafe is on (slam mouse to a corner to abort computer control). No secrets are stored in code or config; only env-var names are referenced.

## Offline vs online

Offline (default, fully functional): local LLM via Ollama, Vosk STT, pyttsx3 TTS, memory, all file/terminal/app tools, task engine, UI, scheduler. Online-only (fail gracefully with clear messages): web search, page fetch, downloads, online models. Losing internet never breaks local capability.

## Project layout

```
harry_core/        config, logging/audit, events+emergency stop, llm router,
                   memory, security, tools, builtin_tools, task_engine,
                   scheduler, speech, brain, diagnostics, ui
tools/             legacy keyword tools (auto-loaded, permission-gated)
tests/smoke_test.py  41 checks: tools, memory, language, security, scheduler,
                   live LLM Q&A, full autonomous task, diagnostics
main.py harry.py harry_brain.py wake_word.py   entry points (old names preserved)
```

`harry_brain.py` keeps the old API (`ask_harry`, `detect_intent`, `save_memory`, `search_web`, `plan_task`, `create_tool`, `suggest_upgrade`) on the new core, so any of your own scripts keep working.

## Troubleshooting

- **"no AI provider available"** — start Ollama: `ollama serve`, then `ollama pull llama3.1` / `ollama pull qwen3:4b`.
- **Slow first answers** — models load on first use; `keep_alive=30m` keeps them warm.
- **Thinking model oddities** — the provider auto-disables thinking for JSON calls; if a reply looks like leaked reasoning, it means content came back empty twice (check `logs/harry.log`).
- **Voice not recognized** — run `python main.py --check`; confirm the Vosk model dir `vosk-model-en-us-0.22` exists in the project root.
- **Telugu/Hindi not spoken** — install a Windows TTS voice for that language (Settings → Time & Language → Speech); text output always works regardless.
- **UI port busy** — set `HARRY_UI__PORT=8766` or edit `config.json`.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — layers, task lifecycle, memory, models, security, interfaces.
- [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md) — what was actually run, with results and explicit non-tests.
- [docs/ANDROID.md](docs/ANDROID.md) — Android readiness: what already supports a phone client, what does not.
- [BUILD_INSTALL.md](BUILD_INSTALL.md) — EXE build, install, update, uninstall.

## Roadmap (what is genuinely not done)

- **Browser automation agent** — fetch, search, download and open-page exist; scripted click/type flows do not (Playwright integration point: `harry_core/builtin_tools.py`).
- **Coding agent workflows** — the coding specialist has inspect/edit/test/verify tools and a prompt, but there is no multi-repo orchestration loop yet.
- **Vision** — screenshot capture, screen reading and OCR are wired; semantic understanding needs a vision model configured (`vision_model`, e.g. `ollama pull llava`). Without one, `vision_analyze` reports that clearly.
- **Multi-device sync** — a token-guarded remote API exists for LAN/mobile clients, but there is no background sync or conflict resolution between devices. See [docs/ANDROID.md](docs/ANDROID.md).
- **VR** — `/vr` is a real WebXR front-end for the dashboard (session request, DOM overlay, same API). A 3D avatar, 3D scene and hand tracking are **not** implemented and are not claimed.
- **Installer** — the Inno Setup script is written; Inno Setup was not installed on the build machine, so `HarrySetup.exe` has not been produced (see BUILD_INSTALL.md §5).

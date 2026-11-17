# Test results

Date: 2026-11-17 · Machine: Windows 10/11, Python 3.10.11 (`.venv`), Ollama local
(`llama3.1`, `qwen3:4b`, `qwen2.5-coder:3b`).

Every result below is from a command that was actually run. Anything not run is
listed in §4 as **not tested** — it is not reported as a pass.

## 1. Commands and results

| # | Command | Result |
|---|---|---|
| 1 | `python -m compileall -q harry_core main.py build_windows.py tests` | **pass** (exit 0, no syntax errors) |
| 2 | `python tests/import_check.py` | **pass** — `checked 19 modules / result: ok` |
| 3 | `python tests/smoke_test.py` | **41 passed, 0 failed, 0 skipped** |
| 4 | `python tests/dashboard_test.py` | **16 passed, 0 failed** |
| 5 | `python main.py --check` | **18/19 healthy** (only "online AI provider — not configured", which is optional and intentionally empty) |
| 6 | `python build_windows.py` | **pass** — PyInstaller 6.22.3, onedir build at `dist/harry_dist/`, ~3003 MB incl. the copied Vosk model |
| 7 | `dist/harry_dist/Harry.exe --check` | **pass** — ran in frozen mode, home = `%LOCALAPPDATA%\Harry`, 38 tools, Vosk model found next to the EXE, 18/19 healthy |
| 8 | `python main.py --ui` + HTTP | `GET /` **200**, `GET /vr` **200** (WebXR client served), `GET /api/state` **200** with 9 agents and task list |
| 9 | `python main.py --ui` + `GET /vr` (before fix) | **500 — `NameError: name '_VR_HTML' is not defined`**; root cause: the VR page string was never written. Fixed by implementing the WebXR client; re-tested → 200 |

## 2. What the suites cover

`tests/smoke_test.py` (41 checks): config merge, event bus, emergency-stop flag,
memory store/search/dedupe/forget/conversation, Telugu/Hindi/English detection
(script and romanized), reply-style hint, permission classification (SAFE /
CONTROLLED / SENSITIVE) and the confirmation requirement, tool registry (builtin
+ legacy present, 38 tools), real file write/read/search/edit/delete, system
info, `run_python`, terminal, memory tools, unknown-tool error path, legacy
battery + calculator tools, scheduler reminder delivery, Ollama reachability,
question/task classification, a full Q&A through `Brain.handle_input`, a full
autonomous task (goal → plan → `write_file` → deterministic filesystem
verification → COMPLETED, file content checked), the diagnostics report, and
task persistence.

`tests/dashboard_test.py` (16 checks, fake LLM + fake speaker — no hardware):
local fast paths (greeting, time, arithmetic) answered in <200 ms with exactly
one answer and one speech call, one model call per question, request-id
idempotency, duplicate-submit suppression, one conversation turn per request,
async `/api/chat` returning 202 then reporting once, unknown request → 404,
empty text → 400, unique task ids, emergency-stop endpoint, `/api/stt`
transcription path, `/api/stt/warm` idempotency, and voice/typed input sharing
one pipeline.

## 3. Fixes made during this campaign

- `/vr` returned 500 because `_VR_HTML` was referenced but never defined. The
  page was implemented as a real WebXR client (session request with
  `dom-overlay`, honest fallback text on devices without a headset), and its
  approval buttons now post to `/api/confirm/<id>` like the desktop page.
- `tests/smoke_test.py` previously counted live-model checks as failures on a
  machine without Ollama. They now report **SKIP** when no local model exists
  (never PASS), so CI is meaningful on runners without a model. On this machine
  all of them ran and passed.

## 4. NOT tested (do not assume these work)

| Area | Why |
|---|---|
| Microphone capture end-to-end (`--voice`, wake word, speaker verification) | No human was present to speak; automated audio playback/recording on this machine would not prove recognition quality. Vosk model loading and `/api/stt` transcription path are exercised, but a real voice command was not. |
| TTS audibility / voice quality (English, Telugu, Hindi) | pyttsx3 initialises and lists an English voice (diagnostics OK); no one listened to output. Telugu TTS voices are not installed on this machine — the documented fallback (show text, speak with the default voice) applies. |
| WebXR immersive session | No headset or WebXR runtime is connected. The page's support detection and fallback message are in place; `immersive-vr` was never entered. |
| Screen understanding with a vision model | No vision model is pulled (`vision_model` empty). Capture/OCR paths exist; semantic understanding is unverified. |
| Inno Setup installer (`HarrySetup.exe`) | Inno Setup 6 is not installed on this machine (`C:\Program Files (x86)\Inno Setup 6\ISCC.exe` absent). The script `installer/harry.iss` is written but has never been compiled. |
| GitHub Actions workflow | `.github/workflows/ci.yml` is authored but has not run on GitHub (no push/CI run at authoring time). Local equivalents (steps 1–5) passed. |
| Android APK | Not built. See `docs/ANDROID.md`; no APK is claimed. |
| Multi-device sync / remote LAN access | The token-guarded remote guard exists, but no second device was connected; behaviour from a non-loopback address was not exercised. |

## 5. Known limitations

- Planning, verification and error analysis rely on the local model; on very
  weak hardware those stages are slow (a multi-step task can take ~1 minute on
  CPU). Fast paths and tools do not depend on the model.
- `verify_voice` (speaker verification) is excluded from the EXE build
  (torch/scipy weight); run from source to use it. Documented in BUILD_INSTALL.md.
- The dashboard is a Flask development server; it is bound to loopback by
  default and is not hardened for public exposure.

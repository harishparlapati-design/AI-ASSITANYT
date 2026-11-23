# Harry AI — feature audit and verification log

Audited and extended on the `dev` branch, 23 November 2026.
Every "tested" line below names the suite that proves it on this machine.
Anything not tested is marked as such.

## Summary

| Area | Before | Now | Evidence |
| --- | --- | --- | --- |
| Conversation | streamed, but duplicated text and a speech/mic race | single-delivery SSE, speech gated by a pending counter | `tests/live_e2e_test.py` 10/10, `tests/conversation_test.py` 16/16 |
| Routing | model classification (5–18 s) that misrouted code requests into the planner | deterministic classification, model fallback when a model refuses | manual repro, `tests/language_policy_test.py` 30/30 |
| Documents | read/summarise only | seven real formats, reopen-validated | `tests/docgen_test.py` 74/74 |
| Screen + vision | `pyautogui` screenshot only, OCR missing | browser capture session, local moondream vision, OCR fallback, privacy gate | `tests/vision_test.py` 39/39 |
| Images | none | real sd-turbo generation on CPU, provider adapters, history, regenerate, delete | `tests/media_test.py` 44/44 + a real 512 px run |
| Video | none | provider adapters (Replicate/fal/ComfyUI) with jobs; **no provider configured, so no video generated** | adapter contracts tested against a local stand-in server |
| PWA | none | manifest, service worker, icons, install prompt, offline chip | `tests/pwa_test.py` 37/37 |
| Hosting | localhost only | login, sessions, CSRF, per-account folders, Docker/Fly/Caddy config | `tests/auth_test.py` 46/46 |
| Security | confirmation gates only | traversal/SSRF/IDOR/CSRF/upload/leak tests | `tests/security_test.py` 51/51 |
| Windows | EXE build script | rebuild without the torch stack, launch check | see "Windows" below |

## Bugs found and fixed

1. **Duplicated streamed text.** `main.py --ui` created two Flask apps, so the
   event bus delivered every token twice. Fixed with a single app instance,
   `events.subscribe_once`, monotonic SSE ids and client-side dedupe.
2. **Microphone captured Harry's own speech.** The listener opened while TTS was
   still playing. Speech waiting is now governed by a pending-item counter.
3. **CLI scripts printed Python source instead of answers.** *Observed during
   this audit*: a command that printed a function definition rather than its
   result was a caller bug, not Harry's; no code change was needed.
4. **Vision capability probe hammered Ollama.** `vision.status()` ran an HTTP
   request on every call, which pushed screen frames outside the rate-limit
   window and slowed the dashboard. Now cached for 10 s.
5. **A half-downloaded diffusion model was reported ready.** Readiness now
   requires real weights in `unet`, `vae` and `text_encoder`.
6. **Deleting a media file could silently fail** (open handle on Windows) while
   the API said "deleted". Deletion now reports the real failure and keeps the
   record.
7. **`think` was sent to models without thinking support**, producing
   `HTTP Error 400` and surfacing "I could not reach the AI model" for an
   ordinary question. Capabilities are now queried and cached, the provider's
   error body is preserved, and a failing model falls back to the chat model.
8. **Code requests were routed into the task planner**, which wrote stray files,
   spent 160 s and answered "I couldn't complete that." Classification is now
   deterministic: content and code requests are answered in chat.
9. **SSRF escape hatch in downloads.** Provider-returned URLs are refused when
   they resolve to loopback/private/metadata addresses unless an explicit local
   flag is set.

## Phase-by-phase status

### Phase 1 — audit
Done: module inventory (25 packages), route inventory, dependency check,
test inventory, secrets scan. Findings are the bug list above.

### Phase 2 — intelligence
Context assembly keeps only complete user→assistant pairs; memory retrieval and
language policy are applied per turn; model routing distinguishes chat,
reasoning, coding, routing and vision. Classification no longer costs a model
call. Verified by `language_policy_test` (30), `integrations_test` (50) and the
live E2E run.

### Phase 3 — voice
Continuous mode, push-to-talk, VAD, barge-in, mute, stop, reconnect and states
are covered by `conversation_test` (16). Vosk cold load (~68 s) runs in the
background and speech turns never wait for it. **Not verified with human
speech**: the assistant cannot speak into the microphone. The default input
device on this machine is a virtual AudioRelay microphone, so the physical
array must be selected before a human test.

### Phase 4 — screen and vision
Real `getDisplayMedia` capture in the dashboard, gated server-side by an
explicit session; frames refused when inactive or paused, rate-limited on
arrival, analysed in memory only, cleared on stop. Local moondream reads text
and describes the screen (8.8 s warm), with a hosted adapter available. Verified
by `vision_test` (39) and a manual frame ("INVOICE 4821" was read correctly).

### Phase 5 — image generation
sd-turbo runs locally on CPU through diffusers: 74.5 s cold (model load), 28.2 s
warm for 512 px / 2 steps. Adapters also cover Automatic1111, ComfyUI and an
OpenAI-compatible images endpoint. History, regenerate, delete, cancel, size and
seed controls are implemented; storage is validated as real PNG/JPEG/WebP.
Verified by `media_test` (44, including one real diffusion run) and a viewed
output image.

### Phase 6 — video generation
Adapters, async jobs, progress, cancellation, download, playback and history are
implemented for Replicate, fal and ComfyUI, with an SSRF guard on result URLs.
**No video has been generated**: no provider key is configured and no local CPU
video model is practical. The adapter contracts are tested against a local
stand-in server; the dashboard shows "not configured" with the exact env vars.

### Phase 7 — documents
PDF, DOCX, XLSX, PPTX, CSV, TXT and MD are written from text, a structure or a
natural-language brief; every file is reopened with the library that wrote it
(pypdf, python-docx, openpyxl, python-pptx) before being listed. Spreadsheets
keep numeric cells and real formulas. Path traversal, unknown formats,
oversized content and cross-account access are refused. `docgen_test` 74/74.

### Phase 8 — autonomous tasks
Plan → execute → verify → replan → complete already existed; this audit fixed
the routing that fed it inappropriate work, integrated document/media/job
tools, and kept step-level retry, cancellation and duplicate prevention.

### Phase 9 — hosting
Hosted mode adds accounts (scrypt, lockout), signed sessions, CSRF on every
mutation, CSP/HSTS/nosniff/frame-denial headers, `/healthz`, and per-account
folders for documents, media and jobs. Docker, compose+Caddy, Fly.io and an
environment template are included. **Not deployed**: no provider account or
payment was authorised. No public URL exists yet.

### Phase 10 — website
The authenticated dashboard is the website. It now also carries document
generation, image/video generation, screen controls, install prompt, offline
state and job progress. Verified by dashboard (16), PWA (37) and auth (46).

### Phase 11 — Android
Installable PWA verified by `pwa_test` (37): manifest fields, icons resolving at
192/512/maskable/180, service worker that never caches `/api`, offline fallback,
install prompt wiring. Capacitor configuration and build instructions live in
`mobile/`. **No APK was built** (no JDK/Android SDK here) and **no physical
device test was performed**.

### Phase 12 — Windows
`build_windows.py` produces an onedir `Harry.exe`. The diffusion/torch stack is
excluded so the installer stays small. See the build log for the exact result on
this machine; the EXE is launched after building to confirm it starts.

### Phase 13 — dashboard
Dark/light themes, responsive layout, orb states, voice visualization, task
list, plugin status, document and media previews, screen indicator, error
messages. Every control calls a real endpoint; development-only panels (GitHub,
agents) are refused in hosted mode.

### Phase 14 — security
`security_test` (51) covers remote-access gating, traversal, IDOR, CSRF, upload
limits, SQL-injection attempts, header hardening, secret-shaped strings in
tracked files and debug-surface removal. Remaining risks are listed at the end.

### Phase 15 — performance
Measured on this CPU-only machine (see `docs/PERFORMANCE.md`): first visible text
≈ 5.1 s, median completion ≈ 24 s for normal questions, documents well under a
second, vision 8.8 s warm, images 28 s warm. These miss the 2–10 s target on
local CPU; a hosted inference provider is the fix, not prompt trimming.

### Phase 16 — testing
Twelve suites, all green as of the last run: 74 + 51 + 50 + 46 + 44 + 39 + 37 +
30 + 16 + 16 + 10 + import check.

## Remaining risks and gaps

- Human speech with the physical microphone is untested.
- No hosted deployment, no public URL, no APK, no installer (Inno Setup absent).
- Video generation has no provider; image generation has no hosted provider.
- Multi-process scaling is unsupported (in-process job table and SSE fan-out).
- Memory and task history are shared per instance, not per account; only files
  and jobs are tenant-scoped. Do not create two accounts on one instance if that
  matters.
- `pip-audit` was not run; dependency CVEs are unverified.

# Harry on Android — readiness plan

**No APK is built or claimed.** This document records the parts of the current
architecture that already support a mobile client, what would have to be added,
and the concrete packaging options. Every item marked *exists* is in the code
today; everything else is planned work.

## 1. What already exists (verified)

| Capability | Where | Notes for mobile |
|---|---|---|
| HTTP JSON API | `harry_core/ui.py` | `/api/chat`, `/api/request/<id>`, `/api/state`, `/api/stt`, `/api/stop`, `/api/confirm/<id>` |
| Streaming state | `GET /api/stream` | Server-sent events: `assistant_message`, `task_status`, `permission_request`, … |
| Request idempotency | `/api/chat` | A client-supplied `request_id` runs once; resubmits return the same entry |
| Remote access control | `config.ui.remote` | Non-loopback clients refused unless enabled **and** they present the token from the env var named in `ui.remote.token_env` |
| Voice upload endpoint | `POST /api/stt` | Accepts PCM16 mono 16 kHz raw bytes (max 5 MB) or a WAV container, transcribes locally with Vosk |
| Text output | all transports | The answer is returned as JSON and as an SSE event with the same request id |
| Persistent state | `data/*.json` | Plain files with atomic writes; safe to sync |

That is enough to build a thin Android client **today**: it can talk, stream
answers, upload recorded audio and show tasks without any server rewrite.

## 2. What is missing

1. **Authentication suitable for the internet.** The current token is a shared
   secret for LAN use. A phone that leaves the home network needs per-device
   credentials, TLS, and token rotation.
2. **A stable API contract.** Endpoints work but are shaped for the dashboard.
   A versioned `/api/v1/*` surface should be added before two clients depend on
   it.
3. **Sync model.** Memory/tasks are single-writer files. Two devices writing at
   once need either a server-owned store with revisions, or a designated
   "primary" device.
4. **Push/nudges.** Reminders are delivered by the laptop's scheduler; a phone
   needs push or local scheduling.
5. **Battery/keep-awake policy** for continuous wake-word listening on a phone.

## 3. Recommended architecture

```
Android app (Kotlin/Compose)        Harry server (current Python core)
  ├─ Chat UI, task list, approvals   ├─ /api/v1/* (versioned, token auth)
  ├─ Android TextToSpeech        ──► ├─ Task engine, tools, memory
  ├─ AudioRecord 16 kHz PCM      ──► ├─ POST /api/v1/stt (existing /api/stt)
  ├─ SpeechRecognizer (offline)  ──► └─ SSE /api/v1/stream
  └─ WorkManager reminders
```

- **Transport:** HTTPS via a reverse proxy (Caddy/nginx) on the laptop, or the
  same API deployed to a small always-on host. The phone must never rely on the
  laptop being awake for *offline-safe* actions; those degrade to local Android
  capabilities (notes, timers, TTS).
- **Auth:** per-device bearer tokens issued once by pairing (a QR code
  containing one short-lived pairing token, exchanged for a device token). The
  server keeps only hashes. This replaces the single shared token for remote
  use; LAN loopback behaviour for the desktop stays as it is.
- **Voice:** two paths, both already compatible with the server — Android
  `SpeechRecognizer` for instant on-device transcription, or stream PCM to
  `/api/stt` for the same Vosk model the desktop uses. Reply audio comes from
  Android `TextToSpeech` (Telugu/Hindi voices when the device has them) or from
  the server's TTS rendered to a file.
- **Language:** mixed Telugu/Hindi/English handling lives on the server
  (`speech.detect_language`, reply styling), so the client sends text as-is.
- **Offline:** the phone keeps its own conversation cache, reminders and drafts;
  the server remains the source of truth for tools and long-term memory.

## 4. Packaging options (honest trade-offs)

| Option | Verdict |
|---|---|
| Native Kotlin client + existing Python server | **Recommended.** Best voice/TTS integration, normal Play-Store tooling, no Python runtime on the phone. |
| Kivy / BeeWare (Python on Android) | Reuses `harry_core` directly, but bundling vosk/pyttsx3/torch and native wheels is heavy and fragile; microphone/TTS quality is worse. |
| Web app / PWA | Zero new packaging: the dashboard already works in a mobile browser, `/api/stt` gives local transcription. Fastest interim step; no background wake word. |

## 5. Order of work

1. Extract `/api/v1/*` from `harry_core/ui.py` (pure refactor, same handlers).
2. Add device pairing + bearer auth; keep loopback as-is.
3. Make memory/tasks single-writer with revisions, or designate the laptop as
   primary and let the phone queue writes.
4. Build the Kotlin client (chat, tasks, approvals, voice).
5. Add WorkManager-based reminders and push for proactive nudges.
6. Only then discuss an APK release.

Steps 1–2 are the only ones that change the server; the current desktop
behaviour must be preserved throughout.

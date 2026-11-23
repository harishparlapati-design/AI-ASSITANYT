# Hosting Harry

Harry ships with everything needed to run as a normal HTTPS web app on any
Linux Docker host, independent of the Windows PC. **Nothing here has been
purchased, deployed or made public.** Deployment needs your approval and an
account on one of the providers below.

## What already works

| Piece | Where | Status |
| --- | --- | --- |
| Login, sessions, CSRF | `harry_core/hosted.py` | implemented, tested (`tests/auth_test.py`) |
| Password store (scrypt, lockout) | `harry_core/accounts.py` | implemented, tested |
| Per-account file isolation | `harry_core/tenant.py` | documents, media and job history are tenant-scoped, tested |
| Security headers (CSP, HSTS, nosniff, frame denial) | `hosted.py` | implemented, tested |
| Health check | `GET /healthz` | implemented |
| Container image | `deploy/Dockerfile` | builds Flask + gunicorn, no local models |
| HTTPS reverse proxy | `deploy/docker-compose.yml` + `deploy/Caddyfile` | ready for a domain |
| Fly.io config | `deploy/fly.toml` | ready to edit and launch |
| PWA install | `/manifest.webmanifest`, `/sw.js`, `/icons/*` | implemented, tested |

## Architecture

```
Android / Windows / browser
        │  HTTPS (Caddy or the platform edge)
        ▼
 gunicorn (1 process, 8 threads) ── Flask app (harry_core.ui.create_app)
        │                            ├── hosted gate: session cookie + CSRF
        │                            ├── SSE /api/stream (live tokens)
        │                            ├── /api/chat, /api/stt (browser voice)
        │                            ├── /api/documents, /api/media
        │                            ├── /api/jobs (background jobs, in-process)
        │                            └── /api/screen (browser capture + vision)
        ▼
 /data volume: accounts.db, documents/<user>, media/<user>, jobs/<user>.json
        │
        ▼
 hosted inference: chat, vision, images (provider APIs, key in env)
```

Why one process: the job table and SSE fan-out are in memory by design for a
personal assistant. Eight threads handle concurrent requests and the long-lived
stream. Horizontal scaling would need the job table and event bus moved to a
shared queue first; that is not implemented.

## Step 1 — create an account on the server

```bash
# inside the container or a checkout with the same /data volume
HARRY_ACCOUNTS_DB=/data/accounts.db python -m harry_core.hosted \
  --create-user you@example.com --name "Harry's owner"
```

Passwords must be at least 10 characters. There is no public sign-up; that is
deliberate.

## Step 2 — pick a host

Prices below are from vendor pricing pages read on **23 November 2026**. They
are estimates, not quotes, and nothing is activated by this repository.

| Option | Always on | Persistent disk | Monthly compute | Notes |
| --- | --- | --- | --- | --- |
| **Fly.io** `shared-cpu-1x`, 1 GB | yes | 3 GB volume | **$6.70** + 3 GB × $0.15 = **$7.15** | Billed per second; personal orgs with a bill under $5 are waived. No free tier for new accounts. |
| **Render** Starter 512 MB | yes (paid) / sleeps (free) | 1 GB disk $0.25 | **$7.25** | Free web services sleep after 15 minutes and have **no persistent disk**, so generated files would vanish. |
| **Railway** Hobby | yes | 1 GB volume | **$5.00** minimum (includes $5 usage) | `$1/month` free plan is a demo keep-alive, not always-on. |
| **Oracle Cloud Always Free** ARM VM | yes | 100 GB block | **$0** | Real free VM (4 OCPU / 24 GB), but account + card verification and you manage the OS, Docker, Caddy and backups yourself. |

### Inference (the part that replaces your PC's GPU/CPU)

| Workload | Option | Approximate price |
| --- | --- | --- |
| Chat / reasoning | any OpenAI-compatible API (OpenAI, Groq, DeepSeek, Gemini…) | ≈ $0.10–$0.60 per 1M tokens; personal use of 100k–1M tokens/month ≈ **$0.02–$0.60** |
| Vision (screen questions) | same API with a vision model | a few tenths of a cent per frame |
| Images | provider image endpoint | ≈ $0.01–$0.08 per image |
| Video | Replicate / fal | provider-specific, commonly $0.10–$1+ per clip — **needs your approval** |

### Realistic totals

| Usage | Compute + disk | Inference | **Total / month** |
| --- | --- | --- | --- |
| Low (a few chats a day, no images) | $5.00–$7.25 | $0.05–$0.50 | **≈ $5–8** |
| Medium (daily use, documents, some images) | $5.00–$7.25 | $1–$4 | **≈ $6–11** |
| Heavy (long sessions, many images/videos) | $8–$13 (bigger VM) | $5–$25+ | **≈ $13–38+** |

Recommended first deployment: **Fly.io, 1 GB, 3 GB volume (~$7.15/month) +
a hosted chat API (~$0.50/month at personal volume) ≈ $8/month.** If you would
rather not pay at all, the Oracle Always Free VM keeps compute at $0 and only
inference costs money.

## Step 3 — deploy

### Docker anywhere (Fly, Oracle VM, your own server)

```bash
cp deploy/.env.example deploy/.env       # fill in the secret and provider key
python -c "import secrets; print(secrets.token_urlsafe(48))"   # HARRY_SESSION_SECRET

# optional: a config.json for language, media providers, voice defaults
docker compose -f deploy/docker-compose.yml up -d --build
```

Point `HARRY_DOMAIN` at the host; Caddy obtains the certificate automatically.
Without a domain you get `http://<host>:8000`, which is fine on a LAN only.

### Fly.io

```bash
fly launch --no-deploy --copy-config --config deploy/fly.toml
fly volumes create harry_data --size 3 --region bom
fly secrets set HARRY_SESSION_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
fly secrets set OPENAI_API_KEY=...        # your key, never committed
fly deploy --config deploy/fly.toml
fly open                                  # https://<app>.fly.dev
```

## Step 4 — verify it for real

Run these **against the public URL**, not localhost:

1. `GET /healthz` returns `{"ok":true}` over HTTPS.
2. Signed-out `GET /` redirects to `/login`; `/api/state` returns 401 JSON.
3. Sign in with the account from step 1; cookie is `Secure; HttpOnly; SameSite=Lax`.
4. Ask a question and watch tokens stream in.
5. Create a PDF, DOCX and XLSX from the dashboard; download each and open it.
6. Share the screen and ask about it (desktop browser only).
7. `POST /api/documents/generate` **without** `X-CSRF-Token` returns 403.
8. Sign in as a second account: no documents, no media, no jobs of the first.
9. Install the PWA on Android, then open it with the PC switched off.
10. Restart the service and confirm files, history and accounts survive.

## Known limitations (deliberate, documented, not hidden)

- **Single process.** Do not scale to multiple gunicorn workers: jobs and SSE
  would fragment. Scale with threads (`--threads 8`) instead.
- **Disk volume required.** Documents and media are files on `/data`. With no
  volume they disappear on redeploy. Object storage is not wired in yet.
- **Running jobs are not durable.** History and finished files survive a
  restart; a job that was mid-flight is marked `interrupted`.
- **No push notifications.** The PWA is installable and works offline for the
  shell, but there is no Web Push subscription service.
- **Local models stay local.** Hosted Harry uses provider APIs for chat, vision
  and images; it does not ship Ollama, moondream or sd-turbo.
- **Voice on the web** uses the browser's speech recognition/synthesis; server
  side Vosk/pyttsx3 are desktop-only.
- **No billing, teams or sharing** — one owner account per instance is the
  intended model, though artifacts are isolated per account if you create more.

"""Web UI: conversation, task dashboard, permissions, voice state, emergency stop.

Flask + Server-Sent Events. Runs on localhost by default.

Single-delivery contract (why the dashboard never shows an answer twice):

* ``POST /api/chat`` takes a client-generated ``request_id`` and returns at
  once (202 Accepted) instead of blocking until the answer and the spoken
  reply are finished.
* The answer is delivered as exactly ONE ``assistant_message`` event carrying
  that ``request_id``.
* ``GET /api/request/<id>`` exposes the same answer for clients that missed the
  event (slow network, reconnect). The page keys both transports by request id,
  so the second delivery is dropped instead of rendered again.
* An id reused by a retry, or an identical submit while the first one is still
  running, is refused as ``duplicate`` - it can never create a second task.

Voice input is browser-side (Web Speech API when available, otherwise the local
Vosk model through ``POST /api/stt``). Nothing is uploaded to a third party.
"""

import json
import logging
import queue
import threading
import time
import uuid
from collections import OrderedDict

from . import events, config
from .brain import get_brain
from .security import get_permissions

log = logging.getLogger("harry.ui")

_flask = None


def _q_listener(event, data):
    for q in list(_queues):
        try:
            q.put_nowait({"event": event, "data": data})
        except Exception:
            pass


_queues = []

# request bookkeeping (bounded, in-memory: this is a UI cache, not storage)
_requests = OrderedDict()
_requests_lock = threading.Lock()
_MAX_KEEP = 300
_recent_submits = {}          # (source, text) -> (ts, request_id)
_DUPLICATE_WINDOW = 2.0       # seconds: an identical submit inside this window
                              # while the first is still running is a double click


def _public(entry):
    return {
        "id": entry["id"],
        "status": entry["status"],
        "response": entry.get("response"),
        "error": entry.get("error"),
        "elapsed_ms": entry.get("elapsed_ms"),
        "created": entry.get("created"),
    }


def create_app():
    global _flask
    if _flask is not None:
        return _flask
    from flask import Flask, request, jsonify, Response

    app = Flask(__name__, static_folder=None)
    brain = get_brain()

    # ---------------- remote / multi-device access -------------
    # Loopback is always allowed (single-user desktop). Any non-loopback client
    # (phone, tablet, another PC) must have remote access enabled AND present the
    # shared token from the env var named in config - never a token in source or
    # in the repository. This is what makes the same API usable by a future
    # Android client without exposing the assistant to the local network by
    # default.
    remote_cfg = (config.CONFIG.get("ui", {}) or {}).get("remote", {}) or {}

    def _is_loopback(addr):
        addr = str(addr or "")
        return addr in ("127.0.0.1", "::1", "localhost") or addr.startswith("127.")

    @app.before_request
    def _guard_remote():
        if _is_loopback(request.remote_addr):
            return None
        if not remote_cfg.get("enabled"):
            return jsonify({"error": "remote access is disabled on this machine"}), 403
        import hmac
        import os
        token = os.environ.get(remote_cfg.get("token_env") or "HARRY_REMOTE_TOKEN", "")
        supplied = request.headers.get("X-Harry-Token") or request.args.get("token", "")
        if not token:
            log.warning("remote request refused: %s is not set",
                        remote_cfg.get("token_env") or "HARRY_REMOTE_TOKEN")
            return jsonify({"error": "no remote token configured on the server"}), 503
        if not hmac.compare_digest(str(supplied), str(token)):
            return jsonify({"error": "unauthorized"}), 401
        return None

    # UI becomes the confirmer for sensitive actions while it is open
    pending_confirmations = {}
    _confirm_seq = [0]

    def ui_confirmer(action_desc, level):
        _confirm_seq[0] += 1
        cid = f"c{_confirm_seq[0]}"
        done = threading.Event()
        pending_confirmations[cid] = {"action": action_desc, "level": level,
                                      "done": done, "result": None}
        events.emit("permission_request", {"id": cid, "action": action_desc,
                                           "level": level})
        done.wait(timeout=120)
        entry = pending_confirmations.pop(cid, None)
        return bool(entry and entry["result"])

    get_permissions().set_confirmer(ui_confirmer)

    # ---------------- request execution ----------------

    def _execute(rid, text, source):
        started = time.perf_counter()
        entry = _requests.get(rid)
        try:
            answer = brain.handle_input(text, source=source, request_id=rid)
            if entry is not None:
                entry["status"] = "done"
                entry["response"] = answer
        except events.EmergencyStop:
            if entry is not None:
                entry["status"] = "cancelled"
                entry["error"] = "emergency stop"
        except Exception as e:
            log.exception("request %s failed", rid)
            if entry is not None:
                entry["status"] = "error"
                entry["error"] = f"{type(e).__name__}: {e}"
            events.emit("task_error", {"id": rid, "step": "request",
                                       "error": f"{type(e).__name__}: {e}",
                                       "attempt": 1})
        finally:
            if entry is not None:
                entry["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
                log.info("request %s finished in %sms (%s)", rid,
                         entry["elapsed_ms"], entry["status"])

    # ---------------- routes ----------------

    @app.get("/")
    def index():
        return _INDEX_HTML

    @app.get("/vr")
    def vr():
        """WebXR front-end (optional): the desktop/mobile UI is unaffected."""
        return _VR_HTML

    @app.post("/api/chat")
    def chat():
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("text", "") or "").strip()
        if not text:
            return jsonify({"error": "empty"}), 400
        rid = str(payload.get("request_id") or "").strip() or uuid.uuid4().hex
        source = "ui-voice" if payload.get("source") == "ui-voice" else "ui"
        wait = bool(payload.get("wait"))

        with _requests_lock:
            existing = _requests.get(rid)
            if existing is not None:
                # Idempotent retry: never run the same request id twice.
                log.info("request %s reused (status %s)", rid, existing["status"])
                return jsonify(_public(existing)), 200
            now = time.time()
            key = (source, text.lower())
            prev = _recent_submits.get(key)
            if prev and now - prev[0] < _DUPLICATE_WINDOW:
                prev_entry = _requests.get(prev[1])
                if prev_entry is not None and prev_entry["status"] == "running":
                    log.info("duplicate submit suppressed (%s ~ %s)", rid, prev[1])
                    return jsonify({"status": "duplicate", "request_id": prev[1]}), 200
            entry = {"id": rid, "text": text, "source": source,
                     "status": "running", "response": None, "error": None,
                     "created": now, "elapsed_ms": None}
            _requests[rid] = entry
            _recent_submits[key] = (now, rid)
            while len(_requests) > _MAX_KEEP:
                _requests.popitem(last=False)
            while len(_recent_submits) > _MAX_KEEP:
                _recent_submits.pop(next(iter(_recent_submits)))

        if wait:
            _execute(rid, text, source)
            return jsonify(_public(entry)), 200

        threading.Thread(target=_execute, args=(rid, text, source),
                         daemon=True, name=f"chat-{rid[:6]}").start()
        return jsonify(_public(entry)), 202

    @app.get("/api/request/<rid>")
    def request_state(rid):
        entry = _requests.get(rid)
        if entry is None:
            return jsonify({"error": "unknown request"}), 404
        return jsonify(_public(entry))

    @app.get("/api/stream")
    def stream():
        q = queue.Queue(maxsize=500)
        _queues.append(q)

        def gen():
            try:
                yield "retry: 2000\n\n"
                while True:
                    try:
                        item = q.get(timeout=15)
                        yield (f"event: {item['event']}\n"
                               f"data: {json.dumps(item['data'], ensure_ascii=False)}\n\n")
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                if q in _queues:
                    _queues.remove(q)

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache"})

    @app.get("/api/state")
    def state():
        from .task_engine import get_engine
        from .tools import get_registry
        from .memory import get_memory
        # Send only what the sidebar renders: full task dicts (steps, results,
        # answers) made this endpoint heavy while the model was busy.
        tasks = [{"id": t["id"], "title": t["title"],
                  "status": t["status"], "created_at": t["created_at"]}
                 for t in get_engine().list_tasks(limit=20)]
        return jsonify({
            "tasks": tasks,
            "tools": get_registry().names(),
            "memory": get_memory().stats(),
            "llm": llm_status(),
            "output_mode": config.CONFIG.get("voice", {}).get("output_mode", "both"),
            "busy": bool(brain.busy),
            "speaking": bool(brain.speaking or __import__(
                "harry_core.speech", fromlist=["get_speaker"]
            ).get_speaker().speaking),
            "agents": [a["name"] for a in __import__(
                "harry_core.agents", fromlist=["list_agents"]).list_agents()],
        })

    # Probing Ollama blocks for up to 2s when the model server is busy (i.e.
    # exactly while an answer is being generated). Never do that inside a
    # request: refresh in the background and serve the last known value.
    llm_cache = {"ts": 0.0, "value": {"local": [], "online": False},
                 "running": False}

    def llm_status():
        now = time.time()
        if now - llm_cache["ts"] > 25 and not llm_cache["running"]:
            llm_cache["running"] = True

            def probe():
                try:
                    from .llm import get_router
                    value = get_router().status()
                except Exception:
                    value = {"local": [], "online": False}
                llm_cache.update(ts=time.time(), value=value, running=False)

            threading.Thread(target=probe, daemon=True, name="llm-probe").start()
        return llm_cache["value"]

    _warmup = {"started": False, "ready": False}

    @app.post("/api/stt/warm")
    def stt_warm():
        """Load the local Vosk model in the background before it is needed.

        The model takes a long time to load cold (~70s here); warming it while
        the user is still speaking keeps the fallback responsive. Idempotent.
        """
        def _load():
            try:
                from .speech import get_listener
                listener = get_listener()
                if listener.available():
                    listener._get_model()
                    _warmup["ready"] = True
            except Exception as e:
                log.warning("stt warm-up failed: %s", e)

        if not _warmup["started"]:
            _warmup["started"] = True
            threading.Thread(target=_load, daemon=True, name="stt-warm").start()
        return jsonify({"started": True, "ready": _warmup["ready"]})

    @app.post("/api/stt")
    def stt():
        """Offline transcription of browser-recorded PCM16 mono 16 kHz audio."""
        data = request.get_data(cache=False)
        if not data:
            return jsonify({"error": "no audio"}), 400
        if len(data) > 5 * 1024 * 1024:
            return jsonify({"error": "audio too large"}), 413
        if data[:4] == b"RIFF":                 # tolerate a WAV container
            idx = data.find(b"data")
            if idx != -1:
                data = data[idx + 8:]
        from .speech import get_listener
        started = time.perf_counter()
        text = get_listener().transcribe_pcm(data)
        elapsed = round((time.perf_counter() - started) * 1000)
        log.info("stt: %s bytes -> %r (%sms)", len(data), text[:80], elapsed)
        return jsonify({"text": text, "ms": elapsed})

    @app.post("/api/confirm/<cid>")
    def confirm(cid):
        entry = pending_confirmations.get(cid)
        if entry:
            entry["result"] = bool((request.json or {}).get("approve"))
            entry["done"].set()
        return jsonify({"ok": True})

    @app.post("/api/stop")
    def stop():
        events.emergency_stop("UI emergency stop")
        brain.stop()
        with _requests_lock:
            for entry in list(_requests.values()):
                if entry["status"] == "running":
                    entry["status"] = "cancelled"
                    entry["error"] = "emergency stop"
        return jsonify({"ok": True})

    @app.post("/api/task/<task_id>/rerun")
    def rerun(task_id):
        from .task_engine import get_engine
        t = get_engine().get(task_id)
        if not t:
            return jsonify({"error": "not found"}), 404
        threading.Thread(target=lambda: get_engine().run_task(task_id),
                         daemon=True).start()
        return jsonify({"ok": True})

    @app.post("/api/config/output_mode")
    def set_output_mode():
        mode = (request.json or {}).get("mode", "both")
        if mode in ("text", "voice", "both"):
            config.CONFIG["voice"]["output_mode"] = mode
            return jsonify({"ok": True, "mode": mode})
        return jsonify({"error": "invalid"}), 400

    # ---------------- voice settings ----------------

    @app.get("/api/voice")
    def get_voice():
        from .speech import get_speaker
        return jsonify(get_speaker().voice_state())

    @app.post("/api/voice")
    def set_voice():
        from .speech import get_speaker
        payload = request.get_json(silent=True) or {}
        speaker = get_speaker()
        ok = speaker.update_settings(name=payload.get("name"),
                                     gender=payload.get("gender"),
                                     rate=payload.get("rate"),
                                     volume=payload.get("volume"))
        if not ok:
            return jsonify({"error": "nothing to change", **speaker.voice_state()}), 400
        return jsonify({"ok": True, **speaker.voice_state()})

    @app.post("/api/speech/stop")
    def stop_speaking():
        from .speech import get_speaker
        get_speaker().stop_speaking()
        return jsonify({"ok": True})

    @app.post("/api/speech/test")
    def test_speech():
        from .speech import get_speaker
        speaker = get_speaker()
        if brain.speaking or speaker.speaking:
            return jsonify({"ok": False, "error": "Harry is already speaking."}), 409

        def _test():
            speaker.speak("Hello, this is Harry's voice check.", output_mode="voice")

        threading.Thread(target=_test, daemon=True, name="voice-test").start()
        return jsonify({"ok": True, "status": "started"}), 202

    # ---------------- persistent dashboard preferences ----------------

    @app.get("/api/preferences")
    def get_preferences():
        return jsonify({
            "theme": config.CONFIG.get("appearance", {}).get("theme", "system"),
            "language": config.CONFIG.get("language", {}).get("preference", "auto"),
            "integrations": {
                "github_enabled": bool(config.CONFIG.get("integrations", {}).get("github_enabled", True)),
                "codex_enabled": bool(config.CONFIG.get("integrations", {}).get("codex_enabled", True)),
                "claude_enabled": bool(config.CONFIG.get("integrations", {}).get("claude_enabled", True)),
            },
        })

    @app.post("/api/preferences")
    def set_preferences():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"error": "preferences must be an object"}), 400
        updates = {}
        theme = payload.get("theme")
        language = payload.get("language")
        if theme is not None:
            if theme not in ("system", "dark", "light"):
                return jsonify({"error": "theme must be system, dark, or light"}), 400
            updates["appearance"] = {"theme": theme}
        if language is not None:
            if language not in ("auto", "mixed", "english", "telugu"):
                return jsonify({"error": "language must be auto, mixed, english, or telugu"}), 400
            updates["language"] = {"preference": language}
        integration_flags = payload.get("integrations")
        if integration_flags is not None:
            if not isinstance(integration_flags, dict):
                return jsonify({"error": "integrations must be an object"}), 400
            allowed = ("github_enabled", "codex_enabled", "claude_enabled")
            if any(key not in allowed or not isinstance(value, bool)
                   for key, value in integration_flags.items()):
                return jsonify({"error": "integration preferences must be boolean flags"}), 400
            if integration_flags:
                updates["integrations"] = dict(integration_flags)
        allowed_fields = {"theme", "language", "integrations"}
        if any(key not in allowed_fields for key in payload):
            return jsonify({"error": "unknown preference field"}), 400
        if updates and not config.update_user_sections(updates):
            return jsonify({"error": "Harry could not save these local settings."}), 500
        for section, values in updates.items():
            config.CONFIG.setdefault(section, {}).update(values)
        return get_preferences()

    # ---------------- actual integrations ----------------

    @app.get("/api/integrations")
    def integrations_status():
        from .integrations import integration_snapshot
        from .tools import get_registry
        include_activity = request.args.get("activity") == "1"
        data = integration_snapshot(get_registry().names())
        if include_activity:
            from .integrations import github_activity
            data["github"]["activity"] = github_activity()
        return jsonify(data)

    @app.get("/api/github/activity")
    def github_activity_status():
        from .integrations import github_activity
        return jsonify(github_activity())

    @app.post("/api/github/refresh")
    def github_refresh():
        from .integrations import refresh_github
        result = refresh_github()
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.post("/api/github/branch")
    def github_create_branch():
        from .integrations import create_branch
        payload = request.get_json(silent=True) or {}
        result = create_branch(payload.get("name"), approved=payload.get("approved") is True)
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.post("/api/github/commit")
    def github_commit():
        from .integrations import commit_paths
        payload = request.get_json(silent=True) or {}
        result = commit_paths(payload.get("paths"), payload.get("message"),
                              approved=payload.get("approved") is True)
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.post("/api/github/push")
    def github_push():
        from .integrations import push_dev
        payload = request.get_json(silent=True) or {}
        result = push_dev(approved=payload.get("approved") is True)
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.post("/api/agents")
    def delegate_agent():
        from .integrations import start_agent
        payload = request.get_json(silent=True) or {}
        result = start_agent(payload.get("agent"), payload.get("prompt"))
        return jsonify(result), (202 if result.get("ok") else 400)

    @app.get("/api/agents/<task_id>")
    def get_agent_task(task_id):
        from .integrations import agent_task
        result = agent_task(task_id)
        return (jsonify(result), 200) if result else (jsonify({"error": "task not found"}), 404)

    @app.post("/api/agents/<task_id>/cancel")
    def stop_agent_task(task_id):
        from .integrations import cancel_agent
        result = cancel_agent(task_id)
        return jsonify(result), (200 if result.get("ok") else 400)

    events.subscribe(_q_listener)
    _flask = app
    return app


def run(host=None, port=None):
    app = create_app()
    host = host or config.CONFIG["ui"]["host"]
    port = int(port or config.CONFIG["ui"]["port"])
    print(f"\nHarry UI: http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)


# --------------------------------------------------------------------- dashboard html

_INDEX_HTML = """<!doctype html>
<html lang="en" data-theme="system">
<head>
<meta charset="utf-8">
<title>Harry — local AI workspace</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    color-scheme:dark;
    --bg:#09111e; --surface:rgba(17,27,44,.84); --surface-strong:#111d30;
    --surface-soft:rgba(28,43,65,.70); --line:rgba(154,180,218,.16);
    --acc:#7c9dff; --acc-strong:#6384f3; --ok:#57d5a2; --err:#ff7887;
    --warn:#edc26b; --tx:#e9effb; --dim:#98a8c2; --faint:#697b98;
    --shadow:0 18px 60px rgba(0,0,0,.26); --input:rgba(7,15,27,.76);
    --grid:rgba(130,164,220,.055); --glass:blur(18px);
  }
  :root[data-theme="light"] {
    color-scheme:light;
    --bg:#eef3fa; --surface:rgba(255,255,255,.86); --surface-strong:#fff;
    --surface-soft:rgba(236,242,251,.90); --line:rgba(55,76,111,.14);
    --acc:#4b67d5; --acc-strong:#3c58c4; --ok:#168661; --err:#cc4057;
    --warn:#9b6a09; --tx:#17243b; --dim:#566783; --faint:#73829a;
    --shadow:0 18px 50px rgba(40,59,91,.10); --input:#f7f9fd;
    --grid:rgba(55,76,111,.055);
  }
  @media (prefers-color-scheme:light) {
    :root[data-theme="system"] {
      color-scheme:light;
      --bg:#eef3fa; --surface:rgba(255,255,255,.86); --surface-strong:#fff;
      --surface-soft:rgba(236,242,251,.90); --line:rgba(55,76,111,.14);
      --acc:#4b67d5; --acc-strong:#3c58c4; --ok:#168661; --err:#cc4057;
      --warn:#9b6a09; --tx:#17243b; --dim:#566783; --faint:#73829a;
      --shadow:0 18px 50px rgba(40,59,91,.10); --input:#f7f9fd;
      --grid:rgba(55,76,111,.055);
    }
  }
  * { box-sizing:border-box; }
  html { min-height:100%; background:var(--bg); }
  body {
    min-height:100vh; margin:0; color:var(--tx);
    font:14px/1.55 Inter,"Segoe UI",system-ui,sans-serif;
    background:
      radial-gradient(ellipse at 18% -8%,rgba(94,126,226,.17),transparent 39%),
      radial-gradient(ellipse at 93% 7%,rgba(56,180,190,.10),transparent 28%),
      linear-gradient(var(--grid) 1px,transparent 1px),
      linear-gradient(90deg,var(--grid) 1px,transparent 1px), var(--bg);
    background-size:auto,auto,36px 36px,36px 36px,auto;
    transition:background-color .25s,color .25s;
  }
  button,input,select,textarea { font:inherit; }
  button,select,input[type=text],input[type=search],textarea {
    color:var(--tx); border:1px solid var(--line); border-radius:11px;
    background:var(--input); transition:border-color .18s,background .18s,transform .18s;
  }
  button { cursor:pointer; padding:9px 13px; background:var(--acc-strong); color:white; border-color:transparent; font-weight:650; }
  button:hover:not(:disabled) { transform:translateY(-1px); filter:brightness(1.06); }
  button:disabled { opacity:.48; cursor:not-allowed; }
  button.ghost { background:var(--surface-soft); color:var(--tx); border-color:var(--line); }
  button.danger { background:rgba(204,64,87,.14); color:var(--err); border-color:rgba(204,64,87,.25); }
  input[type=text],input[type=search],select,textarea { width:100%; padding:10px 12px; }
  textarea { min-height:84px; resize:vertical; }
  input:focus,select:focus,textarea:focus,button:focus-visible { outline:2px solid color-mix(in srgb,var(--acc) 60%,transparent); outline-offset:2px; }
  input[type=range] { width:100%; accent-color:var(--acc); }
  a { color:var(--acc); }
  ::selection { background:var(--acc); color:#fff; }
  ::-webkit-scrollbar { width:9px; height:9px; }
  ::-webkit-scrollbar-thumb { background:color-mix(in srgb,var(--dim) 35%,transparent); border:2px solid transparent; background-clip:padding-box; border-radius:9px; }
  .app { max-width:1680px; min-height:100vh; margin:0 auto; padding:20px 24px 30px; }
  .topbar { display:flex; align-items:center; gap:16px; min-height:58px; margin-bottom:17px; }
  .brand { display:flex; align-items:center; gap:12px; min-width:240px; }
  .brand-mark { width:38px;height:38px;border:1px solid rgba(124,157,255,.4);border-radius:13px;display:grid;place-items:center;color:var(--acc);background:linear-gradient(145deg,rgba(124,157,255,.22),rgba(72,192,202,.08));box-shadow:0 0 30px rgba(99,132,243,.15);font-size:19px; }
  .brand h1 { font-size:16px; letter-spacing:.15em; margin:0; }
  .brand small { display:block;color:var(--dim);font-size:11px;letter-spacing:.06em; }
  .top-spacer { flex:1; }
  .top-control { display:flex;align-items:center;gap:8px;color:var(--dim);font-size:11px; }
  .top-control select { width:auto;min-width:124px;padding:8px 30px 8px 10px;font-size:12px; }
  .status-chip,.chip { display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:999px;padding:5px 10px;background:var(--surface-soft);color:var(--dim);font-size:11px; }
  .status-chip::before { content:"";width:6px;height:6px;border-radius:50%;background:var(--ok);box-shadow:0 0 10px currentColor; }
  .status-chip[data-state=processing]::before,.chip[data-state=processing]::before { background:var(--warn); }
  .status-chip[data-state=listening]::before,.chip[data-state=listening]::before { background:var(--err); }
  .status-chip[data-state=speaking]::before,.chip[data-state=speaking]::before { background:var(--acc); }
  .status-chip[data-state=error]::before,.chip[data-state=error]::before { background:var(--err); }
  .layout { display:grid;grid-template-columns:270px minmax(380px,1fr) 300px;gap:15px;align-items:stretch; }
  .panel { min-width:0;background:var(--surface);backdrop-filter:var(--glass);border:1px solid var(--line);border-radius:19px;box-shadow:var(--shadow); }
  .sidebar,.rightbar { padding:16px; }
  .panel-heading { display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 10px;font-size:11px;font-weight:750;letter-spacing:.13em;text-transform:uppercase;color:var(--dim); }
  .voice-card,.info-card,.subpanel,.integration-card { background:var(--surface-soft);border:1px solid var(--line);border-radius:14px;padding:12px; }
  .voice-card { margin-bottom:13px; }
  #voiceSel { font-size:12px; }
  #voiceMeta { color:var(--dim);font-size:11px;min-height:18px;margin:7px 0 10px;overflow-wrap:anywhere; }
  .range-row { display:block;margin:10px 0 12px;font-size:12px;color:var(--dim); }
  .range-row span { float:right;color:var(--tx);font-variant-numeric:tabular-nums; }
  #speakStop,#voiceTest { width:100%;margin-top:7px; }
  .output-options { display:grid;gap:8px;font-size:12px;color:var(--tx); }
  .output-options label,.statusbar label { display:flex;align-items:center;gap:8px; }
  input[type=radio],input[type=checkbox] { accent-color:var(--acc); }
  #stop { width:100%;margin:4px 0 15px; }
  .helper { color:var(--dim);font-size:11px; }
  .chat-panel { display:flex;flex-direction:column;min-height:660px;overflow:hidden; }
  .chat-head { display:flex;align-items:center;gap:14px;padding:20px 22px 14px;border-bottom:1px solid var(--line); }
  .chat-title { flex:1; }
  .chat-title h2 { margin:0;font-size:16px;letter-spacing:.02em; }
  .chat-title p { margin:3px 0 0;color:var(--dim);font-size:11px; }
  .orb-stage { width:50px;height:50px;display:grid;place-items:center; }
  #voiceOrb { width:38px;height:38px;border-radius:50%;position:relative;display:block;background:radial-gradient(circle at 34% 28%,#d4e6ff 0,#809dff 18%,#4969d5 42%,rgba(56,116,180,.4) 70%,transparent 74%);box-shadow:0 0 24px rgba(104,139,255,.38),inset -7px -8px 13px rgba(8,16,43,.65);transition:transform .35s,box-shadow .35s; }
  #voiceOrb::after { content:"";position:absolute;inset:-7px;border:1px solid rgba(124,157,255,.27);border-radius:50%; }
  #voiceOrb[data-state=processing] { animation:orb-process 1.5s ease-in-out infinite; }
  #voiceOrb[data-state=listening] { animation:orb-listen .8s ease-in-out infinite alternate;box-shadow:0 0 28px rgba(255,120,135,.55); }
  #voiceOrb[data-state=speaking] { animation:orb-speak .55s ease-in-out infinite alternate; }
  @keyframes orb-process { 50%{transform:scale(1.08);box-shadow:0 0 34px rgba(104,139,255,.64)} }
  @keyframes orb-listen { to{transform:scale(1.12)} }
  @keyframes orb-speak { to{transform:scale(1.10);filter:brightness(1.24)} }
  #log { flex:1;min-height:340px;max-height:calc(100vh - 360px);overflow-y:auto;padding:20px 22px;scroll-behavior:smooth; }
  .msg { max-width:min(82%,720px);padding:11px 14px;border-radius:15px;margin:8px 0;white-space:pre-wrap;overflow-wrap:anywhere; }
  .msg.user { background:linear-gradient(135deg,var(--acc-strong),#556ec7);color:#fff;margin-left:auto;border-bottom-right-radius:5px; }
  .msg.harry { background:var(--surface-soft);border:1px solid var(--line);border-bottom-left-radius:5px; }
  .msg.sys { max-width:100%;color:var(--dim);font-size:11px;text-align:center;padding:6px; }
  .typing { opacity:.75;font-style:italic; }
  #confirmations { padding:0 20px; }
  .confirm { border:1px solid rgba(237,194,107,.42);background:color-mix(in srgb,var(--warn) 10%,var(--surface));padding:12px;margin:7px 0;border-radius:13px; }
  .confirm button { margin:8px 8px 0 0; }
  #f { display:flex;gap:8px;padding:14px 18px 9px;border-top:1px solid var(--line); }
  #t { flex:1;min-width:0; }
  #mic { width:42px;padding:8px;font-size:17px;background:var(--surface-soft);color:var(--tx);border-color:var(--line); }
  #mic.on { background:var(--err);color:white; }
  #micstop { padding:8px 11px; }
  #statusbar { display:flex;align-items:center;gap:11px;flex-wrap:wrap;padding:3px 19px 14px;color:var(--dim);font-size:11px; }
  #hint { margin-left:auto; }
  .rightbar { display:flex;flex-direction:column;gap:12px; }
  .rightbar .panel-heading { margin-bottom:8px; }
  #tasks { display:grid;gap:7px;max-height:295px;overflow:auto; }
  .card { background:var(--surface-soft);border:1px solid var(--line);border-radius:12px;padding:9px 10px;margin:0 0 7px;font-size:11px;overflow-wrap:anywhere; }
  .task { border-left:3px solid var(--acc); }
  .task.done { border-left-color:var(--ok); } .task.failed { border-left-color:var(--err); }
  .pill { display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 8px;font-size:10px;color:var(--dim); }
  #mem,#llm,#tools { color:var(--tx);font-size:11px;line-height:1.7; }
  #toolcount { color:var(--tx); }
  .detail-grid { display:grid;grid-template-columns:minmax(320px,1.1fr) minmax(280px,.85fr) minmax(320px,1fr);gap:15px;margin-top:15px; }
  .detail-panel { padding:16px;min-width:0; }
  .detail-panel > p { margin:-4px 0 12px;color:var(--dim);font-size:11px; }
  .section-title { display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px; }
  .section-title h2 { margin:0;font-size:12px;letter-spacing:.12em;text-transform:uppercase; }
  .small-btn { padding:7px 10px;font-size:11px; }
  .github-summary { display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-bottom:10px; }
  .github-summary .info-card { padding:9px;font-size:11px;overflow-wrap:anywhere; }
  .github-summary strong { display:block;color:var(--dim);font-size:9px;letter-spacing:.1em;text-transform:uppercase; }
  .commit-list,.change-list,.integration-list { display:grid;gap:7px;max-height:250px;overflow:auto; }
  .change-row { display:flex;align-items:flex-start;gap:8px;overflow-wrap:anywhere;font:11px/1.4 ui-monospace,Consolas,monospace; }
  .change-row input { margin-top:2px;flex:0 0 auto; }
  .inline-form { display:flex;gap:7px;margin:10px 0; }
  .inline-form input { min-width:0; }
  .inline-form textarea { flex:1; }
  .activity-block { border-top:1px solid var(--line);margin-top:11px;padding-top:10px; }
  .activity-block h3 { margin:0 0 7px;color:var(--dim);font-size:10px;letter-spacing:.09em;text-transform:uppercase; }
  .activity-link { display:block;font-size:11px;text-decoration:none;padding:5px 0;overflow-wrap:anywhere; }
  .activity-link:hover { text-decoration:underline; }
  .integration-category { margin:13px 0 6px;color:var(--dim);font-size:9px;font-weight:750;letter-spacing:.14em;text-transform:uppercase; }
  .integration-card { padding:10px;margin-bottom:6px; }
  .integration-head { display:flex;align-items:flex-start;gap:8px; }
  .integration-name { flex:1;font-size:12px;font-weight:700; }
  .integration-status { font-size:9px;padding:3px 7px;border-radius:999px;background:var(--surface-strong);border:1px solid var(--line);color:var(--dim);white-space:nowrap; }
  .integration-status[data-status=connected],.integration-status[data-status=available] { color:var(--ok); }
  .integration-status[data-status=disabled],.integration-status[data-status="not installed"],.integration-status[data-status="not configured"] { color:var(--dim); }
  .integration-desc,.last-run { margin-top:5px;color:var(--dim);font-size:10px; }
  .integration-actions { display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:7px;font-size:10px;color:var(--dim); }
  .integration-actions label { display:flex;align-items:center;gap:5px; }
  .agent-result { min-height:55px;max-height:250px;overflow:auto;margin-top:10px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px; }
  .agent-controls { display:grid;gap:8px; }
  .agent-controls .inline-form { margin:0; }
  .muted { color:var(--dim); }
  @media (max-width:1180px) {
    .layout { grid-template-columns:250px minmax(0,1fr); }
    .rightbar { grid-column:1 / -1;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));align-items:start; }
    .detail-grid { grid-template-columns:1fr 1fr; }
    .detail-panel.plugins { grid-column:1 / -1; }
    #tasks { max-height:220px; }
  }
  @media (max-width:760px) {
    .app { padding:12px 10px 20px; }
    .topbar { flex-wrap:wrap;gap:9px;margin-bottom:11px; }
    .brand { min-width:0;flex:1; }
    .top-spacer { display:none; }
    .top-control { flex:1;min-width:130px; }
    .top-control select { flex:1;min-width:0; }
    #headerState { order:4; }
    .layout { grid-template-columns:1fr;gap:10px; }
    .sidebar { display:grid;grid-template-columns:1fr 1fr;gap:9px;padding:12px; }
    .sidebar > #stop,.sidebar > .panel-heading:first-of-type { grid-column:1 / -1; }
    .voice-card { margin:0; }
    .output-card { align-self:start; }
    .chat-panel { min-height:600px; }
    .chat-head { padding:14px; }
    #log { max-height:none;min-height:330px;padding:14px; }
    .msg { max-width:91%; }
    #f { padding:10px; }
    #statusbar { padding:3px 12px 12px; }
    .rightbar { grid-column:auto;display:grid;grid-template-columns:1fr; }
    .detail-grid { grid-template-columns:1fr;gap:10px;margin-top:10px; }
    .detail-panel.plugins { grid-column:auto; }
  }
      @media (max-width:420px) {
    .topbar { flex-direction:column; align-items:stretch; }
    .top-control { flex-direction:column; align-items:stretch; gap:4px; min-width:0; }
    .top-control select { width:100%; }
    .brand { min-width:0; }
    .sidebar { grid-template-columns:1fr; }
    .sidebar > #stop,.sidebar > .panel-heading:first-of-type { grid-column:auto; }
    .chat-title p { max-width:190px; }
    .top-control { min-width:calc(50% - 8px); }
    #f { gap:5px; }
    #send { padding:8px 10px; }
  }
  @media (prefers-reduced-motion:reduce) { *,*::before,*::after { scroll-behavior:auto!important;animation-duration:.01ms!important;transition-duration:.01ms!important; } }
</style>
</head>
<body>
<div class="app">
  <header class="topbar">
    <div class="brand"><div class="brand-mark" aria-hidden="true">H</div><div><h1>HARRY</h1><small>LOCAL AI WORKSPACE</small></div></div>
    <span id="headerState" class="status-chip" data-state="ready">ready</span>
    <span class="top-spacer"></span>
    <label class="top-control">Theme
      <select id="themeSel" aria-label="Dashboard theme"><option value="system">System</option><option value="dark">Dark</option><option value="light">Light</option></select>
    </label>
    <label class="top-control">Response language
      <select id="languageSel" aria-label="Response language preference"><option value="auto">Automatic</option><option value="mixed">Telugu + English</option><option value="english">English</option><option value="telugu">Telugu</option></select>
    </label>
    <a href="/vr" class="helper">Immersive view ↗</a>
  </header>
  <div class="layout">
    <aside class="panel sidebar">
      <button id="stop" class="danger">⏹ Emergency stop</button>
      <div>
        <div class="panel-heading">Voice controls <span class="pill">local TTS</span></div>
        <div class="voice-card">
          <label class="helper" for="voiceGender">Voice preference</label>
          <select id="voiceGender" aria-label="Preferred voice gender">
            <option value="male">Male</option><option value="female">Female</option><option value="any">Any</option>
          </select>
          <label class="helper" for="voiceSel">Installed voice</label>
          <select id="voiceSel" aria-label="Installed voice"></select>
          <div id="voiceMeta">Checking speech engine…</div>
          <label class="range-row">Speaking speed <span id="rateVal">170</span>
            <input id="rate" aria-label="Speaking speed" type="range" min="150" max="240" step="5"></label>
          <label class="range-row">Volume <span><span id="volVal">100</span>%</span>
            <input id="vol" aria-label="Speaking volume" type="range" min="0" max="100" step="5"></label>
          <button id="voiceTest" class="ghost">Test voice</button>
          <button id="speakStop" class="ghost">⏹ Stop speaking</button>
        </div>
      </div>
      <div class="output-card">
        <div class="panel-heading">Output mode</div>
        <div id="om" class="output-options">
          <label><input type="radio" name="om" value="both"> Text + voice</label>
          <label><input type="radio" name="om" value="text"> Text only</label>
          <label><input type="radio" name="om" value="voice"> Voice only</label>
        </div>
        <p class="helper" style="margin-top:9px">Voice replies use the same language policy as text.</p>
      </div>
    </aside>
    <main class="panel chat-panel">
      <div class="chat-head">
        <div class="orb-stage"><span id="voiceOrb" data-state="ready" aria-hidden="true"></span></div>
        <div class="chat-title"><h2>Conversation</h2><p>Harry’s local assistant. Chat, voice, memory, and task actions share this session.</p></div>
        <span class="pill">private · local</span>
      </div>
      <div id="log" aria-live="polite" aria-label="Conversation"></div>
      <div id="confirmations"></div>
      <form id="f">
        <button id="mic" type="button" title="Speak to Harry" aria-label="Start microphone">🎤</button>
        <input type="text" id="t" placeholder="Ask Harry… (English / తెలుగు / Hindi / mixed)" autocomplete="off" aria-label="Message Harry">
        <button id="send" type="submit">Send</button>
        <button id="micstop" type="button" class="ghost" title="Stop listening" hidden>⏹</button>
      </form>
      <div id="statusbar">
        <span id="micstate" class="chip" data-state="ready">ready</span>
        <label><input type="checkbox" id="autosend" checked> auto-send spoken text</label>
        <span id="hint"></span>
      </div>
    </main>
    <aside class="panel rightbar">
      <section>
        <div class="panel-heading">Current tasks</div>
        <div id="tasks"></div>
      </section>
      <section>
        <div class="panel-heading">Memory</div>
        <div id="mem" class="info-card">Loading local memory state…</div>
      </section>
      <section>
        <div class="panel-heading">AI runtime</div>
        <div id="llm" class="info-card">Checking configured models…</div>
      </section>
      <section>
        <div class="panel-heading">Registered tools <span class="pill" id="toolcount">0</span></div>
        <div id="tools" class="info-card">Loading tool registry…</div>
      </section>
    </aside>
  </div>
  <section class="detail-grid">
    <section class="panel detail-panel">
      <div class="section-title"><h2>GitHub · dev workflow</h2><button id="githubRefresh" class="ghost small-btn">Refresh status</button></div>
      <p>Local repository facts and authenticated GitHub CLI status. Mutations stay on <code>dev</code> and require confirmation.</p>
      <div id="githubSummary" class="github-summary"><div class="info-card">Loading repository status…</div></div>
      <div id="changeList" class="change-list"></div>
      <div class="inline-form"><input id="commitMessage" type="text" maxlength="160" placeholder="Commit message"><button id="githubCommit" class="small-btn">Commit selected</button></div>
      <div class="inline-form"><input id="branchName" type="text" maxlength="64" placeholder="New branch from dev"><button id="githubBranch" class="ghost small-btn">Create branch</button></div>
      <div class="inline-form"><button id="githubPush" class="danger small-btn">Push dev</button><span class="helper">Normal push only; no force push.</span></div>
      <div class="activity-block"><h3>Recent commits</h3><div id="commitList" class="commit-list muted">—</div></div>
      <div class="activity-block"><h3>Issues & pull requests</h3><div id="githubActivity" class="muted">Not checked yet.</div></div>
      <div id="githubResult" class="helper" role="status" aria-live="polite"></div>
    </section>
    <section class="panel detail-panel">
      <div class="section-title"><h2>Agent delegation</h2></div>
      <p>Uses locally installed and signed-in CLIs. Delegated tasks run read-only with a 120-second limit.</p>
      <div class="agent-controls">
        <label class="helper" for="agentSel">Reasoning agent</label>
        <select id="agentSel"><option value="codex">OpenAI Codex</option><option value="claude">Anthropic Claude</option></select>
        <textarea id="agentPrompt" maxlength="4000" placeholder="Ask the selected agent to inspect or reason about this project…"></textarea>
        <div class="inline-form"><button id="delegateAgent">Delegate task</button><button id="cancelAgent" class="ghost" disabled>Cancel</button></div>
      </div>
      <div id="agentResult" class="agent-result muted" role="status" aria-live="polite">Agent availability is checked from the installed CLI and its local authentication state.</div>
    </section>
    <section class="panel detail-panel plugins">
      <div class="section-title"><h2>Plugins & tools</h2><button id="integrationsRefresh" class="ghost small-btn">Refresh</button></div>
      <p>Connection status comes from Harry’s tool registry, local CLIs, Git checkout, and GitHub authentication.</p>
      <div id="pluginGrid" class="integration-list">Checking installed integrations…</div>
    </section>
  </section>
</div>
<script>
const log = document.getElementById('log');
const input = document.getElementById('t');
const sendBtn = document.getElementById('send');
const micBtn = document.getElementById('mic');
const micStopBtn = document.getElementById('micstop');
const chip = document.getElementById('micstate');

  const seen = new Set();          // message keys already rendered
let pending = null;              // {rid, t0, el, iv}
let refreshTimer = null;
let agentPollTimer = null;
let activeAgentTask = null;
let lastVoiceState = null;
let integrationProbeTimer = null;
let integrationProbePolls = 0;

function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
function humanError(e){ return (e && e.message) ? e.message : String(e); }
function newId(){
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function addMsg(cls, text, key){
  if (key){ if (seen.has(key)) return null; seen.add(key); }
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
  return d;
}
function sys(text){ addMsg('sys', text); }

function setChip(state, text){
  chip.dataset.state = state || 'ready';
  chip.textContent = text || 'ready';
  const header = document.getElementById('headerState');
  const orb = document.getElementById('voiceOrb');
  if (header){ header.dataset.state = state || 'ready'; header.textContent = text || 'ready'; }
  if (orb) orb.dataset.state = state || 'ready';
}
function setBusy(on){
  sendBtn.disabled = on;
  micBtn.disabled = on;
  document.body.classList.toggle('busy', on);
}

function startPending(rid, label){
  pending = { rid: rid, t0: performance.now() };
  setBusy(true);
  const d = document.createElement('div');
  d.className = 'msg harry typing';
  d.textContent = label + ' 0.0s';
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
  pending.el = d;
  pending.iv = setInterval(function(){
    if (pending && pending.el)
      pending.el.textContent = label + ' ' + ((performance.now() - pending.t0) / 1000).toFixed(1) + 's';
  }, 100);
  setChip('processing', 'thinking…');
}

function finishPending(){
  if (!pending) return;
  clearInterval(pending.iv);
  if (pending.el && pending.el.parentNode) pending.el.parentNode.removeChild(pending.el);
  pending = null;
  setBusy(false);
}

function onAnswer(text, rid){
  const key = rid ? 'r:' + rid : null;
  if (pending && (!rid || pending.rid === rid)) finishPending();
  if (text) addMsg('harry', text, key);
  setTimeout(refreshState, 500);
  setTimeout(refreshState, 1800);
}

/* ---------------- sending ---------------- */

document.getElementById('f').addEventListener('submit', function(e){
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  submit(text);
});

async function submit(text, opts){
  text = (text || '').trim();
  if (!text) return;
  if (pending){
    sys('Still working on the previous request — press EMERGENCY STOP to cancel.');
    return;
  }
  const rid = newId();
  addMsg('user', text, 'u:' + rid);
  startPending(rid, (opts && opts.voice) ? 'Heard — thinking' : 'Thinking');
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: text,
        request_id: rid,
        source: (opts && opts.voice) ? 'ui-voice' : 'ui'
      })
    });
    if (res.status === 400){ sys('Nothing to send.'); finishPending(); return; }
    if (!res.ok){ sys('⚠ server error ' + res.status); finishPending(); return; }
    const j = await res.json();
    if (j.status === 'duplicate'){
      sys('Duplicate ignored — that request is already running.');
      finishPending();
      return;
    }
    if (j.status === 'done'){ onAnswer(j.response, rid); return; }
    pollResult(rid);
  } catch (e){
    sys('⚠ ' + humanError(e));
    finishPending();
  }
}

async function pollResult(rid){
  for (let i = 0; i < 300; i++){
    await sleep(700);
    if (!pending || pending.rid !== rid) return;   // event stream already delivered it
    try {
      const r = await fetch('/api/request/' + encodeURIComponent(rid));
      if (r.status === 404){ sys('⚠ request vanished'); finishPending(); return; }
      const j = await r.json();
      if (j.status === 'done'){ onAnswer(j.response, rid); return; }
      if (j.status === 'error'){ sys('⚠ ' + (j.error || 'failed')); finishPending(); return; }
      if (j.status === 'cancelled'){ sys('🛑 stopped'); finishPending(); return; }
    } catch (_) { /* transient: keep polling */ }
  }
  if (pending && pending.rid === rid){
    sys('⚠ No reply after ~200s — check the Harry console window.');
    finishPending();
  }
}

/* ---------------- microphone ---------------- */

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null, listening = false, spokenFinal = false;
let localCtx = null, localStream = null, localProc = null, localSrc = null;
let localChunks = [], localStart = 0, localVoiced = false, localSilence = 0, localRate = 16000;

function micReady(){
  if (!pending && !listening) setChip('ready', 'ready');
}

micBtn.addEventListener('click', function(){
  if (listening){ stopListening(); return; }
  startVoice();
});

micStopBtn.addEventListener('click', function(){
  if (listening && (rec || localCtx)){ stopVoice(true); return; }
  sys('Nothing is listening right now.');
});

function startVoice(){
  if (pending){ sys('Wait for the current reply, or press EMERGENCY STOP.'); return; }
  if (SR){ startBrowserRecognition(); return; }
  sys('This browser has no built-in speech recognition — using Harry’s local model instead.');
  startLocal();
}

function startBrowserRecognition(){
  try {
    rec = new SR();
    rec.lang = 'en-IN';
    rec.interimResults = true;
    rec.continuous = false;
    rec.maxAlternatives = 1;
    spokenFinal = false;
    rec.onstart = function(){
      listening = true;
      micStopBtn.hidden = false;
      setChip('listening', 'listening… tap ⏹ to stop');
    };
    rec.onresult = function(ev){
      let interim = '', fin = '';
      for (let i = ev.resultIndex; i < ev.results.length; i++){
        const r = ev.results[i];
        if (r.isFinal) fin += r[0].transcript; else interim += r[0].transcript;
      }
      if (interim) input.value = interim;
      if (fin.trim()){ spokenFinal = true; handleHeard(fin.trim()); }
    };
    rec.onerror = function(ev){
      listening = false;
      micStopBtn.hidden = true;
      const msgs = {
        'not-allowed': 'Microphone permission denied — type instead.',
        'service-not-allowed': 'Speech service blocked — type instead.',
        'no-speech': 'I didn’t hear anything. Try again.',
        'aborted': 'Listening cancelled.',
        'network': 'Browser speech service unavailable — switching to Harry’s local model.'
      };
      const msg = msgs[ev.error] || ('Voice error: ' + ev.error);
      setChip('error', msg);
      sys(msg);
      if (ev.error === 'network') startLocal();
    };
    rec.onend = function(){
      listening = false;
      micStopBtn.hidden = true;
      if (spokenFinal) setChip('processing', 'sending…');
      else micReady();
    };
    setChip('processing', 'waiting for microphone…');
    rec.start();
  } catch (e){
    sys('⚠ microphone failed: ' + humanError(e));
    setChip('error', 'microphone unavailable');
  }
}

function handleHeard(text){
  text = (text || '').trim();
  if (!text) return;
  input.value = '';
  if (document.getElementById('autosend').checked){
    submit(text, { voice: true });
  } else {
    input.value = text;
    input.focus();
  }
}

function stopListening(){
  if (rec){ try { rec.stop(); } catch (_) {} }
  listening = false;
  micStopBtn.hidden = true;
  micReady();
}

function stopVoice(send){
  if (localCtx || localStream) stopLocal(send);
  else stopListening();
}

async function startLocal(){
  // Warm the local Vosk model now; it loads while the user is still speaking.
  fetch('/api/stt/warm', { method: 'POST' }).catch(function(){});
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
    setChip('error', 'no microphone API');
    sys('This browser cannot access a microphone — type instead.');
    return;
  }
  setChip('processing', 'waiting for microphone…');
  try {
    localStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }
    });
  } catch (e){
    setChip('error', 'permission denied');
    sys('Microphone permission denied — type instead.');
    return;
  }
  const AC = window.AudioContext || window.webkitAudioContext;
  localCtx = new AC();
  localRate = localCtx.sampleRate || 48000;
  localSrc = localCtx.createMediaStreamSource(localStream);
  localProc = localCtx.createScriptProcessor(4096, 1, 1);
  localChunks = []; localVoiced = false; localSilence = 0;
  localStart = performance.now();
  localProc.onaudioprocess = function(ev){
    const inp = ev.inputBuffer.getChannelData(0);
    localChunks.push(new Float32Array(inp));
    let sum = 0;
    for (let i = 0; i < inp.length; i += 4) sum += inp[i] * inp[i];
    const rms = Math.sqrt(sum / (inp.length / 4));
    if (rms > 0.012){ localVoiced = true; localSilence = 0; }
    else if (localVoiced){ localSilence += inp.length / localRate; }
    if (localVoiced && localSilence > 1.2) stopLocal(true);
    else if ((performance.now() - localStart) / 1000 > 12) stopLocal(true);
  };
  localSrc.connect(localProc);
  localProc.connect(localCtx.destination);
  listening = true;
  micStopBtn.hidden = false;
  setChip('listening', 'recording… tap ⏹ when done');
}

function concatFloat(){
  let total = 0;
  for (let i = 0; i < localChunks.length; i++) total += localChunks[i].length;
  const out = new Float32Array(total);
  let off = 0;
  for (let i = 0; i < localChunks.length; i++){ out.set(localChunks[i], off); off += localChunks[i].length; }
  return out;
}

function resample16k(data, rate){
  if (!rate || Math.abs(rate - 16000) < 1){ return data; }
  const ratio = rate / 16000;
  const outLen = Math.floor(data.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++){
    const pos = i * ratio;
    const i0 = Math.floor(pos);
    const i1 = Math.min(i0 + 1, data.length - 1);
    const frac = pos - i0;
    out[i] = data[i0] * (1 - frac) + data[i1] * frac;
  }
  return out;
}

function toPCM16(f32){
  const out = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++){
    const s = Math.max(-1, Math.min(1, f32[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

async function stopLocal(send){
  listening = false;
  micStopBtn.hidden = true;
  if (localProc){ try { localProc.disconnect(); } catch (_) {} localProc = null; }
  if (localSrc){ try { localSrc.disconnect(); } catch (_) {} localSrc = null; }
  if (localStream){ try { localStream.getTracks().forEach(function(t){ t.stop(); }); } catch (_) {} localStream = null; }
  if (localCtx){ try { await localCtx.close(); } catch (_) {} localCtx = null; }
  const audio = concatFloat();
  localChunks = [];
  if (!send || !audio.length || !localVoiced){ micReady(); return; }
  setChip('processing', 'transcribing…');
  try {
    const pcm = toPCM16(resample16k(audio, localRate));
    const res = await fetch('/api/stt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: pcm.buffer
    });
    if (!res.ok){ setChip('error', 'transcription failed'); sys('⚠ speech service returned ' + res.status); return; }
    const j = await res.json();
    const text = (j.text || '').trim();
    if (!text){
      setChip('error', 'didn’t catch that');
      sys('I didn’t catch that — please repeat or type it.');
      return;
    }
    handleHeard(text);
  } catch (e){
    setChip('error', 'transcription failed');
    sys('⚠ ' + humanError(e));
  }
}

document.getElementById('stop').addEventListener('click', async function(){
  try { await fetch('/api/stop', { method: 'POST' }); } catch (_) {}
  finishPending();
  sys('EMERGENCY STOP pressed');
  setChip('ready', 'ready');
});

/* ---------------- events ---------------- */

const es = new EventSource('/api/stream');

es.addEventListener('assistant_message', function(e){
  let d = {};
  try { d = JSON.parse(e.data); } catch (_) {}
  onAnswer(d.text || '', d.request_id);
});

es.addEventListener('user_message', function(e){
  let d = {};
  try { d = JSON.parse(e.data); } catch (_) {}
  if (d.source === 'ui' || d.source === 'ui-voice') return;   // already rendered locally
  addMsg('user', '🎙️ ' + (d.text || ''), d.request_id ? 'u:' + d.request_id : null);
});

['task_status', 'task_planned', 'task_step_done', 'task_error', 'reminder_due',
 'daily_briefing', 'emergency_stop'].forEach(function(name){
  es.addEventListener(name, function(e){
    let d = {};
    try { d = JSON.parse(e.data); } catch (_) {}
    if (name === 'task_status') sys('task ' + d.status + ': ' + d.title);
    else if (name === 'task_planned') sys('plan: ' + (d.steps || []).join(' → '));
    else if (name === 'task_step_done') sys('✓ ' + (d.step || '') + (d.result ? ' → ' + d.result : ''));
    else if (name === 'task_error') sys('⚠ ' + d.error);
    else if (name === 'reminder_due') addMsg('harry', '⏰ ' + d.text);
    else if (name === 'daily_briefing') addMsg('harry', '☀️ ' + d.active_tasks + ' active tasks');
    else if (name === 'emergency_stop'){ sys('🛑 ' + d.reason); finishPending(); }
    scheduleRefresh();
  });
});

es.addEventListener('permission_request', function(e){
  let d = {};
  try { d = JSON.parse(e.data); } catch (_) {}
  const box = document.getElementById('confirmations');
  const card = document.createElement('div');
  card.className = 'confirm';
  const label = document.createElement('div');
  label.textContent = 'Permission needed: ' + (d.action || '');
  const yes = document.createElement('button');
  yes.textContent = 'Approve';
  const no = document.createElement('button');
  no.textContent = 'Deny';
  no.className = 'ghost';
  function answer(approve){
    fetch('/api/confirm/' + encodeURIComponent(d.id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approve: approve })
    });
    if (card.parentNode) card.parentNode.removeChild(card);
  }
  yes.addEventListener('click', function(){ answer(true); });
  no.addEventListener('click', function(){ answer(false); });
  card.appendChild(label); card.appendChild(yes); card.appendChild(no);
  box.appendChild(card);
});

es.addEventListener('permission_result', function(e){
  let d = {};
  try { d = JSON.parse(e.data); } catch (_) {}
  sys((d.allowed ? 'approved: ' : 'denied: ') + d.action);
});

es.onerror = function(){ /* EventSource retries automatically */ };

/* ---------------- sidebar state ---------------- */

function scheduleRefresh(){
  if (refreshTimer) return;
  refreshTimer = setTimeout(function(){ refreshTimer = null; refreshState(); }, 400);
}

async function refreshState(){
  try {
    const s = await (await fetch('/api/state')).json();
    const byId = {};
    (s.tasks || []).forEach(function(t){ byId[t.id] = t; });
    const list = Object.keys(byId).map(function(k){ return byId[k]; });
    document.getElementById('tasks').innerHTML = '';
    list.forEach(function(t){
      const d = document.createElement('div');
      d.className = 'card task ' + (t.status === 'COMPLETED' ? 'done' : (t.status === 'FAILED' ? 'failed' : ''));
      const b = document.createElement('b');
      b.textContent = (t.title || '').slice(0, 60);
      const pill = document.createElement('span');
      pill.className = 'pill';
      // Two tasks can legitimately share a title; the time tells them apart.
      let when = '';
      try { when = new Date(t.created_at * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}); } catch (_) {}
      pill.textContent = t.status + (when ? ' · ' + when : '');
      d.appendChild(b); d.appendChild(document.createElement('br')); d.appendChild(pill);
      document.getElementById('tasks').appendChild(d);
    });
    if (!list.length){
      const d = document.createElement('div'); d.className = 'card'; d.textContent = 'none';
      document.getElementById('tasks').appendChild(d);
    }
    document.getElementById('mem').textContent = JSON.stringify(s.memory);
    document.getElementById('llm').textContent =
      'local: ' + ((s.llm.local || []).join(', ') || 'none') + ' | online: ' + (s.llm.online ? 'yes' : 'no');
    document.getElementById('tools').textContent = (s.tools || []).join(', ');
    document.getElementById('toolcount').textContent = (s.tools || []).length;

    document.querySelectorAll('input[name=om]').forEach(function(r){
      r.checked = (r.value === s.output_mode);
    });
    if (!pending && !listening && chip.dataset.state !== 'error'){
      setChip(s.speaking ? 'speaking' : 'ready', s.speaking ? 'speaking…' : 'ready');
    }
  } catch (_) { /* state refresh is best effort */ }
}

document.querySelectorAll('input[name=om]').forEach(function(r){
  r.addEventListener('change', function(){
    if (!r.checked) return;
    fetch('/api/config/output_mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: r.value })
    });
    sys('output: ' + r.value);
  });
});

/* ---------------- voice settings ---------------- */

const voiceSel = document.getElementById('voiceSel');
const voiceMeta = document.getElementById('voiceMeta');
const rateInput = document.getElementById('rate');
const volInput = document.getElementById('vol');

function renderVoice(v){
  voiceSel.innerHTML = '';
  const genderPreference = document.getElementById('voiceGender');
  if (genderPreference && v.configured_gender) {
    genderPreference.value = v.configured_gender;
  }
  (v.voices || []).forEach(function(item){
    const o = document.createElement('option');
    o.value = item.name;
    o.textContent = item.name + (item.gender ? ' · ' + item.gender : '');
    o.selected = !!item.current;
    voiceSel.appendChild(o);
  });
  if (!(v.voices || []).length){
    const o = document.createElement('option');
    o.textContent = 'no installed voices found';
    voiceSel.appendChild(o);
  }
  voiceMeta.textContent = v.current
    ? ('speaking as: ' + v.current + ' (' + (v.current_gender || 'unknown') + ')')
    : 'engine default voice';
  rateInput.value = v.rate;
  volInput.value = Math.round((v.volume || 0) * 100);
  document.getElementById('rateVal').textContent = v.rate;
  document.getElementById('volVal').textContent = Math.round((v.volume || 0) * 100);
}

async function loadVoice(){
  try { renderVoice(await (await fetch('/api/voice')).json()); } catch (_) {}
}

async function saveVoice(body){
  try {
    const res = await fetch('/api/voice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (res.ok) renderVoice(await res.json());
    else sys('⚠ that voice setting was not accepted');
  } catch (e) { sys('⚠ ' + humanError(e)); }
}

/* Browsers restore form-control values on reload and can fire change without
   any user action. Sending those would overwrite the saved preference with a
   stale widget value, so preference writes require a real interaction. */
function onUserChange(el, handler){
  let touchedAt = 0;
  ['pointerdown', 'keydown'].forEach(function(name){
    el.addEventListener(name, function(){ touchedAt = Date.now(); });
  });
  el.addEventListener('change', function(e){
    if (Date.now() - touchedAt > 2000) return;
    touchedAt = 0;
    handler.call(el, e);
  });
}

onUserChange(voiceSel, function(){ saveVoice({ name: voiceSel.value }); });
onUserChange(document.getElementById('voiceGender'), function(){
  saveVoice({ gender: this.value, name: '' });
});
rateInput.addEventListener('input', function(){
  document.getElementById('rateVal').textContent = rateInput.value;
});
volInput.addEventListener('input', function(){
  document.getElementById('volVal').textContent = volInput.value;
});
onUserChange(rateInput, function(){
  document.getElementById('rateVal').textContent = rateInput.value;
  saveVoice({ rate: Number(rateInput.value) });
});
onUserChange(volInput, function(){
  document.getElementById('volVal').textContent = volInput.value;
  saveVoice({ volume: Number(volInput.value) / 100 });
});
document.getElementById('speakStop').addEventListener('click', function(){
  fetch('/api/speech/stop', { method: 'POST' }).then(function(res){
    if (!res.ok) throw new Error('Stop request failed (' + res.status + ')');
    sys('Speech stopped.');
    refreshState();
  }).catch(function(e){ sys('⚠ ' + humanError(e)); });
});

document.getElementById('voiceTest').addEventListener('click', async function(){
  this.disabled = true;
  try {
    const res = await fetch('/api/speech/test', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Voice test failed.');
    sys('Voice test started.');
  } catch (e) { sys('⚠ ' + humanError(e)); }
  finally { this.disabled = false; }
});

/* ---------------- dashboard preferences ---------------- */

const themeSel = document.getElementById('themeSel');
const languageSel = document.getElementById('languageSel');
function applyTheme(theme){
  document.documentElement.dataset.theme = theme;
  themeSel.value = theme;
}
async function postJSON(url, body){
  const res = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error(data.error || ('Request failed (' + res.status + ')'));
  return data;
}
async function loadPreferences(){
  try {
    const res = await fetch('/api/preferences');
    if (!res.ok) throw new Error('Could not load saved preferences.');
    const pref = await res.json();
    applyTheme(pref.theme || 'system');
    languageSel.value = pref.language || 'auto';
    return pref;
  } catch (e) {
    sys('⚠ ' + humanError(e));
    return { integrations: {} };
  }
}
onUserChange(themeSel, async function(){
  const previous = document.documentElement.dataset.theme || 'system';
  applyTheme(themeSel.value);
  try { await postJSON('/api/preferences', { theme: themeSel.value }); }
  catch (e) { applyTheme(previous); sys('⚠ ' + humanError(e)); }
});
onUserChange(languageSel, async function(){
  try {
    await postJSON('/api/preferences', { language: languageSel.value });
    sys('Response language preference saved.');
  } catch (e) { sys('⚠ ' + humanError(e)); }
});

/* ---------------- GitHub status and guarded dev operations ---------------- */

function textNode(tag, text, className){
  const el = document.createElement(tag);
  el.textContent = text == null ? '' : String(text);
  if (className) el.className = className;
  return el;
}
function setResult(id, text, isError){
  const el = document.getElementById(id);
  el.textContent = text || '';
  el.dataset.state = isError ? 'error' : 'ready';
}
function renderGithub(g){
  const summary = document.getElementById('githubSummary');
  summary.replaceChildren();
  const auth = g.github_cli || {};
  const authLabel = !auth.installed ? 'Not installed' :
    auth.checking ? 'Checking sign-in…' :
    auth.authenticated === true ? 'Authenticated' :
    auth.authenticated === false ? 'Sign-in required' : 'Status unavailable';
  [
    ['Repository', g.repository || 'Not detected'],
    ['Branch / HEAD', (g.branch || 'unknown') + ' · ' + (g.head || '').slice(0, 8)],
    ['Working tree', g.clean ? 'Clean' : (g.changed_files || []).length + ' changed file(s)'],
    ['GitHub CLI', authLabel]
  ].forEach(function(row){
    const card = textNode('div', '', 'info-card');
    card.appendChild(textNode('strong', row[0]));
    card.appendChild(textNode('span', row[1]));
    summary.appendChild(card);
  });
  if (g.branch === 'dev' && g.ahead !== null && g.ahead !== undefined){
    summary.appendChild(textNode('div', 'origin/dev: ' + g.ahead + ' ahead · ' +
      g.behind + ' behind', 'info-card'));
  }
  const changes = document.getElementById('changeList');
  changes.replaceChildren();
  (g.changed_files || []).forEach(function(file){
    const row = textNode('label', '', 'change-row');
    const check = document.createElement('input');
    check.type = 'checkbox'; check.value = file.path;
    row.appendChild(check);
    row.appendChild(textNode('span', (file.code || '??') + '  ' + file.path));
    changes.appendChild(row);
  });
  if (!(g.changed_files || []).length) changes.appendChild(textNode('div', 'No changed files.', 'muted'));
  const commits = document.getElementById('commitList');
  commits.replaceChildren();
  (g.commits || []).forEach(function(commit){
    commits.appendChild(textNode('div', (commit.hash || '').slice(0, 8) + '  ' + commit.subject));
  });
  if (!(g.commits || []).length) commits.appendChild(textNode('div', 'No commit history found.'));
  const onDev = g.branch === 'dev';
  ['githubCommit', 'githubBranch', 'githubPush'].forEach(function(id){
    document.getElementById(id).disabled = !onDev;
  });
}
function renderActivity(data){
  const root = document.getElementById('githubActivity');
  root.replaceChildren();
  if (!data || !data.available){
    root.textContent = (data && data.reason) || 'GitHub activity is unavailable.';
    return;
  }
  let count = 0;
  [['Issues', data.issues], ['Pull requests', data.pull_requests]].forEach(function(group){
    (group[1] || []).forEach(function(item){
      count++;
      const row = document.createElement('div');
      const label = '#' + item.number + ' ' + (item.title || '') + ' · ' + (item.state || '');
      if (typeof item.url === 'string' && /^https:\/\/github\.com\//i.test(item.url)){
        const link = document.createElement('a');
        link.className = 'activity-link'; link.href = item.url;
        link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = group[0] + ': ' + label;
        row.appendChild(link);
      } else row.textContent = group[0] + ': ' + label;
      root.appendChild(row);
    });
  });
  (data.errors || []).forEach(function(message){
    const error = document.createElement('p');
    error.className = 'error-text';
    error.textContent = message;
    root.appendChild(error);
  });
  if (!count && !(data.errors || []).length) root.textContent = 'No open issues or pull requests returned.';
}
async function refreshGithub(){
  setResult('githubResult', 'Refreshing repository and GitHub status…');
  try {
    const res = await fetch('/api/github/refresh', { method: 'POST' });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || 'Refresh failed.');
    renderGithub(data.github);
    const activity = await fetch('/api/github/activity');
    renderActivity(await activity.json());
    await refreshIntegrations(false);
    setResult('githubResult', 'Repository status refreshed.');
  } catch (e) { setResult('githubResult', humanError(e), true); }
}
document.getElementById('githubRefresh').addEventListener('click', refreshGithub);
document.getElementById('githubCommit').addEventListener('click', async function(){
  const paths = Array.from(document.querySelectorAll('#changeList input:checked')).map(function(x){ return x.value; });
  const message = document.getElementById('commitMessage').value.trim();
  if (!paths.length || !message){ setResult('githubResult', 'Select changed files and enter a commit message.', true); return; }
  if (!window.confirm('Commit ' + paths.length + ' selected file(s) on dev?')) return;
  this.disabled = true;
  try {
    const data = await postJSON('/api/github/commit', { paths: paths, message: message, approved: true });
    setResult('githubResult', 'Committed ' + data.commit + ' (' + (data.files || []).length + ' file(s)).');
    await refreshGithub();
  } catch (e) { setResult('githubResult', humanError(e), true); }
  finally { this.disabled = false; }
});
document.getElementById('githubBranch').addEventListener('click', async function(){
  const name = document.getElementById('branchName').value.trim();
  if (!name){ setResult('githubResult', 'Enter a branch name.', true); return; }
  if (!window.confirm('Create local branch "' + name + '" from dev?')) return;
  this.disabled = true;
  try {
    const data = await postJSON('/api/github/branch', { name: name, approved: true });
    setResult('githubResult', data.result + ' Branch: ' + data.branch);
    await refreshGithub();
  } catch (e) { setResult('githubResult', humanError(e), true); }
  finally { this.disabled = false; }
});
document.getElementById('githubPush').addEventListener('click', async function(){
  if (!window.confirm('Push the current dev commit to origin/dev using a normal fast-forward push?')) return;
  this.disabled = true;
  try {
    const data = await postJSON('/api/github/push', { approved: true });
    setResult('githubResult', 'Pushed ' + data.commit + ' to ' + data.remote + '.');
    await refreshGithub();
  } catch (e) { setResult('githubResult', humanError(e), true); }
  finally { this.disabled = false; }
});

/* ---------------- actual plugins and local agent CLIs ---------------- */

function integrationStatusText(item){
  if (item.status) return item.status;
  if (!item.installed) return 'not installed';
  return item.authenticated ? 'available' : 'not configured';
}
function renderIntegrations(data){
  const root = document.getElementById('pluginGrid');
  root.replaceChildren();
  const items = (data && data.items) || [];
  const categories = [];
  items.forEach(function(item){ if (!categories.includes(item.category)) categories.push(item.category); });
  categories.forEach(function(category){
    root.appendChild(textNode('div', category, 'integration-category'));
    items.filter(function(item){ return item.category === category; }).forEach(function(item){
      const card = textNode('article', '', 'integration-card');
      const head = textNode('div', '', 'integration-head');
      head.appendChild(textNode('span', item.name, 'integration-name'));
      const status = textNode('span', integrationStatusText(item), 'integration-status');
      status.dataset.status = integrationStatusText(item);
      head.appendChild(status);
      card.appendChild(head);
      card.appendChild(textNode('div', item.description || '', 'integration-desc'));
      const configText = item.configuration || item.setup ||
        (item.repository ? 'Repository: ' + item.repository : 'Configuration: local Harry registry');
      card.appendChild(textNode('div', configText, 'integration-desc'));
      const actions = textNode('div', '', 'integration-actions');
      if (item.config_key){
        const label = document.createElement('label');
        const check = document.createElement('input');
        check.type = 'checkbox'; check.checked = !!item.enabled;
        check.setAttribute('aria-label', 'Enable ' + item.name);
        onUserChange(check, async function(){
          check.disabled = true;
          try {
            await postJSON('/api/preferences', { integrations: { [item.config_key]: check.checked } });
            await refreshIntegrations(false);
          } catch (e){ check.checked = !check.checked; sys('⚠ ' + humanError(e)); }
          finally { check.disabled = false; }
        });
        label.appendChild(check); label.appendChild(document.createTextNode(check.checked ? 'Enabled' : 'Disabled'));
        actions.appendChild(label);
      } else {
        actions.appendChild(textNode('span', item.enabled ? 'Enabled' : 'Disabled'));
      }
      const last = item.last_execution
        ? item.last_execution.status + ' · ' +
          new Date(Number(item.last_execution.at) * 1000).toLocaleString() +
          (item.last_execution.summary ? ' · ' + item.last_execution.summary : '')
        : 'Not run since Harry started.';
      actions.appendChild(textNode('span', 'Last: ' + last));
      card.appendChild(actions);
      root.appendChild(card);
    });
  });
  if (!items.length) root.appendChild(textNode('div', 'No integrations are registered.', 'muted'));
  updateAgentAvailability(items);
  if (data && data.github) renderGithub(data.github);
}
function updateAgentAvailability(items){
  const select = document.getElementById('agentSel');
  Array.from(select.options).forEach(function(option){
    const agent = items.find(function(item){
      return (option.value === 'codex' && item.name === 'OpenAI Codex') ||
        (option.value === 'claude' && item.name === 'Anthropic Claude');
    });
    option.disabled = !agent || !agent.installed || !agent.authenticated || !agent.enabled;
    option.textContent = (option.value === 'codex' ? 'OpenAI Codex' : 'Anthropic Claude') +
      (agent ? ' · ' + integrationStatusText(agent) : ' · unavailable');
  });
}
async function refreshIntegrations(withActivity){
  if (integrationProbeTimer){ clearTimeout(integrationProbeTimer); integrationProbeTimer = null; }
  try {
    const res = await fetch('/api/integrations' + (withActivity ? '?activity=1' : ''));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not read integration status.');
    renderIntegrations(data);
    if (withActivity && data.github && data.github.activity) renderActivity(data.github.activity);
    const checking = (data.items || []).some(function(item){ return item.status === 'checking'; }) ||
      !!(data.github && data.github.github_cli && data.github.github_cli.checking);
    if (checking && integrationProbePolls < 10){
      integrationProbePolls++;
      integrationProbeTimer = setTimeout(function(){ refreshIntegrations(false); }, 1200);
    } else if (!checking) integrationProbePolls = 0;
  } catch (e){
    document.getElementById('pluginGrid').textContent = 'Integration status unavailable: ' + humanError(e);
  }
}
document.getElementById('integrationsRefresh').addEventListener('click', function(){ refreshIntegrations(true); });

/* ---------------- read-only agent delegation ---------------- */

const agentPrompt = document.getElementById('agentPrompt');
const agentResult = document.getElementById('agentResult');
function showAgentResult(text, error){
  agentResult.textContent = text;
  agentResult.dataset.state = error ? 'error' : 'ready';
}
function stopAgentPolling(){
  if (agentPollTimer){ clearTimeout(agentPollTimer); agentPollTimer = null; }
}
async function pollAgent(taskId){
  if (!activeAgentTask || activeAgentTask !== taskId) return;
  try {
    const res = await fetch('/api/agents/' + encodeURIComponent(taskId));
    const task = await res.json();
    if (!res.ok) throw new Error(task.error || 'Agent task disappeared.');
    showAgentResult((task.agent || 'Agent') + ' · ' + task.status +
      (task.response ? '\\n\\n' + task.response : task.error ? '\\n\\n' + task.error : ''),
      task.status === 'error' || task.status === 'timeout');
    if (task.status === 'running') agentPollTimer = setTimeout(function(){ pollAgent(taskId); }, 900);
    else {
      stopAgentPolling(); activeAgentTask = null;
      document.getElementById('delegateAgent').disabled = false;
      document.getElementById('cancelAgent').disabled = true;
      refreshIntegrations(false);
    }
  } catch (e){
    stopAgentPolling(); activeAgentTask = null;
    document.getElementById('delegateAgent').disabled = false;
    document.getElementById('cancelAgent').disabled = true;
    showAgentResult(humanError(e), true);
  }
}
document.getElementById('delegateAgent').addEventListener('click', async function(){
  const prompt = agentPrompt.value.trim();
  if (!prompt){ showAgentResult('Enter a task for the selected agent.', true); return; }
  this.disabled = true;
  showAgentResult('Starting read-only agent task…');
  try {
    const task = await postJSON('/api/agents', { agent: document.getElementById('agentSel').value, prompt: prompt });
    activeAgentTask = task.task.id;
    document.getElementById('cancelAgent').disabled = false;
    pollAgent(activeAgentTask);
  } catch (e){
    this.disabled = false;
    showAgentResult(humanError(e), true);
  }
});
document.getElementById('cancelAgent').addEventListener('click', async function(){
  if (!activeAgentTask) return;
  this.disabled = true;
  try {
    const data = await postJSON('/api/agents/' + encodeURIComponent(activeAgentTask) + '/cancel', {});
    showAgentResult('Agent task ' + data.task.status + '.');
    stopAgentPolling(); activeAgentTask = null;
    document.getElementById('delegateAgent').disabled = false;
  } catch (e){ showAgentResult(humanError(e), true); this.disabled = false; }
});

loadPreferences().then(function(){ refreshIntegrations(true); });
loadVoice();

addMsg('sys', 'Harry ready. Type, or press 🎤 to talk. Voice and text share this conversation.');
refreshState();
setInterval(refreshState, 1200);
</script>
</body>
</html>"""


# --------------------------------------------------------------------- VR / immersive
#
# A real WebXR client, not a mock-up: the button below requests an
# ``immersive-vr`` session with the ``dom-overlay`` feature, so the same DOM
# panels (conversation, tasks, voice state, emergency stop) are composited by the
# headset browser. On a device without WebXR (every normal desktop browser) the
# same page works as a large-format spatial dashboard and says clearly that no
# immersive session is available.
#
# Deliberately NOT claimed: a 3D avatar, 3D object rendering and hand tracking.
# Those need a 3D engine and asset pipeline; the integration point for them is
# this page plus the event bus, and they are listed as remaining work in
# docs/ANDROID.md and README.md.
#
# The page talks to the SAME endpoints as the desktop dashboard (/api/chat,
# /api/stream, /api/stop, /api/state), so it is a second view of one assistant
# rather than a parallel implementation.

_VR_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Harry — immersive view</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg:#070a12; --panel:rgba(23,30,46,.86); --acc:#4f8cff; --ok:#3ecf8e;
          --err:#ff6b6b; --tx:#eef2fa; --dim:#93a0b8; }
  * { box-sizing:border-box; margin:0; }
  body { background:radial-gradient(circle at 50% 20%,#12203a 0%,var(--bg) 60%);
         color:var(--tx); font:18px/1.55 'Segoe UI',system-ui,sans-serif;
         min-height:100vh; padding:18px; display:grid;
         grid-template-rows:auto 1fr auto; gap:14px; }
  header { display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
  h1 { font-size:22px; letter-spacing:.5px; }
  .spacer { flex:1; }
  .chip { border-radius:12px; padding:4px 12px; background:rgba(35,44,64,.9);
          color:var(--dim); font-size:13px; }
  .chip[data-state=ready] { color:var(--ok); }
  .chip[data-state=working] { color:#e0c060; }
  .chip[data-state=stopped] { color:var(--err); }
  main { display:grid; grid-template-columns:1.5fr 1fr; gap:14px; min-height:0; }
  section { background:var(--panel); border:1px solid #232c40; border-radius:16px;
            padding:14px; min-height:0; display:flex; flex-direction:column; }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:1px;
       color:var(--dim); margin-bottom:8px; }
  #log { flex:1; overflow-y:auto; padding-right:6px; }
  .msg { padding:10px 14px; border-radius:14px; margin:8px 0; white-space:pre-wrap;
         max-width:88%; font-size:17px; }
  .msg.user { background:var(--acc); margin-left:auto; }
  .msg.harry { background:#232c40; }
  .msg.sys { background:transparent; color:var(--dim); font-size:13px;
             text-align:center; max-width:100%; }
  .card { background:#0d1424; border:1px solid #232c40; border-radius:10px;
          padding:8px 10px; margin-bottom:6px; font-size:14px; }
  .card.done { border-left:3px solid var(--ok); }
  .card.failed { border-left:3px solid var(--err); }
  .card.active { border-left:3px solid var(--acc); }
  form { display:flex; gap:10px; padding-top:10px; }
  input[type=text] { flex:1; background:#0d1424; border:1px solid #232c40;
                     color:var(--tx); border-radius:12px; padding:14px; font-size:17px; }
  button { background:var(--acc); border:0; color:#fff; border-radius:12px;
           padding:13px 20px; font-size:16px; cursor:pointer; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button.ghost { background:#232c40; }
  #stop { background:var(--err); font-weight:700; }
  #xr { background:#1b3a63; }
  #xrnote { font-size:13px; color:var(--dim); margin-top:8px; }
  .confirm { border:1px solid #b8860b; background:#2a2410; padding:10px;
             border-radius:10px; margin-top:8px; font-size:14px; }
  .confirm button { padding:8px 14px; font-size:14px; margin-right:6px; }
  @media (max-width:900px) { main { grid-template-columns:1fr; } }
</style>
</head>
<body>
<header>
  <h1>🤖 Harry · immersive</h1>
  <span id="state" class="chip" data-state="ready">ready</span>
  <span id="mode" class="chip">output: both</span>
  <span class="spacer"></span>
  <button id="xr" class="ghost">Enter VR</button>
  <button id="stop">⏹ STOP</button>
  <a href="/" style="color:var(--acc);font-size:14px;align-self:center">desktop view →</a>
</header>
<main>
  <section>
    <h2>Conversation</h2>
    <div id="log"></div>
    <div id="confirmations"></div>
    <form id="f">
      <input type="text" id="t" autocomplete="off"
             placeholder="Ask Harry… (English / తెలుగు / हिंदी / mixed)">
      <button type="submit">Send</button>
    </form>
  </section>
  <section>
    <h2>Tasks</h2>
    <div id="tasks" style="overflow-y:auto;flex:1">—</div>
    <h2 style="margin-top:12px">Memory</h2>
    <div id="mem" class="card">—</div>
    <div id="xrnote">Checking WebXR support…</div>
  </section>
</main>
<script>
const logEl = document.getElementById('log');
const stateChip = document.getElementById('state');
let pendingRid = null;
const seen = new Set();

function setState(s, text) {
  stateChip.dataset.state = s;
  stateChip.textContent = text;
}

function addMsg(kind, text) {
  const d = document.createElement('div');
  d.className = 'msg ' + kind;
  d.textContent = text;
  logEl.appendChild(d);
  logEl.scrollTop = logEl.scrollHeight;
  return d;
}

function send(text) {
  text = (text || '').trim();
  if (!text) return;
  addMsg('user', text);
  pendingRid = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));
  setState('working', 'thinking…');
  fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: text, request_id: pendingRid })
  }).catch(function(){
    setState('stopped', 'offline');
    addMsg('sys', 'Could not reach Harry. Is the assistant still running?');
  });
}

document.getElementById('f').addEventListener('submit', function(e){
  e.preventDefault();
  const input = document.getElementById('t');
  send(input.value);
  input.value = '';
});

document.getElementById('stop').addEventListener('click', function(){
  fetch('/api/stop', { method: 'POST' });
  addMsg('sys', 'Emergency stop requested.');
  setState('stopped', 'stopped');
});

// One answer per request id: the event stream is the only answer transport here.
function onAssistant(data) {
  if (data.id && seen.has(data.id)) return;
  if (data.id) seen.add(data.id);
  if (pendingRid && data.request_id && data.request_id !== pendingRid) return;
  addMsg('harry', data.text || '');
  pendingRid = null;
  setState('ready', 'ready');
}

const es = new EventSource('/api/stream');
es.addEventListener('assistant_message', function(m){ onAssistant(JSON.parse(m.data)); });
es.addEventListener('user_message', function(m){
  const d = JSON.parse(m.data);
  if (d.source === 'voice') addMsg('user', d.text + '  (voice)');
});
es.addEventListener('task_status', function(m){
  const d = JSON.parse(m.data);
  if (d.status && d.status !== 'COMPLETED' && d.status !== 'FAILED') {
    setState('working', d.status.toLowerCase().replace(/_/g, ' '));
  }
});
es.addEventListener('task_error', function(m){
  const d = JSON.parse(m.data);
  addMsg('sys', 'Error: ' + (d.error || 'unknown'));
});
es.addEventListener('permission_request', function(m){
  const d = JSON.parse(m.data);
  const box = document.getElementById('confirmations');
  const card = document.createElement('div');
  card.className = 'confirm';
  card.textContent = '[' + (d.level || 'sensitive') + '] Approval needed: ' +
                     d.action + ' ';
  function answer(approve) {
    if (d.id) {
      fetch('/api/confirm/' + encodeURIComponent(d.id), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approve: approve })
      });
    }
    card.remove();
  }
  const yes = document.createElement('button');
  yes.textContent = 'Approve';
  const no = document.createElement('button');
  no.textContent = 'Deny';
  no.className = 'ghost';
  yes.addEventListener('click', function(){ answer(true); });
  no.addEventListener('click', function(){ answer(false); });
  card.appendChild(yes); card.appendChild(no);
  box.appendChild(card);
});

async function refresh() {
  try {
    const s = await (await fetch('/api/state')).json();
    const tasks = document.getElementById('tasks');
    tasks.innerHTML = '';
    (s.tasks || []).forEach(function(t){
      const d = document.createElement('div');
      const cls = t.status === 'COMPLETED' ? 'done'
                : (t.status === 'FAILED' ? 'failed' : 'active');
      d.className = 'card ' + cls;
      d.textContent = (t.title || '').slice(0, 70) + ' — ' + t.status;
      tasks.appendChild(d);
    });
    if (!(s.tasks || []).length) tasks.textContent = 'no tasks yet';
    document.getElementById('mem').textContent =
      (s.memory.longterm_items || 0) + ' memories · ' +
      (s.memory.conversation_turns || 0) + ' turns';
    document.getElementById('mode').textContent = 'output: ' + s.output_mode;
    if (!pendingRid) setState(s.speaking ? 'working' : 'ready',
                              s.speaking ? 'speaking…' : 'ready');
  } catch (_) {}
}

// ---- WebXR: a real session request, with an honest fallback ----
const xrBtn = document.getElementById('xr');
const xrNote = document.getElementById('xrnote');

(async function initXR() {
  if (!navigator.xr) {
    xrNote.textContent = 'No WebXR in this browser. This page still works as a ' +
                         'large-format dashboard; open it in a headset browser ' +
                         'for an immersive session.';
    xrBtn.disabled = true;
    return;
  }
  let ok = false;
  try { ok = await navigator.xr.isSessionSupported('immersive-vr'); } catch (_) {}
  if (!ok) {
    xrNote.textContent = 'WebXR present, but no immersive-vr device is connected. ' +
                         'Connect a headset (or a WebXR runtime) to enter VR.';
    xrBtn.disabled = true;
    return;
  }
  xrNote.textContent = 'Immersive VR is available on this device.';
  xrBtn.addEventListener('click', async function(){
    try {
      const session = await navigator.xr.requestSession('immersive-vr', {
        optionalFeatures: ['local-floor', 'dom-overlay'],
        domOverlay: { root: document.body }
      });
      xrNote.textContent = 'Immersive session active. Take off the headset or ' +
                           'press Stop to end it.';
      session.addEventListener('end', function(){
        xrNote.textContent = 'Immersive session ended.';
      });
      if (!session.domOverlayState) {
        xrNote.textContent = 'Session started without DOM overlay support: the ' +
                             'headset shows an empty scene, so use the desktop view.';
      }
    } catch (e) {
      xrNote.textContent = 'Could not start the immersive session: ' + e.message;
    }
  });
})();

addMsg('sys', 'Immersive view ready. Voice and text share this conversation with the desktop.');
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""

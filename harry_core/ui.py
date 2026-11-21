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
            "speaking": bool(brain.speaking),
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
<html lang="en">
<head>
<meta charset="utf-8">
<title>Harry</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg:#0f1420; --panel:#171e2e; --acc:#4f8cff; --ok:#3ecf8e; --err:#ff6b6b; --tx:#e8ecf4; --dim:#8a94a8; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--tx); font:15px/1.5 'Segoe UI',system-ui,sans-serif; display:flex; height:100vh; }
  aside { width:300px; background:var(--panel); padding:16px; overflow-y:auto; flex-shrink:0; }
  main { flex:1; display:flex; flex-direction:column; min-width:0; }
  h1 { font-size:18px; margin-bottom:12px; } h2 { font-size:12px; text-transform:uppercase; color:var(--dim); margin:16px 0 6px; }
  .card { background:#111827; border:1px solid #232c40; border-radius:8px; padding:10px; margin-bottom:8px; font-size:13px; }
  .task { border-left:3px solid var(--acc); }
  .task.done { border-left-color:var(--ok); } .task.failed { border-left-color:var(--err); }
  #log { flex:1; overflow-y:auto; padding:20px; }
  .msg { max-width:75%; padding:10px 14px; border-radius:12px; margin:6px 0; white-space:pre-wrap; }
  .user { background:var(--acc); margin-left:auto; } .harry { background:#232c40; }
  .sys { color:var(--dim); font-size:12px; text-align:center; }
  .typing { opacity:.75; font-style:italic; }
  form { display:flex; gap:8px; padding:14px 20px 4px; border-top:1px solid #232c40; }
  input[type=text] { flex:1; background:#111827; border:1px solid #232c40; color:var(--tx); border-radius:8px; padding:12px; font-size:15px; }
  button { background:var(--acc); border:0; color:#fff; border-radius:8px; padding:10px 16px; cursor:pointer; font-size:14px; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button.ghost { background:#232c40; }
  #mic { background:#232c40; font-size:18px; padding:10px 14px; }
  #mic.on { background:var(--err); }
  #stop { background:var(--err); font-weight:bold; width:100%; }
  .confirm { border:1px solid #b8860b; background:#2a2410; padding:10px; margin:6px 20px; border-radius:8px; }
  .confirm button { margin-right:6px; }
  .pill { display:inline-block; background:#232c40; border-radius:10px; padding:1px 8px; font-size:11px; color:var(--dim); }
  #statusbar { display:flex; align-items:center; gap:12px; padding:4px 20px 12px; font-size:12px; color:var(--dim); }
  .chip { border-radius:10px; padding:2px 10px; background:#232c40; color:var(--dim); }
  .chip[data-state=ready] { color:var(--ok); }
  .chip[data-state=listening] { background:var(--err); color:#fff; }
  .chip[data-state=processing] { background:#2a2410; color:#e0c060; }
  .chip[data-state=speaking] { background:#10331f; color:var(--ok); }
  .chip[data-state=error] { background:#3a1414; color:var(--err); }
</style>
</head>
<body>
<aside>
  <h1>🤖 Harry</h1>
  <p style="font-size:12px;margin-bottom:10px"><a href="/vr" style="color:var(--acc)">Open VR / immersive view →</a></p>
  <button id="stop">⏹ EMERGENCY STOP</button>
  <h2>Current tasks</h2><div id="tasks"></div>
  <h2>Memory</h2><div id="mem" class="card">-</div>
  <h2>AI models</h2><div id="llm" class="card">-</div>
  <h2>Tools (<span id="toolcount">0</span>)</h2><div id="tools" class="card" style="font-size:11px;color:var(--dim)"></div>
  <h2>Output</h2>
  <div id="om">
    <label><input type="radio" name="om" value="both"> text+voice</label><br>
    <label><input type="radio" name="om" value="text"> text only</label><br>
    <label><input type="radio" name="om" value="voice"> voice only</label>
  </div>
  <h2>Voice</h2>
  <div class="card">
    <select id="voiceSel" style="width:100%;background:#0f1420;color:var(--tx);border:1px solid #232c40;border-radius:6px;padding:6px"></select>
    <div id="voiceMeta" style="font-size:11px;color:var(--dim);margin:6px 0">-</div>
    <label style="display:block;font-size:12px">speed <span id="rateVal">170</span>
      <input id="rate" type="range" min="120" max="240" step="5" style="width:100%"></label>
    <label style="display:block;font-size:12px">volume <span id="volVal">100</span>%
      <input id="vol" type="range" min="0" max="100" step="5" style="width:100%"></label>
    <button id="speakStop" class="ghost" style="width:100%;margin-top:6px">⏹ Stop speaking</button>
  </div>
</aside>
<main>
  <div id="log"></div>
  <div id="confirmations"></div>
  <form id="f">
    <button id="mic" type="button" title="Speak to Harry">🎤</button>
    <input type="text" id="t" placeholder="Talk to Harry... (English / తెలుగు / हिंदी / mixed)" autocomplete="off">
    <button id="send" type="submit">Send</button>
    <button id="micstop" type="button" class="ghost" hidden>⏹</button>
  </form>
  <div id="statusbar">
    <span id="micstate" class="chip" data-state="ready">ready</span>
    <label><input type="checkbox" id="autosend" checked> auto-send spoken text</label>
    <span id="hint"></span>
  </div>
</main>
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

voiceSel.addEventListener('change', function(){ saveVoice({ name: voiceSel.value }); });
rateInput.addEventListener('change', function(){
  document.getElementById('rateVal').textContent = rateInput.value;
  saveVoice({ rate: Number(rateInput.value) });
});
volInput.addEventListener('change', function(){
  document.getElementById('volVal').textContent = volInput.value;
  saveVoice({ volume: Number(volInput.value) / 100 });
});
document.getElementById('speakStop').addEventListener('click', function(){
  fetch('/api/speech/stop', { method: 'POST' });
  sys('speech stopped');
});

loadVoice();

addMsg('sys', 'Harry ready. Type, or press 🎤 to talk. Voice and text share this conversation.');
refreshState();
setInterval(refreshState, 15000);
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

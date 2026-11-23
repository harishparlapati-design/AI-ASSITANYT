"""Image and video generation tests.

Run:  .venv/Scripts/python.exe tests/media_test.py

The adapters are exercised end to end against small local stand-in servers
that speak the same HTTP contracts as Automatic1111, ComfyUI and Replicate.
That proves the request/response handling, storage, history and deletion
without pretending a hosted GPU provider exists. A real local diffusers run is
attempted only when its weights are present, so the suite stays fast offline.
"""

import base64
import io
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="harry-media-")
os.environ["HARRY_MEDIA_DIR"] = _tmp

from PIL import Image  # noqa: E402

from harry_core import config, media  # noqa: E402

PASS, FAIL = 0, 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {label}")
    else:
        FAIL += 1
        print(f"FAIL {label} {detail}")


def png_bytes(color=(30, 120, 200), size=(64, 64)):
    image = Image.new("RGB", size, color)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00\x00\x00\x00mp42isom" + b"\x00" * 512
PNG = png_bytes()


class FakeHandler(BaseHTTPRequestHandler):
    delay = 0.0

    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/sdapi/v1/sd-models"):
            return self._json([{"title": "fake-model"}])
        if self.path.startswith("/system_stats"):
            return self._json({"system": {"os": "fake"}})
        if self.path.startswith("/video.mp4"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(MP4)))
            self.end_headers()
            self.wfile.write(MP4)
            return
        if self.path.startswith("/predictions/"):
            return self._json({"id": "pred1", "status": "succeeded",
                               "output": [f"http://127.0.0.1:{self.server.server_port}/video.mp4"]})
        if self.path.startswith("/image.png"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
            return
        return self._json({"detail": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.startswith("/sdapi/v1/txt2img"):
            if FakeHandler.delay:
                time.sleep(FakeHandler.delay)
            if not payload.get("prompt"):
                return self._json({"error": "prompt required"}, 400)
            return self._json({"images": [base64.b64encode(PNG).decode()]})
        if self.path.startswith("/predictions"):
            return self._json({"id": "pred1", "status": "succeeded",
                               "output": [f"http://127.0.0.1:{self.server.server_port}/video.mp4"]})
        return self._json({"detail": "not found"}, 404)


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def wait_job(client, job_id, timeout=60):
    state = None
    for _ in range(int(timeout * 10)):
        state = client.get(f"/api/jobs/{job_id}").get_json()
        if state["state"] in ("done", "error", "cancelled"):
            return state
        time.sleep(0.1)
    return state or {}


def configure(server):
    base = f"http://127.0.0.1:{server.server_port}"
    config.CONFIG["media"]["image"].update({
        "provider": "automatic1111", "automatic1111_url": base,
        "comfyui_workflow": "", "openai_model": "",
    })
    config.CONFIG["media"]["video"].update({
        "provider": "auto", "replicate_url": base,
        "replicate_version": "fake-version", "fal_model": "",
    })
    config.CONFIG["media"]["allow_local_downloads"] = True
    os.environ["REPLICATE_API_TOKEN"] = "test-token"
    media.status(force=True)


def test_status_and_guards(server):
    state = media.status(force=True)
    check("image provider detected", "automatic1111" in state["image"]["providers"], state["image"])
    check("video provider detected", "replicate" in state["video"]["providers"], state["video"])
    check("status reports no hint when ready", not state["image"]["hint"], state["image"]["hint"])

    config.CONFIG["media"]["allow_local_downloads"] = False
    target = f"http://127.0.0.1:{server.server_port}/image.png"
    blocked = False
    try:
        media.safe_download(target, 10 ** 6)
    except ValueError:
        blocked = True
    resolved = media.safe_download(target, 10 ** 6, allow_local=True)
    config.CONFIG["media"]["allow_local_downloads"] = True
    check("private-network downloads blocked by default", blocked)
    check("explicit local download is allowed for a local provider",
          resolved[:4] == b"\x89PNG", resolved[:8])

    try:
        media.safe_download("file:///etc/passwd", 100)
        bad_scheme = False
    except ValueError:
        bad_scheme = True
    check("unsupported scheme blocked", bad_scheme)


def test_image_job(server):
    from harry_core.ui import create_app
    client = create_app().test_client()

    listing = client.get("/api/media").get_json()
    check("GET /api/media lists providers", "providers" in listing, list(listing))

    missing = client.post("/api/media/image", json={"prompt": ""})
    check("empty prompt rejected", missing.status_code == 400, missing.status_code)

    long_prompt = client.post("/api/media/image", json={"prompt": "x" * 2500})
    check("overlong prompt rejected", long_prompt.status_code == 413
          or long_prompt.status_code == 400, long_prompt.status_code)

    started = client.post("/api/media/image", json={
        "prompt": "a blue square on a white background", "size": 512, "seed": 7})
    check("image generation accepted", started.status_code == 202, started.status_code)
    if started.status_code != 202:
        return
    state = wait_job(client, started.get_json()["job"]["id"])
    check("image job finished", state.get("state") == "done", state)
    result = state.get("result") or {}
    item_id = result.get("id")
    check("image job returned an id", bool(item_id), result)
    record = media.get(item_id)
    check("image file stored", record and os.path.exists(record["path"]), record)
    with open(record["path"], "rb") as f:
        check("stored file is a real PNG", f.read(8) == b"\x89PNG\r\n\x1a\n")
    check("stored bytes match", record["bytes"] == len(PNG), record["bytes"])

    served = client.get(f"/api/media/{item_id}/file")
    check("image served", served.status_code == 200, served.status_code)
    check("image mime correct", served.headers.get("Content-Type") == "image/png",
          served.headers.get("Content-Type"))
    check("image is not sniffed", served.headers.get("X-Content-Type-Options") == "nosniff")
    served.close()          # release the file handle before deleting on Windows

    again = client.post(f"/api/media/{item_id}/regenerate", json={})
    check("regenerate accepted", again.status_code == 202, again.status_code)
    if again.status_code == 202:
        second = wait_job(client, again.get_json()["job"]["id"])
        check("regenerate finished with a new item",
              second.get("state") == "done"
              and (second.get("result") or {}).get("id") != item_id, second.get("state"))
        check("history has both generations", len(media.listing(kind="image")) >= 2,
              len(media.listing(kind="image")))

    check("unknown media is 404", client.get("/api/media/image_nope/file").status_code in (400, 404))
    deleted = client.post(f"/api/media/{item_id}/delete", json={})
    check("delete accepted", deleted.status_code == 200, deleted.status_code)
    check("file removed from disk", record and not os.path.exists(record["path"]))
    check("deleted media is gone", client.get(f"/api/media/{item_id}/file").status_code == 404)


def test_video_job(server):
    from harry_core.ui import create_app
    client = create_app().test_client()
    started = client.post("/api/media/video", json={
        "prompt": "waves rolling onto a beach at sunset", "seconds": 4, "aspect": "16:9"})
    check("video generation accepted", started.status_code == 202, started.status_code)
    if started.status_code != 202:
        return
    state = wait_job(client, started.get_json()["job"]["id"])
    check("video job finished", state.get("state") == "done", state)
    item_id = (state.get("result") or {}).get("id")
    record = media.get(item_id) if item_id else None
    check("video stored as mp4", record and record["mime"] == "video/mp4", record)
    served = client.get(f"/api/media/{item_id}/file")
    check("video served", served.status_code == 200, served.status_code)
    check("video is a real container", served.data[4:8] == b"ftyp", served.data[:12])
    check("video supports range requests", served.headers.get("Accept-Ranges") == "bytes",
          served.headers.get("Accept-Ranges"))


def test_cancel(server):
    from harry_core.ui import create_app
    client = create_app().test_client()
    FakeHandler.delay = 3.0
    try:
        started = client.post("/api/media/image", json={"prompt": "a slow render"})
        job_id = started.get_json()["job"]["id"]
        time.sleep(0.4)
        before = len(media.listing(kind="image"))
        cancelled = client.post(f"/api/jobs/{job_id}/cancel", json={})
        check("cancel accepted", cancelled.status_code == 200, cancelled.status_code)
        state = wait_job(client, job_id, timeout=30)
        check("slow job ends cancelled", state.get("state") == "cancelled", state.get("state"))
        check("cancelled job stored nothing", len(media.listing(kind="image")) == before)
    finally:
        FakeHandler.delay = 0.0


def test_unavailable_is_honest():
    from harry_core.ui import create_app
    client = create_app().test_client()
    image_cfg = dict(config.CONFIG["media"]["image"])
    config.CONFIG["media"]["image"].update({
        "provider": "auto", "automatic1111_url": "http://127.0.0.1:1",
        "comfyui_workflow": "", "openai_model": "", "diffusers_model": "data/models/does-not-exist",
    })
    try:
        media.status(force=True)
        response = client.post("/api/media/image", json={"prompt": "anything"})
        body = response.get_json()
        check("unavailable image provider returns 503", response.status_code == 503, response.status_code)
        check("unavailable response carries a hint", bool(body.get("hint")), body)
        tool = media.generate_image_tool({"prompt": "anything"})
        check("image tool refuses without a provider", tool.get("ok") is False, tool)
    finally:
        config.CONFIG["media"]["image"].update(image_cfg)
        media.status(force=True)


def test_diffusers_if_ready():
    if not media._diffusers_ready():
        print("     (skipped real diffusers run: local weights are not present)")
        return
    from harry_core.jobs import JobManager
    manager = JobManager(path=os.path.join(_tmp, "jobs.json"))
    previous = config.CONFIG["media"]["image"].get("provider")
    config.CONFIG["media"]["image"]["provider"] = "diffusers"

    def run(job):
        return media.generate_image(job, {"prompt": "a red cube on white background",
                                          "size": 512, "steps": 2})

    started = time.time()
    job = manager.submit("image", "real local image", run)
    for _ in range(1200):
        state = manager.get(job["id"])
        if state.state in ("done", "error", "cancelled"):
            break
        time.sleep(0.5)
    elapsed = time.time() - started
    config.CONFIG["media"]["image"]["provider"] = previous
    check("real diffusers generation succeeded", state.state == "done",
          getattr(state, "error", ""))
    if state.state == "done":
        record = media.get(state.result["id"])
        with open(record["path"], "rb") as f:
            check("diffusers wrote a PNG", f.read(8) == b"\x89PNG\r\n\x1a\n")
        print(f"     local CPU image latency: {elapsed:.1f}s")


def test_panel_present():
    from harry_core.ui import create_app
    html = create_app().test_client().get("/").get_data(as_text=True)
    for element in ("id=\"mediaPrompt\"", "id=\"mediaGenerate\"", "id=\"mediaList\"",
                    "id=\"tabVideo\"", "id=\"mediaCancel\""):
        check(f"dashboard has {element}", element in html)


def main():
    server = start_server()
    try:
        configure(server)
        test_status_and_guards(server)
        test_image_job(server)
        test_video_job(server)
        test_cancel(server)
        test_unavailable_is_honest()
        test_panel_present()
        test_diffusers_if_ready()
    finally:
        server.shutdown()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

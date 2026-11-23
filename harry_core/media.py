"""Image and video generation.

Adapters, chosen by configuration, never by assumption:

images
  diffusers        in-process Stable Diffusion (sd-turbo) on CPU - free, slow
  automatic1111    a local A1111/Forge server (POST /sdapi/v1/txt2img)
  comfyui          a local ComfyUI server with a workflow template
  openai           an OpenAI-compatible images endpoint (needs an API key env)

videos
  replicate        Replicate prediction API (needs REPLICATE_API_TOKEN)
  fal              fal.ai model endpoint (needs FAL_KEY)
  comfyui          a ComfyUI workflow that produces a video file

Nothing here fabricates pixels. If no provider is configured the call fails
with a message that names the exact environment variable or install step, and
the dashboard shows the same state. Provider-returned download URLs are fetched
with a public-address guard so a hostile response cannot reach the local
network.
"""

import base64
import importlib.util
import io
import ipaddress
import json
import logging
import os
import random
import socket
import threading
import time
import uuid
from urllib.parse import urlparse

from . import config, runtime_paths

log = logging.getLogger("harry.media")

IMAGE_EXT = ("png", "jpg", "webp")
VIDEO_EXT = ("mp4", "webm", "mov")
MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_VIDEO_BYTES = 200 * 1024 * 1024

_lock = threading.RLock()
_index_cache = {"path": None, "items": {}}
_status_cache = {"ts": 0.0, "value": None}
_pipe = {"pipeline": None, "id": None}
_pipe_lock = threading.Lock()


# ------------------------------------------------------------------ config

def _image_cfg():
    return (config.CONFIG.get("media", {}) or {}).get("image", {}) or {}


def _video_cfg():
    return (config.CONFIG.get("media", {}) or {}).get("video", {}) or {}


def media_root():
    base = os.environ.get("HARRY_MEDIA_DIR", "").strip()
    if base:
        return os.path.abspath(base)
    return os.path.join(runtime_paths.workspace_root(), "data", "media")


def _index_path():
    return os.path.join(media_root(), "index.json")


def _load_index():
    path = _index_path()
    with _lock:
        if _index_cache["path"] == path:
            return _index_cache["items"]
        items = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw.get("items"), dict):
                items = raw["items"]
        except Exception:
            items = {}
        _index_cache["path"] = path
        _index_cache["items"] = items
        return items


def _save_index(items):
    os.makedirs(media_root(), exist_ok=True)
    tmp = _index_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _index_path())
    with _lock:
        _index_cache["path"] = _index_path()
        _index_cache["items"] = items


# ------------------------------------------------------------------ storage

def _extension(data, kind):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data[4:8] == b"ftyp":
        return "mp4", "video/mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm", "video/webm"
    raise ValueError("the provider returned data that is not an image or video")


def _store(data, kind, prompt, meta=None):
    limit = MAX_VIDEO_BYTES if kind == "video" else MAX_IMAGE_BYTES
    if len(data) > limit:
        raise ValueError(f"generated file exceeds {limit // (1024 * 1024)} MB")
    ext, mime = _extension(data, kind)
    allowed = VIDEO_EXT if kind == "video" else IMAGE_EXT
    if ext not in allowed:
        raise ValueError(f"expected a {kind}, got .{ext}")
    item_id = f"{kind}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    folder = os.path.join(media_root(), kind + "s")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{item_id}.{ext}")
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(folder):
        raise ValueError("refusing to write outside the media folder")
    with open(path, "wb") as f:
        f.write(data)
    record = {
        "id": item_id, "kind": kind, "path": path, "bytes": len(data),
        "mime": mime, "prompt": str(prompt or "")[:2000],
        "meta": dict(meta or {}), "created": time.time(),
        "name": f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}.{ext}",
        "owner": "local",
    }
    items = _load_index()
    items[item_id] = record
    _save_index(items)
    return record


def listing(kind=None, limit=30):
    items = list(_load_index().values())
    if kind:
        items = [i for i in items if i.get("kind") == kind]
    items.sort(key=lambda i: i.get("created", 0), reverse=True)
    return items[:max(1, min(int(limit or 30), 200))]


def get(item_id, owner=None):
    record = _load_index().get(str(item_id or ""))
    if not record:
        return None
    if owner and record.get("owner") != owner:
        return None
    root = os.path.abspath(media_root())
    path = os.path.abspath(record.get("path") or "")
    if not path.startswith(root + os.sep) or not os.path.exists(path):
        return None
    return record


def delete(item_id):
    record = get(item_id)
    if not record:
        return {"ok": False, "error": "unknown media"}
    try:
        os.unlink(record["path"])
    except OSError as e:
        # A file still open in another response, or a permission problem, must
        # not be reported as deleted: the caller would believe the data is gone.
        log.warning("could not delete media file: %s", e)
        return {"ok": False, "error": f"the file could not be removed: {e}"}
    items = _load_index()
    items.pop(record["id"], None)
    _save_index(items)
    return {"ok": True, "deleted": record["id"]}


# ------------------------------------------------------------------ providers

def _probe(url, path="/", timeout=2.0):
    import requests
    try:
        response = requests.get(str(url).rstrip("/") + path, timeout=timeout)
        return response.status_code < 500
    except Exception:
        return False


def _diffusers_ready():
    if importlib.util.find_spec("diffusers") is None or importlib.util.find_spec("torch") is None:
        return False
    model = _local_model_id()
    if not model:
        return False
    if os.path.isdir(model):
        # A directory alone is not a usable model: a partial download leaves
        # model_index.json behind but no weights. Require the three files a
        # text-to-image pipeline actually needs.
        for component in ("unet", "vae", "text_encoder"):
            folder = os.path.join(model, component)
            if not os.path.isdir(folder):
                return False
            weights = [f for f in os.listdir(folder)
                       if f.endswith((".safetensors", ".bin"))]
            if not weights:
                return False
        return os.path.exists(os.path.join(model, "model_index.json"))
    # A Hugging Face id is only usable once its weights are cached locally.
    cache = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    marker = os.path.join(cache, "hub", "models--" + model.replace("/", "--"))
    return os.path.isdir(marker)


def _local_model_id():
    raw = (os.environ.get("HARRY_SD_MODEL") or _image_cfg().get("diffusers_model")
           or "stabilityai/sd-turbo")
    candidate = raw if os.path.isabs(raw) else os.path.join(runtime_paths.workspace_root(), raw)
    if os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, "model_index.json")):
        return candidate
    return raw


def _online_key(cfg, name):
    env = str(cfg.get(name) or "")
    return os.environ.get(env, "") if env else ""


def status(force=False):
    now = time.time()
    if not force and _status_cache["value"] is not None and now - _status_cache["ts"] < 15:
        return dict(_status_cache["value"])
    image_cfg = _image_cfg()
    video_cfg = _video_cfg()

    diffusers = _diffusers_ready()
    a1111_url = str(image_cfg.get("automatic1111_url") or "http://127.0.0.1:7860")
    a1111 = _probe(a1111_url, "/sdapi/v1/sd-models", timeout=1.5)
    comfy_url = str(image_cfg.get("comfyui_url") or "http://127.0.0.1:8188")
    comfy = _probe(comfy_url, "/system_stats", timeout=1.5)
    openai_key = _online_key(image_cfg, "openai_api_key_env")
    openai_model = image_cfg.get("openai_model") or ""

    replicate_key = _online_key(video_cfg, "replicate_api_key_env")
    fal_key = _online_key(video_cfg, "fal_api_key_env")

    image_providers = []
    if diffusers:
        image_providers.append("diffusers")
    if a1111:
        image_providers.append("automatic1111")
    if comfy and image_cfg.get("comfyui_workflow"):
        image_providers.append("comfyui")
    if openai_key and openai_model:
        image_providers.append("openai")

    video_providers = []
    if replicate_key and video_cfg.get("replicate_version"):
        video_providers.append("replicate")
    if fal_key and video_cfg.get("fal_model"):
        video_providers.append("fal")
    if comfy and video_cfg.get("comfyui_workflow"):
        video_providers.append("comfyui")

    result = {
        "image": {
            "available": bool(image_providers),
            "providers": image_providers,
            "preferred": image_providers[0] if image_providers else "",
            "local_model": _local_model_id(),
            "details": {
                "diffusers": {"ready": diffusers, "model": _local_model_id()},
                "automatic1111": {"ready": a1111, "url": a1111_url},
                "comfyui": {"ready": comfy, "url": comfy_url,
                            "workflow": bool(image_cfg.get("comfyui_workflow"))},
                "openai": {"ready": bool(openai_key), "model": openai_model,
                           "key_env": image_cfg.get("openai_api_key_env") or ""},
            },
            "hint": "" if image_providers else
                    ("Install the free local stack: pip install diffusers transformers accelerate, "
                     "then set HARRY_SD_MODEL to a cached model such as stabilityai/sd-turbo. "
                     "A local Automatic1111 or ComfyUI server is also detected automatically."),
        },
        "video": {
            "available": bool(video_providers),
            "providers": video_providers,
            "preferred": video_providers[0] if video_providers else "",
            "details": {
                "replicate": {"ready": bool(replicate_key),
                              "version": bool(video_cfg.get("replicate_version"))},
                "fal": {"ready": bool(fal_key), "model": video_cfg.get("fal_model") or ""},
                "comfyui": {"ready": comfy and bool(video_cfg.get("comfyui_workflow"))},
            },
            "hint": "" if video_providers else
                    ("No local CPU video model is practical. Configure a hosted provider "
                     "(REPLICATE_API_TOKEN plus media.video.replicate_version, or FAL_KEY) "
                     "to enable real video generation; that is a paid service and needs approval."),
        },
        "max": {"image_bytes": MAX_IMAGE_BYTES, "video_bytes": MAX_VIDEO_BYTES,
                "video_seconds": int(video_cfg.get("max_seconds") or 10)},
    }
    _status_cache.update(ts=now, value=result)
    return dict(result)


def _choose(kind_cfg, kind):
    requested = str(kind_cfg.get("provider") or "auto").strip().lower()
    state = status(force=True)[kind]
    if requested and requested != "auto":
        if requested not in state["providers"]:
            raise RuntimeError(
                f"{requested} is configured for {kind} but is not ready. "
                + (state["hint"] or "Check its URL or API key."))
        return requested
    if not state["providers"]:
        raise RuntimeError(state["hint"] or f"no {kind} provider is configured")
    return state["providers"][0]


# ------------------------------------------------------------------ safety

def _is_public_host(host):
    try:
        addresses = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for entry in addresses:
        try:
            ip = ipaddress.ip_address(entry[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return False
    return True


def safe_download(url, max_bytes, allow_local=False, timeout=180):
    """Fetch a provider result, refusing local/private targets unless allowed."""
    import requests
    allow_local = allow_local or bool(
        (config.CONFIG.get("media", {}) or {}).get("allow_local_downloads"))
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("provider returned an unsupported download URL")
    if not allow_local and not _is_public_host(parsed.hostname):
        raise ValueError("provider returned a private-network download URL")
    response = requests.get(url, timeout=timeout, stream=True, allow_redirects=False)
    if response.status_code in (301, 302, 303, 307, 308):
        target = response.headers.get("Location", "")
        response.close()
        return safe_download(target, max_bytes, allow_local=allow_local, timeout=timeout)
    response.raise_for_status()
    chunks, total = [], 0
    for chunk in response.iter_content(65536):
        total += len(chunk)
        if total > max_bytes:
            response.close()
            raise ValueError("provider file is larger than the allowed limit")
        chunks.append(chunk)
    response.close()
    return b"".join(chunks)


# ------------------------------------------------------------------ image providers

def _load_diffusers(model_id):
    import torch
    from diffusers import StableDiffusionPipeline
    pipe = StableDiffusionPipeline.from_pretrained(
        model_id, torch_dtype=torch.float32,
        safety_checker=None, requires_safety_checker=False,
    )
    pipe = pipe.to("cpu")
    try:
        pipe.enable_attention_slicing()
    except Exception:
        pass
    return pipe


def _generate_image_diffusers(job, prompt, size, steps, seed, negative):
    import torch
    model_id = _local_model_id()
    with _pipe_lock:
        if _pipe["pipeline"] is None or _pipe["id"] != model_id:
            job.update(progress=5, message="loading the local image model")
            _pipe["pipeline"] = _load_diffusers(model_id)
            _pipe["id"] = model_id
        pipe = _pipe["pipeline"]
    job.update(progress=12, message="generating image")
    generator = torch.Generator(device="cpu").manual_seed(int(seed))

    def on_step(pipe_obj, step, timestep, kwargs):
        job.update(progress=min(90, 15 + int(step / max(1, steps) * 70)),
                   message=f"step {step}/{steps}")
        job.check_cancelled()
        return kwargs

    result = pipe(prompt=prompt, negative_prompt=negative or None,
                  num_inference_steps=steps, guidance_scale=0.0,
                  width=size, height=size, generator=generator,
                  callback_on_step_end=on_step)
    image = result.images[0]
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), {"provider": "diffusers", "model": model_id,
                               "seed": int(seed), "steps": steps, "size": size}


def _generate_image_a1111(job, prompt, size, steps, seed, negative):
    import requests
    url = str(_image_cfg().get("automatic1111_url") or "http://127.0.0.1:7860").rstrip("/")
    job.update(progress=15, message="asking the local A1111 server")
    response = requests.post(f"{url}/sdapi/v1/txt2img", json={
        "prompt": prompt, "negative_prompt": negative or "",
        "width": size, "height": size, "steps": steps, "seed": int(seed),
        "sampler_name": _image_cfg().get("sampler") or "Euler a",
    }, timeout=600)
    response.raise_for_status()
    images = response.json().get("images") or []
    if not images:
        raise RuntimeError("the A1111 server returned no image")
    job.update(progress=90, message="decoding image")
    data = base64.b64decode(images[0].split(",")[-1])
    return data, {"provider": "automatic1111", "seed": int(seed), "steps": steps, "size": size}


def _generate_image_openai(job, prompt, size, steps, seed, negative):
    import requests
    cfg = _image_cfg()
    key_env = cfg.get("openai_api_key_env") or "OPENAI_API_KEY"
    key = os.environ.get(key_env, "")
    base = str(cfg.get("openai_url") or "https://api.openai.com/v1").rstrip("/")
    model = cfg.get("openai_model") or ""
    if not (key and model):
        raise RuntimeError(f"set {key_env} and media.image.openai_model to use the hosted image API")
    job.update(progress=15, message="requesting the hosted image model")
    response = requests.post(f"{base}/images/generations", timeout=300,
                             headers={"Authorization": f"Bearer {key}"},
                             json={"model": model, "prompt": prompt,
                                   "size": cfg.get("openai_size") or "1024x1024",
                                   "n": 1})
    response.raise_for_status()
    payload = response.json()
    entry = (payload.get("data") or [{}])[0]
    job.update(progress=85, message="downloading the result")
    if entry.get("b64_json"):
        data = base64.b64decode(entry["b64_json"])
    elif entry.get("url"):
        data = safe_download(entry["url"], MAX_IMAGE_BYTES)
    else:
        raise RuntimeError("the image API returned no image")
    return data, {"provider": "openai", "model": model, "seed": int(seed)}


def _run_comfyui(job, workflow, prompt, seed, kind, timeout=900):
    """Submit a ComfyUI workflow with {{PROMPT}}/{{SEED}} tokens replaced."""
    import requests
    url = str((_image_cfg() if kind == "image" else _video_cfg())
              .get("comfyui_url") or "http://127.0.0.1:8188").rstrip("/")
    text = json.dumps(workflow).replace("{{PROMPT}}", json.dumps(prompt)[1:-1])
    text = text.replace("{{SEED}}", str(int(seed)))
    payload = json.loads(text)
    job.update(progress=15, message="queued on ComfyUI")
    created = requests.post(f"{url}/prompt", json={"prompt": payload}, timeout=60)
    created.raise_for_status()
    prompt_id = created.json().get("prompt_id")
    if not prompt_id:
        raise RuntimeError("ComfyUI did not return a prompt id")
    started = time.time()
    while time.time() - started < timeout:
        job.check_cancelled()
        history = requests.get(f"{url}/history/{prompt_id}", timeout=30).json()
        entry = history.get(prompt_id)
        if entry:
            for node in (entry.get("outputs") or {}).values():
                for key in ("images", "gifs", "videos"):
                    for item in node.get(key) or []:
                        params = {"filename": item.get("filename"),
                                  "subfolder": item.get("subfolder", ""),
                                  "type": item.get("type", "output")}
                        blob = requests.get(f"{url}/view", params=params, timeout=120).content
                        return blob, {"provider": "comfyui", "prompt_id": prompt_id,
                                      "seed": int(seed)}
        elapsed = time.time() - started
        job.update(progress=min(90, 20 + int(elapsed)), message="rendering")
        time.sleep(3)
    raise RuntimeError("ComfyUI did not finish within the time limit")


def generate_image(job, spec):
    """Job body for image generation."""
    prompt = str(spec.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("a prompt is required")
    provider = _choose(_image_cfg(), "image")
    size = int(spec.get("size") or _image_cfg().get("size") or 512)
    size = 512 if size <= 512 else (768 if size <= 768 else 1024)
    steps = max(1, min(int(spec.get("steps") or _image_cfg().get("steps") or 4), 50))
    seed = int(spec.get("seed") or random.randint(1, 2 ** 31 - 1))
    negative = str(spec.get("negative") or "")
    job.update(progress=3, message=f"provider: {provider}")
    if provider == "diffusers":
        data, meta = _generate_image_diffusers(job, prompt, size, steps, seed, negative)
    elif provider == "automatic1111":
        data, meta = _generate_image_a1111(job, prompt, size, steps, seed, negative)
    elif provider == "openai":
        data, meta = _generate_image_openai(job, prompt, size, steps, seed, negative)
    elif provider == "comfyui":
        path = _image_cfg().get("comfyui_workflow")
        with open(path, "r", encoding="utf-8") as f:
            workflow = json.load(f)
        s = int(spec.get("size") or 512)
        if isinstance(workflow, dict) and "__size__" in workflow:
            s = int(workflow.pop("__size__") or s)
        data, meta = _run_comfyui(job, workflow, prompt, seed, "image")
    else:
        raise RuntimeError(f"unsupported image provider {provider}")
    job.check_cancelled()
    record = _store(data, "image", prompt,
                    {**meta, "negative": negative, "requested_size": size})
    return {"ok": True, "id": record["id"], "kind": "image",
            "download": f"/api/media/{record['id']}/file",
            "bytes": record["bytes"], "mime": record["mime"],
            "meta": record["meta"], "name": record["name"]}


# ------------------------------------------------------------------ video providers

def _generate_video_replicate(job, prompt, seconds, aspect):
    import requests
    cfg = _video_cfg()
    key_env = cfg.get("replicate_api_key_env") or "REPLICATE_API_TOKEN"
    key = os.environ.get(key_env, "")
    version = cfg.get("replicate_version") or ""
    if not (key and version):
        raise RuntimeError(f"set {key_env} and media.video.replicate_version")
    base = str(cfg.get("replicate_url") or "https://api.replicate.com/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Prefer": "wait=5"}
    job.update(progress=10, message="creating a Replicate prediction")
    created = requests.post(f"{base}/predictions", headers=headers, timeout=60, json={
        "version": version,
        "input": {"prompt": prompt, "duration": seconds, "aspect_ratio": aspect},
    })
    created.raise_for_status()
    prediction = created.json()
    poll_url = (prediction.get("urls") or {}).get("get")
    started = time.time()
    while prediction.get("status") in ("starting", "processing") and time.time() - started < 900:
        job.check_cancelled()
        time.sleep(3)
        polled = requests.get(poll_url, headers={"Authorization": f"Bearer {key}"}, timeout=60)
        polled.raise_for_status()
        prediction = polled.json()
        elapsed = time.time() - started
        job.update(progress=min(88, 12 + int(elapsed / 4)), message=f"status: {prediction.get('status')}")
    if prediction.get("status") != "succeeded":
        raise RuntimeError(f"Replicate prediction {prediction.get('status')}: "
                           f"{prediction.get('error') or 'no detail'}")
    output = prediction.get("output")
    url = output[-1] if isinstance(output, list) and output else output
    if not isinstance(url, str):
        raise RuntimeError("Replicate returned no video URL")
    job.update(progress=92, message="downloading video")
    data = safe_download(url, MAX_VIDEO_BYTES)
    return data, {"provider": "replicate", "prediction": prediction.get("id"),
                  "seconds": seconds, "aspect": aspect}


def _generate_video_fal(job, prompt, seconds, aspect):
    import requests
    cfg = _video_cfg()
    key_env = cfg.get("fal_api_key_env") or "FAL_KEY"
    key = os.environ.get(key_env, "")
    model = cfg.get("fal_model") or ""
    if not (key and model):
        raise RuntimeError(f"set {key_env} and media.video.fal_model")
    base = str(cfg.get("fal_url") or "https://fal.run").rstrip("/")
    job.update(progress=10, message="submitting to fal")
    created = requests.post(f"{base}/{model}", timeout=120,
                            headers={"Authorization": f"Key {key}",
                                     "Content-Type": "application/json"},
                            json={"prompt": prompt, "duration": seconds,
                                  "aspect_ratio": aspect})
    created.raise_for_status()
    payload = created.json()
    url = (payload.get("video") or {}).get("url") if isinstance(payload.get("video"), dict) else None
    if not url:
        outputs = payload.get("videos") or payload.get("output") or []
        if isinstance(outputs, list) and outputs:
            first = outputs[0]
            url = first.get("url") if isinstance(first, dict) else first
    if not isinstance(url, str) or not url:
        raise RuntimeError("fal returned no video URL")
    job.update(progress=80, message="downloading video")
    data = safe_download(url, MAX_VIDEO_BYTES)
    return data, {"provider": "fal", "model": model, "seconds": seconds, "aspect": aspect}


def generate_video(job, spec):
    prompt = str(spec.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("a prompt is required")
    provider = _choose(_video_cfg(), "video")
    seconds = max(1, min(int(spec.get("seconds") or 4), int(_video_cfg().get("max_seconds") or 10)))
    aspect = str(spec.get("aspect") or "16:9")
    job.update(progress=3, message=f"provider: {provider}")
    if provider == "replicate":
        data, meta = _generate_video_replicate(job, prompt, seconds, aspect)
    elif provider == "fal":
        data, meta = _generate_video_fal(job, prompt, seconds, aspect)
    elif provider == "comfyui":
        path = _video_cfg().get("comfyui_workflow")
        with open(path, "r", encoding="utf-8") as f:
            workflow = json.load(f)
        data, meta = _run_comfyui(job, workflow, prompt,
                                  random.randint(1, 2 ** 31 - 1), "video")
    else:
        raise RuntimeError(f"unsupported video provider {provider}")
    job.check_cancelled()
    record = _store(data, "video", prompt, {**meta, "requested_seconds": seconds})
    return {"ok": True, "id": record["id"], "kind": "video",
            "download": f"/api/media/{record['id']}/file",
            "bytes": record["bytes"], "mime": record["mime"],
            "meta": record["meta"], "name": record["name"]}


# ------------------------------------------------------------------ tools

def generate_image_tool(args):
    from .jobs import get_jobs
    if not status()["image"]["available"]:
        return {"ok": False, "error": status()["image"]["hint"]}
    spec = {k: args.get(k) for k in ("prompt", "size", "steps", "seed", "negative")
            if args.get(k) is not None}
    job = get_jobs().submit("image", f"image · {str(args.get('prompt'))[:60]}",
                            lambda j: generate_image(j, spec), meta={"kind": "image"})
    return {"ok": True, "job": job, "message": "Image generation started as a background job."}


def generate_video_tool(args):
    from .jobs import get_jobs
    state = status()["video"]
    if not state["available"]:
        return {"ok": False, "error": state["hint"]}
    spec = {k: args.get(k) for k in ("prompt", "seconds", "aspect")
            if args.get(k) is not None}
    job = get_jobs().submit("video", f"video · {str(args.get('prompt'))[:60]}",
                            lambda j: generate_video(j, spec), meta={"kind": "video"})
    return {"ok": True, "job": job, "message": "Video generation started as a background job."}


def register_media_tools(registry):
    from .tools import Tool
    from .security import SAFE
    registry.register(Tool(
        "generate_image", "Create a real image from a prompt (needs a configured provider)",
        generate_image_tool, SAFE,
        {"prompt": "str", "size": "int", "steps": "int", "seed": "int", "negative": "str"},
    ))
    registry.register(Tool(
        "generate_video", "Create a real video from a prompt (needs a hosted provider)",
        generate_video_tool, SAFE,
        {"prompt": "str", "seconds": "int", "aspect": "str"},
    ))

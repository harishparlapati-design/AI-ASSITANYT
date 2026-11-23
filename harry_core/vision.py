"""Provider-independent vision.

Two kinds of provider are supported behind one interface:

* ``ollama``  - a local vision model (moondream, llava, minicpm-v, ...). This is
  the development and offline path; it costs nothing and needs no key.
* ``openai_compatible`` - any hosted chat-completions endpoint that accepts an
  image URL. Configured with an env-var *name* (never a key in source).

Frames are processed in memory only: nothing is written to disk here. Callers
that need to keep an image (an explicit screenshot tool) do that themselves.

OCR is separate and optional: it is used only when a tesseract binary is
actually installed, and the result says so.
"""

import base64
import hashlib
import io
import logging
import os
import threading
import time

from . import config

log = logging.getLogger("harry.vision")

MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_SIDE = 1280
JPEG_QUALITY = 85
_CACHE_SIZE = 24

# Model families that accept images through Ollama.
VISION_FAMILIES = (
    "moondream", "llava", "bakllava", "minicpm-v", "minicpm",
    "qwen2.5vl", "qwen2-vl", "qwen2vl", "gemma3", "mistral-small3",
    "llama3.2-vision", "granite3.2-vision", "phi3.5-vision",
)

_lock = threading.Lock()
_call_lock = threading.Lock()          # one model call at a time on CPU
_cache = {}
_cache_order = []
_status_cache = {"ts": 0.0, "value": None}
STATUS_TTL = 10.0                      # seconds; probing Ollama is not free


# ------------------------------------------------------------------ helpers

def _local_url():
    return str(config.CONFIG.get("models", {}).get("local", {}).get("url")
               or "http://localhost:11434").rstrip("/")


def installed_models(timeout=4):
    import requests
    try:
        response = requests.get(f"{_local_url()}/api/tags", timeout=timeout)
        response.raise_for_status()
        return [m.get("name", "") for m in response.json().get("models", [])]
    except Exception:
        return []


def vision_models(models=None):
    """Installed models that can look at images."""
    names = models if models is not None else installed_models()
    found = []
    for name in names:
        family = name.split(":")[0].lower()
        if any(marker in family for marker in VISION_FAMILIES):
            found.append(name)
    return found


def pick_model():
    """Configured model if it is installed, else the best installed one."""
    configured = str(config.CONFIG.get("models", {}).get("vision_model") or "").strip()
    names = installed_models()
    if configured:
        if not names or configured in names or any(n.split(":")[0] == configured for n in names):
            return configured
    candidates = vision_models(names)
    return candidates[0] if candidates else ""


def ocr_available():
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def status(force=False):
    """What the dashboard and tools can honestly say about vision right now.

    The Ollama probe is cached briefly: dashboard polling must not turn into a
    stream of HTTP requests that slow down real frame analysis.
    """
    now = time.time()
    cached = _status_cache["value"]
    if not force and cached is not None and now - _status_cache["ts"] < STATUS_TTL:
        return dict(cached)
    names = installed_models()
    local_models = vision_models(names)
    model = pick_model()
    online = config.CONFIG.get("models", {}).get("online", {}) or {}
    online_ready = bool(
        online.get("type") == "openai_compatible" and online.get("url")
        and online.get("model") and os.environ.get(online.get("api_key_env") or "")
    )
    provider = "ollama" if model else ("openai_compatible" if online_ready else "none")
    available = bool(model) or online_ready
    result = {
        "available": available,
        "provider": provider,
        "model": model,
        "installed_vision_models": local_models,
        "installed_models": sorted(names),
        "ollama_reachable": bool(names),
        "ocr": ocr_available(),
        "hint": "" if available else "No vision model found. Install one with: ollama pull moondream",
        "max_frame_bytes": MAX_INPUT_BYTES,
    }
    _status_cache.update(ts=now, value=result)
    return dict(result)


def _prepare(image_bytes):
    """Decode, downscale and re-encode a frame. Returns (jpeg_bytes, meta)."""
    from PIL import Image

    if not image_bytes:
        raise ValueError("empty image")
    if len(image_bytes) > MAX_INPUT_BYTES:
        raise ValueError(f"image is larger than {MAX_INPUT_BYTES // (1024 * 1024)} MB")
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except Exception:
        raise ValueError("not a readable image (PNG/JPEG/WebP expected)")
    original = image.size
    image = image.convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue(), {"original": original, "sent": image.size}


def _cache_key(jpeg, question):
    digest = hashlib.sha256(jpeg).hexdigest()
    return hashlib.sha256((digest + "\x00" + question).encode("utf-8")).hexdigest()


def _cache_get(key):
    with _lock:
        return _cache.get(key)


def _cache_put(key, value):
    with _lock:
        if key not in _cache:
            _cache_order.append(key)
        _cache[key] = value
        while len(_cache_order) > _CACHE_SIZE:
            _cache.pop(_cache_order.pop(0), None)


# ------------------------------------------------------------------ providers

def _analyze_ollama(jpeg, question, model, timeout):
    import requests
    payload = {
        "model": model,
        "prompt": question,
        "images": [base64.b64encode(jpeg).decode("ascii")],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 380},
    }
    response = requests.post(f"{_local_url()}/api/generate", json=payload,
                             timeout=timeout)
    response.raise_for_status()
    text = str(response.json().get("response") or "").strip()
    if not text:
        # Small models sometimes answer an unusual question with nothing. One
        # retry with a plain prompt is a real attempt, not a fabricated answer.
        payload["prompt"] = "Describe this image and transcribe any text in it."
        retry = requests.post(f"{_local_url()}/api/generate", json=payload,
                              timeout=timeout)
        retry.raise_for_status()
        text = str(retry.json().get("response") or "").strip()
    if not text:
        raise RuntimeError("the vision model returned an empty answer")
    return text


def _analyze_online(jpeg, question, timeout):
    import requests
    online = config.CONFIG.get("models", {}).get("online", {}) or {}
    url = str(online.get("url") or "").rstrip("/")
    model = str(online.get("vision_model") or online.get("model") or "")
    key_name = str(online.get("api_key_env") or "")
    key = os.environ.get(key_name, "") if key_name else ""
    if not (url and model and key):
        raise RuntimeError("no hosted vision provider is configured")
    data_uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    response = requests.post(
        f"{url}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "max_tokens": 500, "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }]},
        timeout=timeout,
    )
    response.raise_for_status()
    choices = response.json().get("choices") or []
    if not choices:
        raise RuntimeError("the hosted provider returned no choices")
    text = str(choices[0].get("message", {}).get("content") or "").strip()
    if not text:
        raise RuntimeError("the hosted provider returned an empty answer")
    return text


def ocr(image_bytes):
    """Best-effort local OCR. Returns (text, method) or (None, None)."""
    if not ocr_available():
        return None, None
    try:
        import pytesseract
        from PIL import Image
        image = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(image).strip()
        return (text or None), "tesseract"
    except Exception as e:
        log.info("ocr failed: %s", e)
        return None, None


DEFAULT_QUESTION = (
    "Describe what is visible in this screenshot. Read every error message, "
    "dialog, menu label and piece of text verbatim, then say what the user is "
    "most likely looking at."
)


def analyze(image_bytes, question=DEFAULT_QUESTION, timeout=180, allow_ocr=True):
    """Look at one image. Never raises for provider problems: reports them."""
    started = time.perf_counter()
    question = str(question or DEFAULT_QUESTION).strip()[:1500] or DEFAULT_QUESTION
    try:
        jpeg, meta = _prepare(image_bytes)
    except ValueError as e:
        return {"ok": False, "error": str(e), "kind": "bad_image"}

    key = _cache_key(jpeg, question)
    cached = _cache_get(key)
    if cached:
        return {**cached, "cached": True,
                "ms": round((time.perf_counter() - started) * 1000)}

    model = pick_model()
    errors = []
    if model:
        try:
            with _call_lock:
                text = _analyze_ollama(jpeg, question, model, timeout)
            result = {"ok": True, "text": text, "method": f"ollama:{model}",
                      "model": model, "provider": "ollama", "image": meta,
                      "ms": round((time.perf_counter() - started) * 1000)}
            if allow_ocr and ocr_available():
                text_ocr, how = ocr(jpeg)
                if text_ocr:
                    result["ocr_text"] = text_ocr
                    result["ocr_method"] = how
            _cache_put(key, result)
            return result
        except Exception as e:
            log.warning("local vision failed: %s", e)
            errors.append(f"local model {model}: {e}")

    online_cfg = config.CONFIG.get("models", {}).get("online", {}) or {}
    if online_cfg.get("type") == "openai_compatible":
        try:
            text = _analyze_online(jpeg, question, timeout)
            result = {"ok": True, "text": text, "method": "openai_compatible",
                      "model": online_cfg.get("vision_model") or online_cfg.get("model"),
                      "provider": "online", "image": meta,
                      "ms": round((time.perf_counter() - started) * 1000)}
            _cache_put(key, result)
            return result
        except Exception as e:
            errors.append(f"hosted provider: {e}")

    if allow_ocr:
        text_ocr, how = ocr(jpeg)
        if text_ocr:
            return {"ok": True, "text": text_ocr, "method": f"ocr:{how}",
                    "provider": "local", "ocr_text": text_ocr,
                    "ocr_method": how, "image": meta,
                    "note": "Only text was extracted; no vision model was used.",
                    "ms": round((time.perf_counter() - started) * 1000)}

    hint = ("Install a free local vision model: ollama pull moondream"
            if not model else "The vision model is unavailable right now.")
    return {"ok": False, "error": "no vision provider could answer",
            "details": errors, "hint": hint,
            "ms": round((time.perf_counter() - started) * 1000)}


def analyze_file(path, question=DEFAULT_QUESTION, timeout=180):
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_INPUT_BYTES + 1)
    except Exception as e:
        return {"ok": False, "error": f"cannot read image: {e}"}
    return analyze(data, question=question, timeout=timeout)

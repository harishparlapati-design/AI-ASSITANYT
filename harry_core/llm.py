"""Model provider abstraction and routing layer.

Harry never depends on one specific model. Tasks are routed by class:

  CHAT      - simple conversation, quick answers            (fast/local)
  REASONING - planning, decomposition, error analysis       (strongest available)
  CODING    - code generation / modification                (coding model)
  ROUTING   - intent detection / classification             (tiny, low latency)
  VISION    - image / screenshot understanding              (vision model)

Providers:
  - Ollama (local, offline)  - always attempted first
  - OpenAI-compatible HTTP   - optional, only if configured via env var name

Offline-first: if the local provider fails and no online provider is
configured/available, calls raise a clear ProviderError that callers handle
gracefully (e.g. planner falls back to rule-based planning).
"""

import json
import logging
import os
import re
import time
import urllib.request

from . import config

log = logging.getLogger("harry.llm")

CHAT, REASONING, CODING, ROUTING, VISION = (
    "chat", "reasoning", "coding", "routing", "vision"
)


class ProviderError(Exception):
    pass


# --------------------------------------------------------------------------- providers

class OllamaProvider:
    """Local Ollama server. Fully offline capable."""

    def __init__(self, url=None):
        self.url = (url or config.CONFIG["models"]["local"]["url"]).rstrip("/")

    def available(self):
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=2) as r:
                data = json.loads(r.read().decode())
                return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    def has_model(self, name):
        if not name:
            return False
        models = self.available()
        return any(m == name or m.split(":")[0] == name.split(":")[0] for m in models)

    def chat(self, model, messages, options=None, json_mode=False, _retry=0):
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": "30m",
        }
        opts = dict(options or {})
        # "think" is a provider directive, not a model option
        if "think" in opts:
            payload["think"] = bool(opts.pop("think"))
        if json_mode:
            payload["format"] = "json"
            # Thinking models (e.g. qwen3) burn the entire token budget on the
            # hidden 'thinking' field before emitting JSON. Disable thinking
            # natively and give the call headroom.
            payload["think"] = False
            opts.setdefault("num_predict", 600)
        if opts:
            payload["options"] = opts
        req = urllib.request.Request(
            self.url + "/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        started = time.perf_counter()
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read().decode())
        # Timing evidence for every model call (the console and logs show what
        # the prompt actually cost versus generation).
        log.info("llm %s: prompt %s tok, generated %s tok, total %.1fs",
                 model, data.get("prompt_eval_count"), data.get("eval_count"),
                 time.perf_counter() - started)
        msg = data.get("message") or {}
        content = msg.get("content", "") or ""
        # strip <think> blocks some models inline into content
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        thinking = msg.get("thinking") or ""
        if json_mode:
            # If content carries no JSON but the thinking text does, prefer it.
            if not re.search(r"[\[{]", content) and re.search(r"[\[{]", thinking):
                content = thinking
        if not content:
            # Thinking model exhausted its budget inside 'thinking'. Retry once
            # with thinking disabled so the budget goes to the answer.
            if _retry < 1:
                no_think = dict(options or {})
                no_think["think"] = False
                if no_think.get("num_predict", 0) < 400:
                    no_think["num_predict"] = 400
                return self.chat(model, messages, options=no_think, json_mode=json_mode,
                                 _retry=1)
            raise ProviderError("empty response from local model")
        return content


class OpenAICompatibleProvider:
    """Optional online provider (OpenAI-compatible /chat/completions)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.url = (cfg.get("url") or "").rstrip("/")
        self.model = cfg.get("model") or ""

    def available(self):
        return bool(self.cfg.get("type") == "openai_compatible" and self.url and self.model)

    def chat(self, model, messages, options=None, json_mode=False):
        if not self.available():
            raise ProviderError("online provider not configured")
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.cfg.get("api_key_env") or "", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {"model": model or self.model, "messages": messages}
        if options and "num_predict" in options:
            payload["max_tokens"] = options["num_predict"]
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            self.url + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode())
        content = data["choices"][0]["message"]["content"]
        if not content:
            raise ProviderError("empty response from online model")
        return content


# --------------------------------------------------------------------------- router

class ModelRouter:
    def __init__(self):
        self.local = OllamaProvider()
        self.online = OpenAICompatibleProvider(config.CONFIG["models"]["online"])
        self._local_models = None
        self._checked_at = 0.0

    # -- availability ------------------------------------------------------

    def local_models(self, refresh=False):
        now = time.time()
        if self._local_models is None or refresh or now - self._checked_at > 30:
            self._local_models = self.local.available()
            self._checked_at = now
        return self._local_models

    def online_available(self):
        try:
            return self.online.available()
        except Exception:
            return False

    def status(self):
        return {
            "local": self.local_models(),
            "online": self.online_available(),
        }

    # -- model selection ---------------------------------------------------

    def _pick(self, task_class, json_mode=False):
        provider_pref = config.CONFIG["models"].get("provider", "auto")
        m = config.CONFIG["models"]

        if task_class == VISION:
            name = m.get("vision_model")
            if name and self.local.has_model(name):
                return self.local, name

        if task_class == CODING:
            # prefer any locally installed coder model
            for candidate in ("qwen2.5-coder", "codellama", "deepseek-coder"):
                hit = next(
                    (x for x in self.local_models() if x.startswith(candidate)), None
                )
                if hit:
                    return self.local, hit

        key = {
            CHAT: "chat_model",
            REASONING: "reasoning_model",
            ROUTING: "routing_model",
        }.get(task_class, "chat_model")
        name = m["local"].get(key)

        if provider_pref in ("auto", "local") and self.local.has_model(name):
            return self.local, name

        if provider_pref in ("auto", "online") and self.online_available():
            return self.online, self.online.model

        # last resort: try local with whatever exists
        models = self.local_models()
        if models:
            return self.local, models[0]

        raise ProviderError(
            "no AI provider available. Start Ollama (ollama serve) or configure "
            "an online provider in config.json."
        )

    # -- main entry --------------------------------------------------------

    def chat(self, messages, task_class=CHAT, options=None, json_mode=False):
        """Send a chat completion. messages: [{role, content}, ...]

        Reasoning/coding tasks may use the model's thinking mode; chat and
        routing stay fast with thinking disabled (caller can override via
        options['think']).
        """
        opts = dict(options or {})
        opts.setdefault("think", task_class in (REASONING, CODING))
        provider, model = self._pick(task_class, json_mode)
        last_err = None
        for attempt in range(2):
            try:
                return provider.chat(model, messages, options=opts, json_mode=json_mode)
            except ProviderError:
                raise
            except Exception as e:  # transient network/model errors: retry once
                last_err = e
                log.warning("LLM call failed (%s), retrying: %s", provider.__class__.__name__, e)
                time.sleep(0.5)
        raise ProviderError(f"LLM call failed: {last_err}")


_router = None


def get_router():
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router


def chat(messages, task_class=CHAT, options=None, json_mode=False):
    """Module-level convenience: route a chat call by task class."""
    return get_router().chat(messages, task_class=task_class, options=options, json_mode=json_mode)

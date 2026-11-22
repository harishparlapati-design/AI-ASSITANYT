"""Configuration system for Harry.

Sources (later wins):
1. Built-in defaults
2. config.example.json (tracked, safe defaults)
3. config.json (user local, git-ignored)
4. Environment variables: HARRY_<SECTION>__<KEY>__<KEY> = value

No API keys or personal secrets live in source code. Secrets are referenced
by environment-variable *name* only (e.g. "api_key_env": "OPENAI_API_KEY").
"""

import copy
import json
import os
import shutil
import tempfile
import threading

from . import runtime_paths

_CONFIG_WRITE_LOCK = threading.RLock()

DEFAULTS = {
    "assistant_name": "Harry",
    "user_name": "",                       # set in config.json, never in code
    "wake_word": "harry",
    "data_dir": "data",
    "log_dir": "logs",
    "log_level": "INFO",
    "vr": {
        # Optional WebXR front-end served at /vr. It is a real WebXR client
        # (immersive session with DOM overlay); no headset means a 2D fallback.
        "enabled": True,
    },
    "ui": {
        "host": "127.0.0.1",
        "port": 8765,
        # Non-loopback clients (phone/tablet/other PC) are refused unless this
        # is enabled and they present the token from the named env var.
        "remote": {
            "enabled": False,
            "token_env": "HARRY_REMOTE_TOKEN",
        },
    },
    "models": {
        "provider": "auto",                # auto | local | online
        "local": {
            "type": "ollama",
            "url": "http://localhost:11434",
            "chat_model": "llama3.1:latest",    # non-thinking, fast, conversational
            "reasoning_model": "qwen3:4b",      # thinking model for planning
            "routing_model": "llama3.1:latest", # fast classification
        },
        "online": {
            "type": "none",                # none | openai_compatible
            "url": "",
            "api_key_env": "",             # env var NAME holding the key
            "model": "",
        },
        "vision_model": "",                # optional local vision model name
    },
    "voice": {
        "enabled": True,
        "wake_word_mode": "text",          # text | porcupine | off
        "porcupine_access_key_env": "PORCUPINE_ACCESS_KEY",
        "keyword_path": "hay-harry_en_windows_v4_0_0.ppn",
        "stt_model": "vosk-model-en-us-0.22",
        "tts_engine": "pyttsx3",
        # Voice selection is metadata-driven: the requested gender is matched
        # against the installed voice's Gender attribute, never its name.
        "voice_gender": "male",            # male | female | any
        "voice_name": "",                  # pin an installed voice (substring ok)
        "tts_rate": 170,                   # words per minute (slower = calmer)
        "tts_volume": 1.0,
        "tts_sentence_pause_ms": 200,      # pause between sentences
        "tts_max_sentence_chars": 240,     # long answers are chunked for this
        "tts_read_numbers": True,          # "2026" -> "two thousand and twenty six"
        "tts_expand_abbreviations": True,  # "e.g." -> "for example"
        "output_mode": "both",             # text | voice | both
        "verify_voice": False,             # voiceprint check on wake
        "verify_threshold": 0.43,
        "voice_profile_path": "hareesh_voice_profile.pkl",
    },
    "language": {
        # auto follows the product policy: mixed Telugu-English for general
        # conversation and English for task/technical responses.
        "preference": "auto",          # auto | mixed | english | telugu
    },
    "appearance": {
        "theme": "system",             # system | dark | light
    },
    "integrations": {
        # These flags control whether Harry may delegate to already-installed
        # local CLIs. They do not imply that a CLI is installed or signed in.
        "codex_enabled": True,
        "claude_enabled": True,
        "github_enabled": True,
    },
    "security": {
        "require_confirmation_for": "sensitive",   # sensitive | controlled | none
        "allowed_dirs": [],                        # empty = all, still audited
    },
    "proactive": {
        "enabled": True,
        "briefing_hour": 8,
    },
}


def _deep_merge(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def update_user_sections(sections, path=None):
    """Atomically persist several settings sections in the user's config.json.

    Existing values outside the supplied keys are preserved. Returns the file
    path on success, else None.
    """
    if not isinstance(sections, dict) or not sections:
        return None
    if any(not isinstance(values, dict) or not values
           for values in sections.values()):
        return None
    target = path or os.path.join(runtime_paths.workspace_root(), "config.json")
    if any(not isinstance(section, str) or not section
           or any(not isinstance(key, str) or not key for key in values)
           for section, values in sections.items()):
        return None
    folder = os.path.dirname(os.path.abspath(target))
    tmp = None
    try:
        with _CONFIG_WRITE_LOCK:
            data = {}
            if os.path.exists(target):
                with open(target, "r", encoding="utf-8") as f:
                    data = json.load(f)
            if not isinstance(data, dict):
                return None
            for section, values in sections.items():
                node = data.setdefault(section, {})
                if not isinstance(node, dict):
                    node = data[section] = {}
                node.update(values)
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".harry-config-", suffix=".tmp",
                                       dir=folder)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
            tmp = None
        return target
    except Exception:
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def update_user(section, values, path=None):
    """Persist settings into the user's config.json (never config.example.json)."""
    if not isinstance(values, dict) or not values:
        return None
    return update_user_sections({section: values}, path=path)


def load():
    """Return the merged configuration dict."""
    cfg = copy.deepcopy(DEFAULTS)
    root = runtime_paths.workspace_root()
    if runtime_paths.is_frozen():
        # Give a fresh install a config template next to its data folder.
        bundled_example = runtime_paths.find_asset("config.example.json")
        destination = os.path.join(root, "config.example.json")
        if bundled_example and not os.path.exists(destination) and bundled_example != destination:
            try:
                shutil.copyfile(bundled_example, destination)
            except OSError:
                pass
    for name in ("config.example.json", "config.json"):
        path = os.path.join(root, name)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    user = json.load(f)
                _deep_merge(cfg, user)
            except Exception as e:
                print(f"[config] Could not parse {name}: {e}")

    # Environment overrides: HARRY_MODELS__LOCAL__URL=http://... etc.
    for var, value in os.environ.items():
        if not var.startswith("HARRY_") or var == "HARRY_":
            continue
        path = var[len("HARRY_"):].lower().split("__")
        node = cfg
        try:
            for k in path[:-1]:
                node = node[k]
            node[path[-1]] = value
        except (KeyError, TypeError):
            print(f"[config] Ignoring unknown env var {var}")

    return cfg


CONFIG = load()

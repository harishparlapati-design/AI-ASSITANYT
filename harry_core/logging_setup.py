"""Logging and audit system.

- Normal logs: logs/harry.log (rotating)
- Audit log:   logs/audit.jsonl - one JSON line per security-relevant event
                (tool calls, permission requests, confirmations, denials).
Secrets are scrubbed before anything is written.
"""

import json
import logging
import os
import re
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from . import config, runtime_paths

_log_lock = threading.Lock()
_audit_lock = threading.Lock()

# Patterns scrubbed from any log/audit text
_SECRET_PATTERNS = [
    (re.compile(r"(sk-[A-Za-z0-9_\-]{16,})"), "<redacted-key>"),
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"), r"\1<redacted>"),
]


def scrub(text):
    """Remove secret-looking material from a string."""
    text = str(text)
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def setup():
    """Initialize rotating file + console logging. Safe to call repeatedly."""
    # Windows consoles default to cp1252 and crash on Telugu/Hindi output.
    try:
        for stream in (sys.stdout, sys.stderr):
            if stream and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    log_dir = os.path.join(runtime_paths.workspace_root(), config.CONFIG.get("log_dir", "logs"))
    os.makedirs(log_dir, exist_ok=True)
    level = getattr(logging, str(config.CONFIG.get("log_level", "INFO")).upper(), logging.INFO)

    root = logging.getLogger("harry")
    if root.handlers:
        root.setLevel(level)
        return root
    root.setLevel(level)

    fh = RotatingFileHandler(
        os.path.join(log_dir, "harry.log"), maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(ch)
    return root


def get_logger(name="harry"):
    return setup().getChild(name)


def audit(event_type, detail=None, **fields):
    """Append an audit record to logs/audit.jsonl. Never raises."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "type": event_type,
        "detail": scrub(detail) if detail else None,
    }
    record.update({k: scrub(v) for k, v in fields.items()})
    try:
        log_dir = os.path.join(runtime_paths.workspace_root(), config.CONFIG.get("log_dir", "logs"))
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, "audit.jsonl")
        with _audit_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
    try:
        get_logger("audit").info("%s %s", event_type, record["detail"] or "")
    except Exception:
        pass

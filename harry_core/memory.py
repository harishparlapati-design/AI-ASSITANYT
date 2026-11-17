"""Persistent multi-layer memory system.

Layers:
  working   - current task/session scratchpad (volatile, also persisted for resume)
  convers.  - rolling conversation turns (JSONL, trimmed)
  longterm  - facts/preferences/projects with tags, importance, TTL updating

Storage: plain JSON/JSONL files under data/ - no database dependency, fully
offline, trivially inspectable and portable across devices.

Search is keyword + lightweight token-overlap scoring (no external services).
"""

import json
import logging
import os
import re
import threading
import time
from collections import Counter

from . import config, runtime_paths

log = logging.getLogger("harry.memory")
_lock = threading.RLock()

_STOPWORDS = set(
    "a an the is are was were be been being to of in on at for with and or but if "
    "then than that this these those it its as by from my your his her our their "
    "what when where which who whom how why not no yes do does did done can could "
    "should would will shall may might must have has had i you he she we they me "
    "him us them so too very just now also".split()
)


def _data_path(*parts):
    d = os.path.join(runtime_paths.workspace_root(), config.CONFIG.get("data_dir", "data"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, *parts)


def _tokens(text):
    return [t for t in re.findall(r"[\w']+", str(text).lower()) if t not in _STOPWORDS and len(t) > 1]


# ----------------------------------------------------------------- working memory

class WorkingMemory:
    """Scratchpad for the current task/session; persisted so tasks can resume."""

    KEY = "working_memory.json"

    def __init__(self):
        self.data = {}
        self._load()

    def _load(self):
        try:
            with open(_data_path(self.KEY), "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def _save(self):
        with open(_data_path(self.KEY), "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def set(self, key, value):
        with _lock:
            self.data[key] = value
            self._save()

    def get(self, key, default=None):
        return self.data.get(key, default)

    def clear(self, key=None):
        with _lock:
            if key is None:
                self.data = {}
            else:
                self.data.pop(key, None)
            self._save()


# ----------------------------------------------------------------- conversation

class ConversationMemory:
    """Rolling conversation history in JSONL."""

    KEY = "conversation.jsonl"
    MAX = 400

    def __init__(self):
        self.path = _data_path(self.KEY)

    def add(self, role, text, meta=None):
        entry = {"ts": time.time(), "role": role, "text": text}
        if meta:
            entry["meta"] = meta
        with _lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._trim()

    def recent(self, n=20):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-n:]
            return [json.loads(l) for l in lines if l.strip()]
        except Exception:
            return []

    def search(self, query, limit=5):
        q_tokens = _tokens(query)
        scored = []
        for entry in self.recent(400):
            score = len(set(q_tokens) & set(_tokens(entry["text"])))
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda x: -x[0])
        return [e for _, e in scored[:limit]]

    def _trim(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > self.MAX:
                with open(self.path, "w", encoding="utf-8") as f:
                    f.writelines(lines[-self.MAX:])
        except Exception:
            pass

    def clear(self):
        with _lock:
            open(self.path, "w").close()


# ----------------------------------------------------------------- long-term

class LongTermMemory:
    """Facts, preferences, projects, procedures - tagged and importance-ranked."""

    KEY = "longterm.json"

    def __init__(self):
        self.path = _data_path(self.KEY)
        self.items = []
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.items = json.load(f)
        except Exception:
            self.items = []

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def store(self, text, kind="fact", tags=None, importance=1, source=None):
        """Store or update a memory. Updating similar existing memory instead
        of duplicating."""
        text = str(text).strip()
        if not text:
            return None
        with _lock:
            existing = self._find_similar(text)
            now = time.time()
            if existing:
                existing["text"] = text
                existing["updated_at"] = now
                existing["importance"] = max(existing.get("importance", 1), importance)
                if tags:
                    existing["tags"] = sorted(set(existing.get("tags", [])) | set(tags))
                self._save()
                return existing["id"]
            item = {
                "id": f"m{int(now * 1000)}",
                "text": text,
                "kind": kind,          # fact | preference | project | procedure | person
                "tags": tags or [],
                "importance": importance,
                "source": source,
                "created_at": now,
                "updated_at": now,
                "recalls": 0,
            }
            self.items.append(item)
            self._save()
            return item["id"]

    def _find_similar(self, text):
        key = "".join(sorted(_tokens(text)))[:80]
        for item in self.items:
            if "".join(sorted(_tokens(item["text"])))[:80] == key:
                return item
        return None

    def search(self, query, limit=6, kind=None, tag=None):
        q = set(_tokens(query))
        scored = []
        for item in self.items:
            if kind and item.get("kind") != kind:
                continue
            if tag and tag not in item.get("tags", []):
                continue
            overlap = len(q & set(_tokens(item["text"])))
            if not overlap:
                continue
            score = overlap + item.get("importance", 1) * 0.5 + min(item.get("recalls", 0), 5) * 0.1
            scored.append((score, item))
        scored.sort(key=lambda x: -x[0])
        for _, item in scored[:limit]:
            item["recalls"] = item.get("recalls", 0) + 1
        if scored:
            self._save()
        return [item for _, item in scored[:limit]]

    def get(self, memory_id):
        for item in self.items:
            if item["id"] == memory_id:
                return item
        return None

    def forget(self, memory_id=None, text=None):
        """Remove a memory by id or by matching text. Returns count removed."""
        removed = 0
        with _lock:
            keep = []
            for item in self.items:
                match = (memory_id and item["id"] == memory_id) or (
                    text and text.lower() in item["text"].lower()
                )
                if match:
                    removed += 1
                else:
                    keep.append(item)
            self.items = keep
            self._save()
        return removed

    def all(self, kind=None, tag=None):
        return [
            i for i in self.items
            if (not kind or i.get("kind") == kind) and (not tag or tag in i.get("tags", []))
        ]


# ----------------------------------------------------------------- facade

class MemorySystem:
    def __init__(self):
        self.working = WorkingMemory()
        self.conversation = ConversationMemory()
        self.longterm = LongTermMemory()

    # convenience passthroughs
    def store(self, text, **kw):
        return self.longterm.store(text, **kw)

    def search(self, query, **kw):
        return self.longterm.search(query, **kw)

    def forget(self, **kw):
        return self.longterm.forget(**kw)

    def stats(self):
        return {
            "longterm_items": len(self.longterm.items),
            "conversation_turns": len(self.conversation.recent(10000)),
            "working_keys": len(self.working.data),
        }


_memory = None


def get_memory():
    global _memory
    if _memory is None:
        _memory = MemorySystem()
    return _memory

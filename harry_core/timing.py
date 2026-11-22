"""Per-request latency tracing.

One Trace follows a request from receipt to delivery and records the stage
boundaries worth knowing about: intent detection, memory retrieval, model
choice, first token, inference, tool steps, TTS and delivery. Stage values are
absolute millisecond offsets from receipt, so one log line shows both where the
time went and the total.

A small ring buffer of finished traces is kept for the dashboard. Traces hold
durations and model names only - never message text.
"""

import collections
import contextlib
import logging
import threading
import time

log = logging.getLogger("harry.timing")

_local = threading.local()
_lock = threading.Lock()
_recent = collections.deque(maxlen=80)


class Trace:
    """Stage timings for one request, in ms since receipt."""

    def __init__(self, request_id="", source=""):
        self.request_id = str(request_id or "")
        self.source = str(source or "")
        self.started = time.perf_counter()
        self.stages = []
        self.model = ""
        self.first_token_ms = None
        self.status = "running"

    def elapsed_ms(self):
        return (time.perf_counter() - self.started) * 1000.0

    def mark(self, name):
        """Record that a stage boundary was reached now."""
        ms = round(self.elapsed_ms())
        self.stages.append((str(name)[:40], ms))
        return ms

    def first_token(self):
        """Record the first visible model token exactly once."""
        if self.first_token_ms is None:
            self.first_token_ms = self.mark("first_token")
        return self.first_token_ms

    def set_model(self, model):
        self.model = str(model or "")

    def finish(self, status="ok"):
        self.status = status
        summary = " ".join(f"{name}={ms}" for name, ms in self.stages)
        log.info("[%s] timing %s total=%.0fms model=%s",
                 self.request_id or "-", summary, self.elapsed_ms(),
                 self.model or "-")
        with _lock:
            _recent.appendleft(self.to_dict())
        return self.to_dict()

    def to_dict(self):
        return {
            "request_id": self.request_id,
            "source": self.source,
            "model": self.model,
            "status": self.status,
            "total_ms": round(self.elapsed_ms()),
            "first_token_ms": self.first_token_ms,
            "stages": [{"stage": name, "ms": ms} for name, ms in self.stages],
        }


def start(request_id="", source=""):
    """Begin tracing the current thread's request."""
    trace = Trace(request_id, source)
    _local.trace = trace
    return trace


def current():
    return getattr(_local, "trace", None)


def clear():
    _local.trace = None


def mark(name):
    trace = current()
    return trace.mark(name) if trace is not None else None


def first_token():
    trace = current()
    return trace.first_token() if trace is not None else None


def set_model(model):
    trace = current()
    if trace is not None:
        trace.set_model(model)


@contextlib.contextmanager
def stage(name):
    """Time a block of work: ``with timing.stage("memory"): ...``"""
    started = time.perf_counter()
    try:
        yield
    finally:
        trace = current()
        if trace is not None:
            trace.stages.append(
                (str(name)[:40], round((time.perf_counter() - trace.started) * 1000))
            )


def recent(limit=20):
    """The most recent finished traces, newest first."""
    with _lock:
        return list(_recent)[:max(0, int(limit or 0))]

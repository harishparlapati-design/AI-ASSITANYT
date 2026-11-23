"""Browser screen sharing session.

The browser owns the capture (``getDisplayMedia``), so the user sees the real
permission prompt and the OS indicator. This module is the server-side gate:

* frames are refused unless a session is active (never silently captured)
* paused sessions refuse frames without dropping the session
* frames are analysed in memory and discarded; only text is kept
* ``stop`` clears the retained text immediately
* a minimum interval between model calls keeps CPU inference usable

Nothing in this module touches the camera or the desktop directly.
"""

import logging
import threading
import time

from . import events, vision

log = logging.getLogger("harry.screen")

INACTIVE, ACTIVE, PAUSED = "inactive", "active", "paused"
MIN_INTERVAL = 1.2          # seconds between analyses
MAX_ANALYSES_PER_MINUTE = 40


class ScreenSession:
    def __init__(self):
        self.state = INACTIVE
        self.source = "browser"
        self.started = None
        self.frames = 0
        self.analyses = 0
        self.failures = 0
        self.last_frame_at = 0.0
        self.last_request_at = 0.0
        self.last_analysis = None
        self.last_error = None
        self._analyzing = False
        self._recent = []
        self._lock = threading.RLock()

    # -- state -------------------------------------------------------------

    def status(self):
        with self._lock:
            return {
                "state": self.state,
                "sharing": self.state == ACTIVE,
                "paused": self.state == PAUSED,
                "source": self.source,
                "active_seconds": round(time.time() - self.started, 1) if self.started else 0,
                "frames": self.frames,
                "analyses": self.analyses,
                "failures": self.failures,
                "last_frame_at": self.last_frame_at,
                "last_analysis": self.last_analysis,
                "last_error": self.last_error,
                "vision": vision.status(),
                "min_interval_seconds": MIN_INTERVAL,
            }

    def _emit(self, status=None):
        events.emit("screen_state", status or self.status())

    def start(self, source="browser"):
        with self._lock:
            self.state = ACTIVE
            self.source = str(source or "browser")[:40]
            self.started = time.time()
            self.frames = 0
            self.analyses = 0
            self.failures = 0
            self.last_error = None
            self.last_request_at = 0.0
            self._analyzing = False
            self._recent = []
        log.info("screen sharing started (%s)", self.source)
        self._emit()
        return self.status()

    def pause(self):
        with self._lock:
            if self.state == ACTIVE:
                self.state = PAUSED
        self._emit()
        return self.status()

    def resume(self):
        with self._lock:
            if self.state == PAUSED:
                self.state = ACTIVE
        self._emit()
        return self.status()

    def stop(self):
        with self._lock:
            was = self.state
            self.state = INACTIVE
            self.started = None
            self.last_analysis = None      # do not retain screen text after stop
            self._recent = []
        log.info("screen sharing stopped (was %s)", was)
        self._emit()
        return self.status()

    # -- frames ------------------------------------------------------------

    def _rate_limited(self):
        """Gate on *arrival* time, so slow inference cannot build a backlog."""
        now = time.time()
        with self._lock:
            self._recent = [t for t in self._recent if now - t < 60]
            if self._analyzing:
                return True, 1, "an analysis is already running"
            if len(self._recent) >= MAX_ANALYSES_PER_MINUTE:
                return True, 60, "too many analyses this minute"
            wait = MIN_INTERVAL - (now - self.last_request_at) if self.last_request_at else 0
            if wait > 0:
                return True, round(wait, 2), "wait for the previous frame"
            self.last_request_at = now
        return False, 0, ""

    def accept_frame(self, image_bytes, question=None, timeout=180):
        """Analyse one frame captured by the browser."""
        with self._lock:
            if self.state == INACTIVE:
                return {"ok": False, "error": "screen sharing is not active",
                        "kind": "inactive"}, 409
            if self.state == PAUSED:
                return {"ok": False, "error": "screen sharing is paused",
                        "kind": "paused"}, 409
            self.frames += 1
        limited, wait, why = self._rate_limited()
        if limited:
            return {"ok": False, "error": f"frame not accepted: {why}",
                    "retry_after": wait, "kind": "rate_limited"}, 429
        if not image_bytes:
            return {"ok": False, "error": "no frame supplied", "kind": "empty"}, 400

        with self._lock:
            self._analyzing = True
        started = time.perf_counter()
        try:
            result = vision.analyze(image_bytes, question=question, timeout=timeout)
        finally:
            with self._lock:
                self._analyzing = False
        elapsed = round((time.perf_counter() - started) * 1000)
        with self._lock:
            self.last_frame_at = time.time()
            self._recent.append(self.last_frame_at)
            if result.get("ok"):
                self.analyses += 1
                self.last_analysis = {
                    "text": str(result.get("text") or "")[:4000],
                    "method": result.get("method"),
                    "at": time.time(),
                    "ms": elapsed,
                }
                self.last_error = None
            else:
                self.failures += 1
                self.last_error = result.get("error") or "analysis failed"
        snapshot = self.status()
        payload = {**result, "session": snapshot}
        self._emit(snapshot)
        return payload, (200 if result.get("ok") else 503)

    def context_for_chat(self, max_chars=2500):
        """Latest screen text, for the conversation to reference."""
        with self._lock:
            if self.state == INACTIVE or not self.last_analysis:
                return ""
            text = str(self.last_analysis.get("text") or "").strip()
        return text[:max_chars]


_session = None


def get_screen():
    global _session
    if _session is None:
        _session = ScreenSession()
    return _session

"""Proactive assistance scheduler.

User-controlled proactive features:
  - one-shot reminders ("remind me in 10 minutes to...")
  - daily briefing at a configured hour
  - unfinished-task nudges

Everything is opt-out via config ("proactive.enabled": false) and emits
events on the bus; the UI/front-end decides how to surface them. A separate
daemon thread checks every 20 seconds; honors the emergency stop.
"""

import logging
import threading
import time

from . import config, events
from .memory import get_memory

log = logging.getLogger("harry.scheduler")

CHECK_INTERVAL = 20  # seconds


class Scheduler:
    def __init__(self):
        self._reminders = []           # [{id, text, due_ts, done}]
        self._lock = threading.Lock()
        self._thread = None
        self._last_brief_day = None
        self._last_nudge = 0.0

    # -- reminders ----------------------------------------------------------

    def add_reminder(self, text, delay_minutes=5, due_ts=None):
        rid = f"rem_{int(time.time() * 1000)}"
        with self._lock:
            self._reminders.append({
                "id": rid,
                "text": text,
                "due_ts": due_ts or (time.time() + max(delay_minutes, 0.02) * 60),
                "done": False,
            })
        events.emit("reminder_created", {"id": rid, "text": text})
        return rid

    def pending(self):
        with self._lock:
            return [r for r in self._reminders if not r["done"]]

    # -- background loop ------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="harry-scheduler")
        self._thread.start()
        log.info("proactive scheduler started")

    def _loop(self):
        while not events.is_stopped():
            try:
                self._tick()
            except Exception as e:
                log.warning("scheduler tick error: %s", e)
            for _ in range(CHECK_INTERVAL * 5):
                if events.is_stopped():
                    return
                time.sleep(0.2)

    def _tick(self):
        now = time.time()
        with self._lock:
            due = [r for r in self._reminders if not r["done"] and r["due_ts"] <= now]
            for r in due:
                r["done"] = True
        for r in due:
            events.emit("reminder_due", {"id": r["id"], "text": r["text"]})
            get_memory().conversation.add(
                "assistant", f"⏰ Reminder: {r['text']}")
            log.info("reminder due: %s", r["text"])

        if not config.CONFIG.get("proactive", {}).get("enabled", True):
            return

        # daily briefing
        briefing_hour = int(config.CONFIG.get("proactive", {}).get("briefing_hour", 8))
        local_hour = time.localtime().tm_hour
        today = time.strftime("%Y-%m-%d")
        if local_hour == briefing_hour and self._last_brief_day != today:
            self._last_brief_day = today
            self._daily_briefing()

        # unfinished-task nudge (at most every 2 hours, quiet hours respected)
        if 9 <= local_hour <= 21 and now - self._last_nudge > 2 * 3600:
            self._nudge_unfinished()

    def _daily_briefing(self):
        try:
            from .task_engine import get_engine
            open_tasks = get_engine().list_tasks()
            active = [t for t in open_tasks if t["status"] not in
                      ("COMPLETED", "FAILED", "CANCELLED")]
            overdue = [r["text"] for r in self.pending()]
            briefing = {
                "active_tasks": len(active),
                "task_titles": [t["title"][:60] for t in active[:5]],
                "pending_reminders": overdue[:5],
            }
            events.emit("daily_briefing", briefing)
            get_memory().conversation.add(
                "assistant",
                f"☀️ Good morning! You have {len(active)} active task(s)"
                + (f" and {len(overdue)} reminder(s)." if overdue else "."))
            log.info("daily briefing emitted: %s", briefing)
        except Exception as e:
            log.warning("briefing failed: %s", e)

    def _nudge_unfinished(self):
        try:
            from .task_engine import get_engine
            self._last_nudge = time.time()
            unfinished = [t for t in get_engine().list_tasks()
                          if t["status"] in ("EXECUTING", "PLANNING", "ERROR")]
            if unfinished:
                events.emit("unfinished_tasks", {"count": len(unfinished),
                                                 "ids": [t["id"] for t in unfinished[:3]]})
        except Exception:
            pass


_scheduler = None


def get_scheduler():
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler

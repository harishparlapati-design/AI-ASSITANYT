"""Background jobs.

One place for slow work that must not block an HTTP request: document
creation, image/video generation, long task plans. Each job is a real thread
with a lifecycle the dashboard can poll and cancel:

    queued -> running -> done | error | cancelled

State is persisted to data/jobs.json so history survives a restart. A job that
was running when the process died is marked ``interrupted`` on load instead of
being reported as finished.
"""

import json
import logging
import os
import threading
import time
import uuid

from . import events, runtime_paths, tenant

log = logging.getLogger("harry.jobs")

QUEUED, RUNNING, DONE, ERROR, CANCELLED = (
    "queued", "running", "done", "error", "cancelled",
)
TERMINAL = (DONE, ERROR, CANCELLED)
MAX_KEEP = 120


class Job:
    def __init__(self, kind, label, meta=None, job_id=None):
        self.id = job_id or f"job_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        self.kind = str(kind or "job")
        self.label = str(label or self.kind)
        self.meta = dict(meta or {})
        self.state = QUEUED
        self.progress = 0
        self.message = "queued"
        self.result = None
        self.error = None
        self.created = time.time()
        self.updated = self.created
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # -- hooks used by the worker function --------------------------------

    def update(self, progress=None, message=None, **extra):
        with self._lock:
            if progress is not None:
                self.progress = max(0, min(100, int(progress)))
            if message is not None:
                self.message = str(message)
            for key, value in extra.items():
                if key in ("result", "error", "state"):
                    setattr(self, key, value)
            self.updated = time.time()
        events.emit("job_progress", self.public())
        return self

    def request_cancel(self):
        self._cancel.set()
        self.update(message="cancelling")

    @property
    def cancelled(self):
        return self._cancel.is_set() or events.is_stopped()

    def check_cancelled(self):
        if self.cancelled:
            raise JobCancelled("cancelled")

    def public(self, include_result=True):
        data = {
            "id": self.id, "kind": self.kind, "label": self.label,
            "state": self.state, "progress": self.progress,
            "message": self.message, "error": self.error,
            "created": self.created, "updated": self.updated,
            "meta": self.meta,
        }
        if include_result:
            data["result"] = self.result
        return data


class JobCancelled(Exception):
    pass


class JobManager:
    def __init__(self, path=None):
        self.path = path or os.path.join(
            runtime_paths.workspace_root(), "data", "jobs",
            f"{tenant.current()}.json")
        self.jobs = {}
        self._lock = threading.RLock()
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for item in raw.get("jobs", []):
                job = Job(item.get("kind", "job"), item.get("label", ""),
                          item.get("meta"), item.get("id"))
                for key in ("state", "progress", "message", "result", "error",
                            "created", "updated"):
                    if key in item:
                        setattr(job, key, item[key])
                if job.state in (QUEUED, RUNNING):
                    job.state = ERROR
                    job.error = "interrupted by a restart"
                    job.message = "interrupted"
                self.jobs[job.id] = job
        except Exception:
            self.jobs = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with self._lock:
                items = sorted(self.jobs.values(), key=lambda j: j.created)[-MAX_KEEP:]
                payload = {"jobs": [{
                    "id": j.id, "kind": j.kind, "label": j.label, "meta": j.meta,
                    "state": j.state, "progress": j.progress, "message": j.message,
                    "result": j.result, "error": j.error, "created": j.created,
                    "updated": j.updated,
                } for j in items]}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning("could not persist jobs: %s", e)

    # -- lifecycle ---------------------------------------------------------

    def submit(self, kind, label, fn, meta=None):
        """Run ``fn(job)`` on a worker thread. Returns the public job dict."""
        job = Job(kind, label, meta)
        with self._lock:
            self.jobs[job.id] = job
            self._prune()
        self._save()
        events.emit("job_created", job.public(include_result=False))

        owner = tenant.current()

        def worker():
            # Background threads do not inherit request context: restore the
            # tenant so a user's files never land in another user's folder.
            token = tenant.set_tenant(owner)
            job.state = RUNNING
            job.message = "running"
            job.updated = time.time()
            events.emit("job_progress", job.public(include_result=False))
            try:
                result = fn(job)
                if job.cancelled:
                    raise JobCancelled("cancelled")
                job.state = DONE
                job.progress = 100
                job.result = result
                job.message = "done"
            except JobCancelled:
                job.state = CANCELLED
                job.message = "cancelled"
            except events.EmergencyStop:
                job.state = CANCELLED
                job.message = "stopped"
            except Exception as e:
                log.exception("job %s failed", job.id)
                job.state = ERROR
                job.error = f"{type(e).__name__}: {e}"
                job.message = "failed"
            finally:
                job.updated = time.time()
                self._save()
                events.emit("job_finished", job.public())
                tenant.reset(token)

        threading.Thread(target=worker, daemon=True,
                         name=f"job-{job.kind}-{job.id[-4:]}").start()
        return job.public(include_result=False)

    def _prune(self):
        finished = sorted((j for j in self.jobs.values() if j.state in TERMINAL),
                          key=lambda j: j.updated)
        for job in finished[:max(0, len(finished) - MAX_KEEP)]:
            self.jobs.pop(job.id, None)

    def get(self, job_id):
        return self.jobs.get(str(job_id or ""))

    def list(self, kind=None, limit=30):
        with self._lock:
            items = list(self.jobs.values())
        if kind:
            items = [j for j in items if j.kind == kind]
        items.sort(key=lambda j: j.created, reverse=True)
        return [j.public() for j in items[:max(1, min(int(limit or 30), 200))]]

    def cancel(self, job_id):
        job = self.get(job_id)
        if not job:
            return {"ok": False, "error": "unknown job"}
        if job.state in TERMINAL:
            return {"ok": False, "error": f"job already {job.state}"}
        job.request_cancel()
        return {"ok": True, "job": job.public(include_result=False)}


_managers = {}
_manager_lock = threading.Lock()


def recover_stale(max_age=1800):
    """Fail jobs a crashed worker left ``running``. Used by self-healing."""
    manager = get_jobs()
    now = time.time()
    recovered = []
    with manager._lock:
        for job in manager.jobs.values():
            if job.state == RUNNING and now - float(job.updated or 0) > float(max_age):
                job.state = ERROR
                job.error = "interrupted; recovered by the self-healing engine"
                job.message = "interrupted"
                job.updated = now
                recovered.append(job.id)
        if recovered:
            manager._save()
    return {"ok": True, "recovered": recovered, "count": len(recovered)}


def get_jobs():
    """One job table per tenant, so hosted users never see each other's jobs."""
    name = tenant.current()
    with _manager_lock:
        manager = _managers.get(name)
        if manager is None:
            manager = JobManager()
            _managers[name] = manager
        return manager

"""Continuous health monitoring with cheap, bounded checks.

One daemon thread samples the machine and the subsystems on an interval
(120 s by default). Expensive probes are amortised: the network is checked every
ten cycles, the speech stack every five, and Ollama's model list is cached. Every
threshold breach is de-duplicated for 30 minutes so the administrator is not
spammed by the same warning.

Nothing here restarts a service. Monitoring observes and alerts; repairs belong
to :mod:`harry_core.selfheal`.
"""

import json
import logging
import os
import socket
import threading
import time

from . import config, runtime_paths

log = logging.getLogger("harry.monitoring")

DEFAULTS = {
    "enabled": True,
    "interval_seconds": 120,
    "disk_warn_percent": 90,
    "cpu_warn_percent": 92,
    "ram_warn_percent": 92,
    "latency_warn_seconds": 45,
}

_lock = threading.RLock()
_thread = None
_stop = threading.Event()
_snapshot = {"at": 0, "ok": None, "checks": {}, "alerts": []}
_last_alert = {}
_history = []


def settings():
    merged = dict(DEFAULTS)
    merged.update((config.CONFIG.get("monitor", {}) or {}))
    return merged


def _pct(value):
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _check_cpu_ram_disk():
    checks = {}
    try:
        import psutil
        checks["cpu"] = {"ok": True, "value": _pct(psutil.cpu_percent(interval=None)),
                         "unit": "%"}
        memory = psutil.virtual_memory()
        checks["ram"] = {"ok": True, "value": _pct(memory.percent), "unit": "%"}
        root = runtime_paths.workspace_root()
        usage = psutil.disk_usage(root)
        checks["disk"] = {"ok": True, "value": _pct(usage.percent), "unit": "%",
                          "free_gb": round(usage.free / 1e9, 1)}
    except Exception as e:
        checks["cpu"] = {"ok": False, "error": str(e)}
    return checks


def _check_ollama():
    try:
        from . import llm
        router = llm.get_router()
        models = router.local_models()
        return {"ok": bool(models), "models": models[:5], "count": len(models),
                "detail": "reachable" if models else "no models answered"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _check_voice():
    checks = {}
    try:
        from .speech import get_listener
        checks["stt"] = {"ok": get_listener().available()}
    except Exception as e:
        checks["stt"] = {"ok": False, "error": str(e)}
    try:
        from .speech import get_speaker
        speaker = get_speaker()
        engine = speaker._init_engine()
        checks["tts"] = {"ok": bool(engine)}
    except Exception as e:
        checks["tts"] = {"ok": False, "error": str(e)}
    return checks


def _check_backend():
    checks = {}
    try:
        from . import jobs
        manager = jobs.get_jobs()
        items = manager.list(limit=50)
        failed = [j for j in items if j.get("state") == "error"]
        running = [j for j in items if j.get("state") == "running"]
        checks["jobs"] = {"ok": True, "failed": len(failed), "running": len(running)}
    except Exception as e:
        checks["jobs"] = {"ok": False, "error": str(e)}
    try:
        from . import selfheal
        bad = [name for name, item in selfheal.diagnose().items() if not item.get("ok")]
        checks["components"] = {"ok": not bad, "unhealthy": bad}
    except Exception as e:
        checks["components"] = {"ok": False, "error": str(e)}
    try:
        from . import capabilities as _caps
        pending = [c["name"] for c in _caps.list_capabilities()
                   if c.get("status") in ("pending", "invalid")]
        checks["pending_plugins"] = {"ok": not pending, "names": pending[:5]}
    except Exception:
        pass
    try:
        from . import capabilities
        failed = [c["name"] for c in capabilities.list_capabilities()
                  if c.get("status") == "failed"]
        checks["plugins"] = {"ok": not failed, "failed": failed}
    except Exception as e:
        checks["plugins"] = {"ok": False, "error": str(e)}
    try:
        from . import timing
        traces = timing.recent(8)
        slow = [t for t in traces
                if float(t.get("total") or 0) > settings()["latency_warn_seconds"]]
        checks["latency"] = {"ok": not slow, "recent": len(traces), "slow": len(slow)}
    except Exception:
        pass
    return checks


def _check_network():
    started = time.perf_counter()
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=1.5).close()
        return {"ok": True, "ms": round((time.perf_counter() - started) * 1000)}
    except Exception:
        return {"ok": False, "detail": "offline; local features still work"}


def check(include_network=False, include_voice=False):
    """Run one sampling round. Cheap by default, safe to call from tests."""
    checks = {}
    checks.update(_check_cpu_ram_disk())
    checks.update(_check_backend())
    checks["ollama"] = _check_ollama()
    if include_voice:
        checks.update(_check_voice())
    if include_network:
        checks["network"] = _check_network()
    limits = settings()
    alerts = []
    if (checks.get("cpu", {}).get("value") or 0) >= limits["cpu_warn_percent"]:
        alerts.append(("cpu_high", f"CPU at {checks['cpu']['value']}%"))
    if (checks.get("ram", {}).get("value") or 0) >= limits["ram_warn_percent"]:
        alerts.append(("ram_high", f"memory at {checks['ram']['value']}%"))
    if (checks.get("disk", {}).get("value") or 0) >= limits["disk_warn_percent"]:
        alerts.append(("disk_low", f"disk at {checks['disk']['value']}% used"))
    if checks.get("ollama", {}).get("ok") is False:
        alerts.append(("model_offline", "the local model server did not answer"))
    if checks.get("components", {}).get("unhealthy"):
        alerts.append(("component_unhealthy",
                       "unhealthy: " + ", ".join(checks["components"]["unhealthy"])))
    if checks.get("plugins", {}).get("failed"):
        alerts.append(("plugin_failed",
                       "failed plugins: " + ", ".join(checks["plugins"]["failed"])))
    if checks.get("latency", {}).get("slow"):
        alerts.append(("latency_high",
                       f"{checks['latency']['slow']} recent answers were slow"))
    failed = [name for name, item in checks.items() if isinstance(item, dict)
              and item.get("ok") is False]
    snapshot = {
        "at": time.time(), "ok": not failed and not alerts, "checks": checks,
        "failed": failed, "alerts": [{"kind": k, "message": m} for k, m in alerts],
    }
    _store_alerts(alerts)
    global _snapshot
    _snapshot = snapshot
    _history.append(snapshot)
    del _history[:-120]
    return snapshot


def _store_alerts(alerts):
    now = time.time()
    from . import selfheal
    for kind, message in alerts:
        if now - _last_alert.get(kind, 0) < 1800:
            continue
        _last_alert[kind] = now
        selfheal.alert(kind, message, {"source": "monitor"})
        log.warning("monitor alert: %s", message)


def _loop():
    interval = float(settings()["interval_seconds"])
    cycle = 0
    while not _stop.wait(interval):
        try:
            check(include_network=(cycle % 10 == 0), include_voice=(cycle % 5 == 0))
        except Exception as e:
            log.warning("monitor cycle failed: %s", e)
        cycle += 1


def start():
    global _thread
    if not settings()["enabled"]:
        return {"ok": False, "error": "monitoring is disabled in config"}
    with _lock:
        if _thread and _thread.is_alive():
            return {"ok": True, "running": True, "already": True}
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="harry-monitor", daemon=True)
        _thread.start()
    log.info("monitoring started (every %ss)", settings()["interval_seconds"])
    return {"ok": True, "running": True}


def stop():
    _stop.set()
    return {"ok": True, "running": False}


def status():
    thread = _thread
    return {
        "running": bool(thread and thread.is_alive()),
        "interval": settings()["interval_seconds"],
        "snapshot": _snapshot if _snapshot.get("at") else None,
        "history": _history[-30:],
    }


def read_alerts():
    path = os.path.join(runtime_paths.workspace_root(), "data", "alerts.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def mark_alerts_seen():
    path = os.path.join(runtime_paths.workspace_root(), "data", "alerts.json")
    items = read_alerts()
    for item in items:
        item["seen"] = True
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return {"ok": True, "count": len(items)}


def ensure_started():
    """Start monitoring once, lazily, when the dashboard first asks."""
    if status()["running"]:
        return status()
    try:
        start()
    except Exception as e:
        log.warning("monitor did not start: %s", e)
    return status()

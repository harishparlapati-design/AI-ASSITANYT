"""Global event bus and emergency stop.

The emergency stop is process-wide: every long-running loop (task executor,
wake-word listener, proactive scheduler, speech playback) checks
events.is_stopped() and aborts promptly.
"""

import queue
import threading
import time

_listeners = []          # list of (event_name_or_None, callback, use_queue)
_lock = threading.Lock()
_stop_flag = threading.Event()


def emit(event, data=None):
    """Fire an event to all interested listeners (synchronously, errors isolated)."""
    with _lock:
        targets = list(_listeners)
    for name, cb, _q in targets:
        if name is None or name == event:
            try:
                cb(event, data)
            except Exception as e:
                # never let one listener break the bus
                try:
                    from . import logging_setup
                    logging_setup.get_logger("events").warning("listener error: %s", e)
                except Exception:
                    pass


def subscribe(callback, event=None):
    """Register callback(event, data). event=None receives everything."""
    with _lock:
        _listeners.append((event, callback, None))


def unsubscribe(callback):
    with _lock:
        _listeners[:] = [(n, c, q) for (n, c, q) in _listeners if c is not callback]


# ------------------------------------------------------------- emergency stop

def emergency_stop(reason="user requested"):
    """Immediately flag all loops to stop. Safe-to-stop operations must honor it."""
    _stop_flag.set()
    emit("emergency_stop", {"reason": reason, "ts": time.time()})


def clear_stop():
    _stop_flag.clear()


def is_stopped():
    return _stop_flag.is_set()


def check_stop():
    """Convenience for loops: raise if emergency stop is active."""
    if _stop_flag.is_set():
        raise EmergencyStop("emergency stop active")


class EmergencyStop(Exception):
    pass

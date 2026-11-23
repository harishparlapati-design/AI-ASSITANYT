"""Per-request tenant scope.

Local desktop use is the tenant ``local`` and keeps the flat data folders it
has always used. A hosted instance signs users in; each request then carries
that user's tenant id, and generated documents, media and job history land in
a folder owned by that user. Nothing client-supplied ever becomes a path
component: ids are reduced to letters, digits and underscores first.
"""

import contextvars
import re

LOCAL = "local"
_current = contextvars.ContextVar("harry_tenant", default=LOCAL)


def safe_name(value):
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_")
    return (text[:40] or LOCAL)


def current():
    return _current.get()


def set_tenant(value):
    """Set the tenant for this request/thread. Returns a reset token."""
    return _current.set(safe_name(value))


def reset(token):
    try:
        _current.reset(token)
    except (ValueError, RuntimeError):
        pass


def is_local():
    return _current.get() == LOCAL

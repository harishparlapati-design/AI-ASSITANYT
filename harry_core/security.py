"""Security and permission system.

Every tool call passes through the PermissionManager:

  SAFE        - read/search/analyze/open           -> auto-allowed
  CONTROLLED  - create/edit/run/install            -> allowed, audited loudly
  SENSITIVE   - delete/send/upload/credentials/
                external communication/financial   -> require confirmation

Confirmation can come from:
  - an interactive console prompt (default)
  - a registered confirmer callback (e.g. the UI shows an "Approve" button)

An emergency stop (events.emergency_stop) aborts everything that is safe to
stop; sensitive operations additionally always require a fresh confirmation.
"""

import logging
import threading

from . import config, events
from . import logging_setup as logsys

log = logging.getLogger("harry.security")

SAFE = "safe"
CONTROLLED = "controlled"
SENSITIVE = "sensitive"

# Words that classify an action description into a danger level.
SENSITIVE_MARKERS = (
    "delete", "remove", "format", "shutdown", "restart", "reboot",
    "uninstall", "drop ", "wipe", "overwrite", "git push", "send ", "email",
    "upload", "password", "credential", "payment", "purchase", "buy",
    "transfer", "curl -x post", "invoke-webrequest -method post",
)
CONTROLLED_MARKERS = (
    "write", "create", "edit", "modify", "move ", "rename", "install",
    "pip install", "npm install", "git commit", "git clone", "run ",
    "terminal", "powershell", "cmd ", "start ", "launch", "open app",
    "kill process", "download", "screenshot", "type ", "click",
)


def classify(action_desc):
    d = str(action_desc).lower()
    if any(m in d for m in SENSITIVE_MARKERS):
        return SENSITIVE
    if any(m in d for m in CONTROLLED_MARKERS):
        return CONTROLLED
    return SAFE


class PermissionDenied(Exception):
    pass


class PermissionManager:
    def __init__(self):
        self._confirmer = None          # callable(action_desc, level) -> bool
        self._ui_lock = threading.Lock()
        self.require_for = config.CONFIG["security"].get(
            "require_confirmation_for", "sensitive"
        )
        self.allowed_dirs = config.CONFIG["security"].get("allowed_dirs", [])

    # -- confirmer registration (UI / console) ----------------------------

    def set_confirmer(self, fn):
        """fn(action_desc: str, level: str) -> bool (may block until user answers)"""
        self._confirmer = fn

    def confirm(self, action_desc, level=SENSITIVE):
        """Ask the user to confirm an action. Defaults to console prompt."""
        events.emit("permission_request", {"action": action_desc, "level": level})
        logsys.audit("permission_request", action_desc, level=level)
        if self._confirmer:
            try:
                allowed = bool(self._confirmer(action_desc, level))
            except Exception as e:
                log.error("confirmer failed: %s", e)
                allowed = False
        else:
            allowed = self._console_confirm(action_desc, level)
        logsys.audit("permission_result", action_desc, level=level, allowed=allowed)
        events.emit("permission_result", {"action": action_desc, "allowed": allowed})
        return allowed

    def _console_confirm(self, action_desc, level):
        try:
            print(f"\n⚠  [{level.upper()}] Harry wants to: {action_desc}")
            ans = input("   Approve? [y/N]: ").strip().lower()
            return ans in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    # -- path policy -------------------------------------------------------

    def path_allowed(self, path):
        if not self.allowed_dirs:
            return True
        import os
        p = os.path.abspath(str(path))
        return any(p.startswith(os.path.abspath(d)) for d in self.allowed_dirs)

    # -- gate --------------------------------------------------------------

    def check(self, action_desc, level=None, context=None):
        """Gate a tool call. Returns True to proceed; raises PermissionDenied."""
        if events.is_stopped():
            raise PermissionDenied("emergency stop active")
        level = level or classify(action_desc)
        logsys.audit("tool_attempt", action_desc, level=level, context=context)
        if level == SENSITIVE and self.require_for != "none":
            if not self.confirm(action_desc, level):
                logsys.audit("tool_denied", action_desc, level=level)
                raise PermissionDenied(f"user denied: {action_desc}")
        return True


_pm = None


def get_permissions():
    global _pm
    if _pm is None:
        _pm = PermissionManager()
    return _pm

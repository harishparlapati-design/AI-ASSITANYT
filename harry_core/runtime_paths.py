"""Runtime path resolution, including PyInstaller-frozen (EXE) mode.

Source mode (python main.py):
  - everything resolves to the project root  (unchanged behavior)

Frozen mode (Harry.exe):
  - workspace_root(): writable user home for config/data/logs.
      1) HARRY_HOME env var, if set
      2) %LOCALAPPDATA%\\Harry  (created on demand)
    Preserves memory and settings across app updates because the EXE directory
    itself is never written to.
  - bundled_dir(): read-only assets inside the distribution.
      sys._MEIPASS (onefile) or the EXE/_internal directory (onedir)
  - find_asset(): locate a runtime file (model dir, .ppn, voice profile,
    config template) in workspace -> bundle -> CWD order.
"""

import os
import sys


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def _source_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundled_dir():
    """Read-only directory holding bundled assets (models, tools, templates)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return meipass
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return _source_root()


def workspace_root():
    """Writable user workspace: config.json, data/, logs/, tools cache."""
    if not is_frozen():
        return _source_root()
    base = os.environ.get("HARRY_HOME", "")
    if not base:
        localappdata = os.environ.get(
            "LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local")
        )
        base = os.path.join(localappdata, "Harry")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = os.path.expanduser("~")
        os.makedirs(base, exist_ok=True)
    return base


def find_asset(name, extra_dirs=None):
    """Return the first existing path for a runtime asset, else None.

    Search order: extra_dirs -> workspace -> bundle -> CWD.
    """
    name = str(name or "")
    if os.path.isabs(name):
        return name if os.path.exists(name) else None
    dirs = []
    if extra_dirs:
        dirs.extend(extra_dirs)
    dirs.append(workspace_root())
    dirs.append(bundled_dir())
    if is_frozen():
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))  # next to the EXE
    dirs.append(os.getcwd())
    for d in dirs:
        candidate = os.path.join(d, name)
        if os.path.exists(candidate):
            return candidate
    return None

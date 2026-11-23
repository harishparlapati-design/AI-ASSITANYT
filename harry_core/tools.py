"""Tool system.

Tools are plain callables registered by name with metadata:

    register(Tool(
        name="read_file",
        description="Read a text file",
        danger="safe",                # safe | controlled | sensitive
        func=my_func,                 # func(args: dict) -> dict  {ok, result/error}
        schema={"path": "str"},       # human-readable arg hints
    ))

The LLM chooses tools; the registry validates permission level via the
security system and executes with error capture. Legacy harry.py tools in
tools/ (run(command, talk)) are auto-wrapped as command-match tools.
"""

import importlib
import json
import logging
import os
import re
import threading

from . import config, events
from . import logging_setup as logsys
from .security import SAFE, CONTROLLED, SENSITIVE, get_permissions, classify, PermissionDenied

log = logging.getLogger("harry.tools")


class Tool:
    def __init__(self, name, description, func, danger=SAFE, schema=None, source="builtin"):
        self.name = name
        self.description = description
        self.func = func                  # func(args: dict) -> dict
        self.danger = danger
        self.schema = schema or {}
        self.source = source

    def run(self, args=None):
        args = args or {}
        pm = get_permissions()
        try:
            pm.check(self.description or self.name, level=self.danger,
                     context={"tool": self.name, "args": args})
        except PermissionDenied as e:
            return {"ok": False, "error": f"permission denied: {e}", "denied": True}
        try:
            result = self.func(args)
            logsys.audit("tool_result", self.name,
                         ok=bool(result.get("ok")) if isinstance(result, dict) else True)
            return result if isinstance(result, dict) else {"ok": True, "result": result}
        except events.EmergencyStop:
            raise
        except Exception as e:
            log.exception("tool %s failed", self.name)
            # A failing tool is diagnostic evidence: record it against the
            # capability and let the self-healing engine classify the component.
            try:
                from . import capabilities as _caps, selfheal as _heal
                _caps.record_error(self.name, e)
                _heal.report_error("auto", f"{self.name}: {type(e).__name__}: {e}",
                                   context=self.source)
            except Exception:
                pass
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class ToolRegistry:
    def __init__(self):
        self._tools = {}
        self._lock = threading.RLock()

    def register(self, tool):
        with self._lock:
            self._tools[tool.name] = tool
        events.emit("tool_registered", {"name": tool.name, "danger": tool.danger})

    def get(self, name):
        return self._tools.get(name)

    def names(self):
        return sorted(self._tools.keys())

    def all(self):
        return dict(self._tools)

    def catalog(self):
        """Compact catalog for LLM prompts."""
        lines = []
        for name in sorted(self._tools):
            t = self._tools[name]
            args = ", ".join(f"{k}:{v}" for k, v in t.schema.items())
            lines.append(f"- {name}({args}) [{t.danger}]: {t.description}")
        return "\n".join(lines)

    def run(self, name, args=None):
        tool = self.get(name)
        if not tool:
            return {"ok": False, "error": f"unknown tool: {name}"}
        logsys.audit("tool_call", name, args=json.dumps(args or {}, ensure_ascii=False)[:500])
        return tool.run(args)


_registry = None


def get_registry():
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        from .builtin_tools import register_builtins  # lazy: avoids import cycle
        register_builtins(_registry)
        load_legacy_tools(_registry)
        try:
            from .autonomy_tools import register as register_autonomy
            register_autonomy(_registry)
        except Exception as _e:  # pragma: no cover - defensive
            import logging
            logging.getLogger("harry.tools").warning(
                "autonomy tools not registered: %s", _e)
    return _registry


def register(name, description, func, danger=SAFE, schema=None):
    get_registry().register(Tool(name, description, func, danger, schema))


# --------------------------------------------------------------------------- legacy tools/

def _wrap_legacy_tool(module, filename):
    """Wrap an old-style tool module exposing run(command, talk) -> bool."""

    def _run(args):
        command = str(args.get("command", ""))
        chunks = []

        def talk(text):
            chunks.append(str(text))

        try:
            handled = module.run(command, talk)
        except events.EmergencyStop:
            raise
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {
            "ok": True,
            "result": " | ".join(chunks) if chunks else "",
            "handled": bool(handled),
        }

    return Tool(
        name=f"legacy_{filename}",
        description=f"Legacy tool {filename}: responds if the command matches its keywords",
        func=_run,
        danger=CONTROLLED,
        schema={"command": "str"},
        source="legacy",
    )


def load_legacy_tools(registry):
    from . import runtime_paths
    folder = os.path.join(runtime_paths.bundled_dir(), "tools")
    if os.path.isdir(folder):
        files = [file for file in sorted(os.listdir(folder))
                 if file.endswith(".py") and file != "__init__.py"]
    else:
        # Frozen mode: the tools/ source folder is not shipped as data; the
        # modules live in the bundled archive and names come from the build info.
        try:
            from harry_build_info import TOOL_MODULES
        except Exception:
            return
        files = [name + ".py" for name in sorted(TOOL_MODULES)]
    for file in files:
        modname = file[:-3]
        if modname.startswith("tools."):
            modname = modname[len("tools."):]
        try:
            module = importlib.import_module(f"tools.{modname}")
            if hasattr(module, "run") and callable(module.run):
                registry.register(_wrap_legacy_tool(module, modname))
                log.info("legacy tool loaded: %s", modname)
        except Exception as e:
            log.warning("legacy tool %s failed to load: %s", modname, e)

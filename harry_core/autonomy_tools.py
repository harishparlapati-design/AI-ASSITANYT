"""Tools that expose self-diagnosis, self-repair and the registry to the planner.

Repair is privileged: the tool refuses unless the web layer verified an
administrator elevation for this request. That check happens on the server, not
in the prompt, so a task plan cannot talk its way past it.
"""

from . import admin, capabilities, monitoring, selfdev, selfheal
from .security import SAFE, CONTROLLED


def _denied():
    return {"ok": False, "error": "administrator mode is required for self-repair",
            "needs_admin": True}


def self_diagnose(args):
    component = (args or {}).get("component")
    checks = selfheal.diagnose(component)
    unhealthy = [name for name, item in checks.items() if not item.get("ok")]
    return {"ok": True, "unhealthy": unhealthy,
            "checks": {name: {"ok": item.get("ok"),
                              "problems": item.get("problems", [])}
                       for name, item in checks.items()}}


def self_repair(args):
    if not admin.request_admin():
        return _denied()
    component = str((args or {}).get("component") or "")
    if not component:
        results = {name: selfheal.heal(name)
                   for name, item in selfheal.diagnose().items()
                   if not item.get("ok")}
        return {"ok": True, "results": results,
                "repaired": [n for n, r in results.items() if r.get("repaired")]}
    return selfheal.heal(component)


def capability_status(args):
    capabilities.sync_tools()
    items = capabilities.list_capabilities()
    status = str((args or {}).get("status") or "")
    if status:
        items = [i for i in items if i.get("status") == status]
    return {"ok": True, "count": len(items),
            "items": [{"name": i["name"], "status": i["status"],
                       "version": i["version"], "source": i["source"]}
                      for i in items[:60]]}


def upgrade_status(args):
    return {"ok": True, "items": selfdev.list_upgrades(10),
            "backups": len(selfdev.backups())}


def system_health(args):
    snapshot = monitoring.check(include_network=False, include_voice=False)
    return {"ok": True, "at": snapshot["at"], "healthy": snapshot["ok"],
            "failed": snapshot.get("failed", []),
            "alerts": snapshot.get("alerts", [])}


def register(registry):
    registry.register(_tool("self_diagnose", "Run Harry's own health checks and list "
                            "unhealthy components.", self_diagnose, SAFE))
    registry.register(_tool("self_repair", "Repair a component that has a safe automated "
                            "repair. Requires administrator mode.", self_repair, CONTROLLED))
    registry.register(_tool("capability_status", "List capabilities with version, "
                            "source and status.", capability_status, SAFE))
    registry.register(_tool("upgrade_status", "List staged, active and rolled-back "
                            "upgrades plus checkpoints.", upgrade_status, SAFE))
    registry.register(_tool("system_health", "Current CPU, memory, disk, model, job and "
                            "component health snapshot.", system_health, SAFE))
    return len(registry.names())


def _tool(name, description, func, danger):
    from .tools import Tool
    return Tool(name, description, func, danger, {}, source="builtin")

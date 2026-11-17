"""Import every harry_core subsystem and report failures.

Run: python tests/import_check.py

Used by CI as a fast structural check: every module in the package must import
on a machine that only has the portable core dependencies installed. Optional
features (voice, screen capture, automation) degrade at runtime instead of
breaking import time, so a failure here is a real defect.
"""

import importlib
import os
import pkgutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import harry_core  # noqa: E402


def main():
    failures = []
    modules = sorted(m.name for m in pkgutil.iter_modules(harry_core.__path__))
    for name in modules:
        try:
            importlib.import_module("harry_core." + name)
        except Exception as exc:  # noqa: BLE001 - report every failure, don't stop
            failures.append((name, f"{type(exc).__name__}: {exc}"))

    print(f"checked {len(modules)} modules")
    for name, error in failures:
        print(f"  FAILED {name}: {error}")
    print("result:", "ok" if not failures else f"{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

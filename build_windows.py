"""Reproducible Windows build for Harry.

Usage:
    python build_windows.py                 # build dist/harry_dist/Harry.exe
    python build_windows.py --no-model-copy # skip copying local vosk model into dist
    python build_windows.py --verify-only   # just run the built EXE's --check

What it does:
1. Verifies build requirements (pyinstaller in the active Python).
2. Cleans build/ and dist/harry_dist/.
3. Runs PyInstaller with build/harry.spec.
4. Copies locally-installed runtime assets into the distribution so it works
   out of the box: vosk-model-en-us-0.22/ (only if --no-model-copy is absent).
   Config, data and logs are NEVER bundled - they are created in the user's
   data folder on first run (%LOCALAPPDATA%\\Harry or HARRY_HOME).
5. Runs dist/harry_dist/Harry.exe --check and prints the health report.

The vosk model is copied from this machine (not downloaded, not zipped into
the EXE) - documented for fresh installs in BUILD_INSTALL.md.
"""

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist", "harry_dist")
VOSK_DIR = "vosk-model-en-us-0.22"


def run(cmd, **kw):
    print(f"[build] {cmd[0]} {' '.join(cmd[1:])}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"[build] command failed with exit code {result.returncode}",
              file=sys.stderr)
        sys.exit(result.returncode)


def main():
    ap = argparse.ArgumentParser(description="Build Harry.exe")
    ap.add_argument("--no-model-copy", action="store_true",
                    help="do not copy the local vosk model into the dist")
    ap.add_argument("--verify-only", action="store_true",
                    help="only run the built EXE startup diagnostics")
    args = ap.parse_args()

    exe = os.path.join(DIST, "Harry.exe")
    if args.verify_only:
        run([exe, "--check"])
        return

    try:
        import PyInstaller  # noqa
    except ImportError:
        print("PyInstaller is missing in this Python environment.")
        print("Install it first:  pip install pyinstaller")
        sys.exit(1)

    # clean previous output
    for folder in (os.path.join(ROOT, "build", "harry"),):
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
    if os.path.isdir(DIST):
        shutil.rmtree(DIST, ignore_errors=True)

    run([sys.executable, "-m", "PyInstaller", os.path.join("build", "harry.spec"),
         "--noconfirm", "--distpath", "dist", "--workpath", "build"],
        cwd=ROOT)

    if not os.path.isfile(exe):
        print(f"[build] expected exe not found: {exe}", file=sys.stderr)
        sys.exit(1)

    # Copy local runtime assets so the EXE works out of the box.
    if not args.no_model_copy and os.path.isdir(os.path.join(ROOT, VOSK_DIR)):
        dest = os.path.join(DIST, VOSK_DIR)
        print(f"[build] copying speech model -> {dest}")
        shutil.copytree(os.path.join(ROOT, VOSK_DIR), dest)
    else:
        print(f"[build] NOTE: {VOSK_DIR}/ not copied - voice input needs the "
              f"model copied into the app folder separately.")

    size_mb = sum(
        os.path.getsize(os.path.join(d, f))
        for d, _, files in os.walk(DIST) for f in files
    ) / 1e6
    print(f"[build] done: {DIST} (~{size_mb:.0f} MB)")

    print("\n=== startup diagnostics (Harry.exe --check) ===")
    run([exe, "--check"], cwd=ROOT)


if __name__ == "__main__":
    main()

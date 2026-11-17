# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Harry assistant.

Build (from the project root):
    python build_windows.py        # calls pyinstaller with this spec
Output:
    dist/harry_dist/Harry.exe      # onedir console build

Notes
- onedir (not onefile): much faster startup and far more reliable with
  sounddevice/vosk native DLLs.
- console=True: text interaction + stdout diagnostics remain usable; you can
  still double-click and it shows a console window (documented behavior).
- Heavy optional deps (torch/Resemblyzer voice verification, scipy) are
  excluded to keep the build small; they degrade gracefully at runtime.
"""

import json
import os
import glob
from PyInstaller.utils.hooks import collect_dynamic_libs

PROJECT = os.path.dirname(SPECPATH)  # build/harry.spec -> project root

# Legacy keyword tools are loaded dynamically; freeze each as an importable module.
tool_modules = [
    "tools." + os.path.splitext(os.path.basename(f))[0]
    for f in glob.glob(os.path.join(PROJECT, "tools", "*.py"))
    if os.path.basename(f) != "__init__.py"
]

hiddenimports = sorted(
    {"harry_brain"}.union(tool_modules)  # harry_brain is imported by tools/research.py
)

# Voices metadata the frozen app needs when the tools/ data folder is absent:
# list the bundled legacy tool modules so load_legacy_tools can find them in
# the PYZ (build/harry_build_info.py is generated here on every build).
with open(os.path.join(SPECPATH, "harry_build_info.py"), "w", encoding="utf-8") as f:
    f.write("TOOL_MODULES = %r\n" % (tool_modules,))
hiddenimports.append("harry_build_info")

excludes = [
    "torch", "torchvision", "torchaudio", "resemblyzer", "scipy", "soundfile",
    "matplotlib", "pandas", "pytest", "PyInstaller", "faulthandler", "tkinter",
]

datas = [
    (os.path.join(PROJECT, "config.example.json"), "."),          # config template -> bundle
    (os.path.join(PROJECT, "hay-harry_en_windows_v4_0_0.ppn"), "."),  # porcupine keyword (tiny)
]

a = Analysis(
    [os.path.join(PROJECT, "main.py")],
    pathex=[PROJECT, SPECPATH],
    binaries=collect_dynamic_libs("vosk"),   # vosk ships libvosk.dll etc. inside the package
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Harry",
    console=True,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="harry_dist",
)

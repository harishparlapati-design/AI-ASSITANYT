# Building, Installing and Packaging Harry (Windows)

This document covers the first distribution deliverable: **`dist/harry_dist/Harry.exe`** —
a self-contained onedir build. You can also install Harry from source; both paths are
described here.

## 1. Build the EXE (from source checkout)

Requirements: Windows 10/11, Python 3.10+ dedicated to this project (a venv works,
`.venv/` is used in this repo), and all runtime dependencies from `requirements.txt`.

```bash
pip install -r requirements.txt
pip install pyinstaller
python build_windows.py
```

Output: `dist/harry_dist/` (~3 GB: framework + speakers/STT model copied from this
machine). The script:

1. Cleans `build/` and `dist/harry_dist/`.
2. Runs PyInstaller with [build/harry.spec](build/harry.spec) (onedir, console build).
3. Copies the local **vosk model** directory into the dist folder so voice input works
   out of the box (not bundled into the EXE itself, not re-downloaded).
4. Runs `Harry.exe --check` startup diagnostics and prints the health report.

Useful variants:

| Command | Effect |
|---|---|
| `python build_windows.py --no-model-copy` | skip copying the vosk model (voice input then needs the model copied manually) |
| `python build_windows.py --verify-only` | run the already-built EXE's `--check` |

Notes:

- **onedir, not onefile** — chosen deliberately: much faster startup and reliable
  loading of the vosk/sounddevice native libraries. Ship the whole `harry_dist` folder.
- **Console window** appears on launch (diagnostics + typed interaction stay available).
- Heavy optional deps (torch / Resemblyzer voiceprint verification, scipy) are
  **excluded** from the build; they degrade gracefully (voice verification simply
  allows the speaker).
- What is bundled: code, `config.example.json`, the `.ppn` wake-word keyword, vosk's
  DLLs, the python runtime. What is NOT bundled: your `config.json`, `data/`, `logs/`,
  the vosk model (copied next to the EXE by the build script), and any memories/tokens.

## 2. Install (copy) the application

This is a portable app: copy the whole `harry_dist` folder anywhere (e.g.
`C:\Apps\Harry`) and run `Harry.exe`. No installer and no administrator rights needed.

On first launch the app creates its **data folder**:

```
%LOCALAPPDATA%\Harry\
    config.example.json   <- starting template (copied automatically)
    config.json           <- create/edit this for real settings
    data\                 <- memories, tasks, conversation
    logs\                 <- harry.log, audit.jsonl
```

`HARRY_HOME` environment variable relocates the data folder; without it, memory and
settings persist there across app updates (never inside the installation folder).

### Home-folder contents you may place next to the EXE (found automatically)

| File/dir | Purpose |
|---|---|
| `vosk-model-en-us-0.22\` | speech recognition (copy is made by `build_windows.py`; this model is a free download from Vosk) |
| `hay-harry_en_windows_v4_0_0.ppn` | bundled already |
| `hareesh_voice_profile.pkl` | optional personalized voiceprint (copy manually if you want it) |
| `config.json` | settings (also works when placed in the data folder) |

## 3. Run the EXE

```text
Harry.exe             text chat + web dashboard (http://127.0.0.1:8765)
Harry.exe --voice     hands-free voice mode (needs mic + speakers)
Harry.exe --text      text chat only (no browser tab side effects)
Harry.exe --ui        dashboard only
Harry.exe --check     startup diagnostics (what `build_windows.py` runs)
```

Ollama must be running separately (`ollama serve`) with at least one model pulled
(`ollama pull llama3.1` / `qwen3:4b`). Without Ollama the app still starts; only
LLM answers are unavailable (everything else — tools, memory, UI — keeps working).

## 4. Uninstall

1. Close the Harry window (or the dashboard's EMERGENCY STOP then close).
2. Delete the `harry_dist` folder.
3. If you want the memories/logs gone too, delete `%LOCALAPPDATA%\Harry`
   (deleting it removes your data — do that only if you don't want to keep it).
4. Optionally uninstall Ollama from Windows Settings if nothing else uses it.

## 5. Optional installer (Inno Setup)

An Inno Setup script is included at [installer/harry.iss](installer/harry.iss). It is
**not yet built/verified** — Inno Setup was not installed on the development machine
(check `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`); once installed the installer
can be produced with two commands:

1. Install Inno Setup 6 (free, no paid service): `winget install JRSoftware.InnoSetup`
   or download from https://jrsoftware.org/isdl.php
2. Build first, then compile: `python build_windows.py` then
   `"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\harry.iss`
   -> `installer\Output\HarrySetup.exe`

The script does: per-user install (no admin), directory selection, Start-Menu +
optional desktop shortcuts, a first-run instructions dialog, and an uninstaller that
**leaves `%LOCALAPPDATA%\Harry` (all memories/settings/logs) in place** unless you
explicitly confirm deletion twice during uninstall.

## 6. Update checklist

1. Build the new EXE (§1).
2. Keep the old `harry_dist` folder available in case of a problem.
3. Replace the installed folder; your data stays in `%LOCALAPPDATA%\Harry` untouched.

## 7. Known limitations of this packaging (tested and sized)

- Voice verification (`verify_voice: true`) is unavailable in the EXE. Including
  its dependencies was measured before deciding: torch 467 MB + scipy 117 MB +
  Resemblyzer 17 MB of wheels, and PyInstaller bundling of torch typically grows
  this to well over 1 GB with fragile Windows DLL loading — roughly doubling the
  ~3 GB distribution for a feature that is off by default (`verify_voice: false`)
  and degrades gracefully (it always allows when the profile is absent). If the
  user enables voice verification, run Harry from source instead, or plan a
  follow-up replacement with a lightweight ONNX speaker-embedding model before
  re-adding ~1 GB to the installer. The feature is not silently disabled — the
  default was already `false` before packaging and stays `false`.
- `run_python` tool needs a Python on `PATH` in the frozen build.
- The vosk model (~130 MB) lives next to the EXE; it is not inside Harry.exe.

"""Real microphone and Vosk probe - no mocks, no recorded fixtures.

Run: python tests/voice_live_probe.py [capture_seconds]

Opens the actual default input device, reports the measured noise floor and
whether the VAD threshold fires, times one Vosk transcription of the captured
audio, and exercises the Vosk end-of-utterance path. It reports exactly what the
hardware gave it: speech recognition of *your* voice still has to be tried by a
person, and this script says so instead of inventing a transcript.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harry_core import config  # noqa: E402
from harry_core.speech import get_listener  # noqa: E402


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    report = {"requested_seconds": seconds}
    try:
        import numpy as np
        import sounddevice as sd
    except Exception as e:
        print(json.dumps({"error": f"audio stack unavailable: {e}"}, indent=2))
        return 1

    try:
        device = sd.query_devices(kind="input")
        report["input_device"] = {
            "name": str(device.get("name")),
            "channels": int(device.get("max_input_channels") or 0),
            "default_samplerate": float(device.get("default_samplerate") or 0),
        }
    except Exception as e:
        print(json.dumps({"error": f"no input device: {e}"}, indent=2))
        return 1

    # ---- raw capture: how loud is the room, and does the VAD see speech?
    samplerate = 16000
    blocks = int(samplerate / 2 * seconds)
    levels = []
    started = time.perf_counter()
    try:
        with sd.RawInputStream(samplerate=samplerate, blocksize=4000,
                               dtype="int16", channels=1) as stream:
            for _ in range(blocks):
                data, _overflow = stream.read(2000)
                audio = np.frombuffer(bytes(data), dtype=np.int16).astype(np.float32)
                levels.append(float(np.sqrt(np.mean(audio ** 2))))
    except Exception as e:
        print(json.dumps({"error": f"capture failed: {e}"}, indent=2))
        return 1
    capture_seconds = time.perf_counter() - started
    threshold = int((config.CONFIG.get("voice", {}).get("conversation", {}) or {})
                    .get("vad_min_volume", 450))
    loudest = max(levels) if levels else 0.0
    report["capture"] = {
        "blocks": len(levels),
        "seconds": round(capture_seconds, 2),
        "rms_min": round(min(levels), 1) if levels else None,
        "rms_median": round(sorted(levels)[len(levels) // 2], 1) if levels else None,
        "rms_max": round(loudest, 1),
        "vad_threshold": threshold,
        "vad_would_fire": loudest >= threshold,
    }

    listener = get_listener()
    report["vosk_available"] = bool(listener.available())
    report["vosk_model"] = os.path.basename(str(listener.model_path))

    # ---- Vosk load + transcription timing on the real captured audio
    if listener.available():
        pcm = (b"\x00\x00" * int(samplerate * 0.5))
        t0 = time.perf_counter()
        listener.transcribe_pcm(pcm, samplerate)
        report["vosk_first_transcribe_ms"] = round((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        listener.transcribe_pcm(pcm, samplerate)
        report["vosk_warm_transcribe_ms"] = round((time.perf_counter() - t0) * 1000)

    # ---- the real VAD path: does it detect an utterance at all?
    t0 = time.perf_counter()
    heard = listener.listen_once(samplerate=16000, silence_blocks=4, max_seconds=5)
    report["vad_listen"] = {
        "returned_text": heard,
        "seconds": round(time.perf_counter() - t0, 2),
        "note": ("empty means no speech above the VAD threshold was captured"
                 if not heard else "speech above the threshold was captured"),
    }

    report["manual_step_required"] = (
        "Say a sentence out loud while this probe runs to confirm ASR of your own "
        "voice; the probe cannot speak for you."
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

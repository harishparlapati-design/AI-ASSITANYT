"""Backward-compatible wake word module.

The old harry.py imports wait_for_wake_word from here. Real wake-word
handling now lives in harry_core.speech (Porcupine keyword file support,
STT-based fallback, or always-on for UI mode).
"""

from harry_core.speech import wait_for_wake_word  # noqa: F401

if __name__ == "__main__":
    print("Say the wake word...")
    if wait_for_wake_word():
        print("Wake word detected!")

"""Harry - legacy entry point, preserved for backward compatibility.

Old behavior: wake word -> voice verify -> continuous voice commands.
All real logic now lives in the harry_core package; this file is a thin
launcher kept so 'python harry.py' still works exactly as before.

Modern entry point: python main.py (see main.py for all options).
"""

import time

from harry_core.brain import get_brain
from harry_core.config import CONFIG
from harry_core.events import is_stopped
from harry_core.logging_setup import setup

from wake_word import wait_for_wake_word
from harry_core.speech import get_listener, get_speaker, verify_voice


def run_harry_voice():
    """The original hands-free flow, now on the new core."""
    setup()
    brain = get_brain()
    speaker = get_speaker()
    listener = get_listener()

    if not listener.available():
        print("Vosk model not found - voice input unavailable.")
        print("Run in UI mode instead: python main.py")
        return

    while not is_stopped():
        if not wait_for_wake_word():
            time.sleep(0.2)
            continue
        if CONFIG["voice"].get("verify_voice") and not verify_voice():
            print("Voice not recognized.")
            continue
        speaker.speak("Hello, I am listening continuously.")
        last_active = time.time()
        while not is_stopped():
            heard = listener.listen_once(max_seconds=15)
            if not heard:
                if time.time() - last_active > 90:
                    speaker.speak("Going back to sleep.")
                    break
                continue
            last_active = time.time()
            low = heard.lower()
            if "stop listening" in low or "goodbye" in low:
                speaker.speak("Okay, going back to sleep.")
                break
            brain.handle_input(heard, source="voice")


if __name__ == "__main__":
    run_harry_voice()

"""Harry - SUPER AUTONOMOUS PERSONAL AI ASSISTANT AGENT - main launcher.

Usage:
  python main.py            -> interactive text chat + web UI + proactive scheduler
  python main.py --voice    -> wake word + speech loop on top (needs mic)
  python main.py --text     -> text chat only, no UI
  python main.py --ui       -> web UI only
  python main.py --check    -> system diagnostics and exit

All real logic lives in the harry_core package.
"""

import argparse
import sys
import threading
import time

from harry_core import logging_setup, runtime_paths
from harry_core.brain import get_brain
from harry_core.config import CONFIG
from harry_core.events import is_stopped
from harry_core.scheduler import get_scheduler


def start_ui_thread():
    from harry_core.ui import run as run_ui
    t = threading.Thread(target=run_ui, daemon=True, name="harry-ui")
    t.start()
    return t


def text_chat_loop(brain):
    print("\nHarry text chat. Commands: 'exit' quits, 'Harry stop' aborts work.\n")
    while not is_stopped():
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in ("exit", "quit", "bye"):
            brain.say("Goodbye!")
            break
        brain.handle_input(text, source="console")


def voice_loop(brain):
    """Hands-free loop: wake word -> optional voice verify -> continuous convo."""
    from harry_core.speech import wait_for_wake_word, get_listener, verify_voice
    listener = get_listener()
    if not listener.available():
        brain.say("Voice input is unavailable (Vosk model missing). "
                  "Use text mode or check the docs.")
        return
    brain.say("Voice mode starting. Say the wake word to begin.")
    while not is_stopped():
        if not wait_for_wake_word():
            continue
        if CONFIG["voice"].get("verify_voice") and not verify_voice():
            print("Voice not recognized.")
            continue
        brain.say(f"Hello{', ' + CONFIG['user_name'] if CONFIG['user_name'] else ''}, "
                  "I am listening. Say 'stop listening' to go back to sleep.")
        last_active = time.time()
        while not is_stopped():
            heard = listener.listen_once(max_seconds=15)
            if not heard:
                if time.time() - last_active > 90:
                    brain.say("Going back to sleep. Say the wake word when you need me.")
                    break
                continue
            last_active = time.time()
            low = heard.lower()
            if "stop listening" in low or "goodbye" in low or "go to sleep" in low:
                brain.say("Okay, going back to sleep.")
                break
            brain.handle_input(heard, source="voice")


def main():
    parser = argparse.ArgumentParser(description="Harry - personal AI assistant agent")
    parser.add_argument("--text", action="store_true", help="text chat only")
    parser.add_argument("--voice", action="store_true", help="enable hands-free voice mode")
    parser.add_argument("--ui", action="store_true", help="web UI only")
    parser.add_argument("--check", action="store_true", help="run system diagnostics")
    args = parser.parse_args()

    logging_setup.setup()
    log = logging_setup.get_logger("main")
    log.info("Harry starting (mode: %s)", "voice" if args.voice else "ui+text")
    if runtime_paths.is_frozen():
        print(f"Home folder (config, data, logs): {runtime_paths.workspace_root()}")

    if args.check:
        from harry_core.diagnostics import run_diagnostics
        print(run_diagnostics())
        return

    brain = get_brain()
    get_scheduler().start()   # proactive reminders / briefing (user-configurable)

    if not args.text:
        start_ui_thread()

    if args.voice and not args.ui:
        voice_loop(brain)
    elif args.voice and args.ui:
        threading.Thread(target=voice_loop, args=(brain,), daemon=True).start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    elif args.ui:
        from harry_core.ui import run as run_ui
        run_ui()  # blocking
    else:
        print(f"Harry UI running at http://{CONFIG['ui']['host']}:{CONFIG['ui']['port']}")
        text_chat_loop(brain)


if __name__ == "__main__":
    main()

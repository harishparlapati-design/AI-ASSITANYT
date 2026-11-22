"""Unit tests for Harry's response-language routing.

Run: python tests/language_policy_test.py

Pure functions only: no model, no network, no audio. These replace the manual
live checks that used to be the only coverage for the language policy.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harry_core import language_policy as lp  # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    try:
        result = fn()
        ok = bool(result)
    except Exception as e:
        result, ok = f"{type(e).__name__}: {e}", False
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -> {result}"))


def has(text, needle):
    return needle.lower() in str(text).lower()


# ------------------------------------------------------------- explicit requests
check("explicit English request",
      lambda: lp.explicit_language("please reply in English") == "english")
check("explicit Telugu request",
      lambda: lp.explicit_language("answer in Telugu please") == "telugu")
check("explicit Telugu script request",
      lambda: lp.explicit_language("దయచేసి తెలుగులో చెప్పు") == "telugu")
check("explicit mixed request",
      lambda: lp.explicit_language("reply in tenglish") == "mixed")
check("no override in ordinary message",
      lambda: lp.explicit_language("hello harry") is None)
check("literal output detection",
      lambda: lp.requires_literal_output("output only OK")
      and not lp.requires_literal_output("tell me about OK"))
check("'output only' is not read as a language request",
      lambda: lp.explicit_language("output only: READY") is None)

# --------------------------------------------------------------- response modes
check("greeting is general conversation",
      lambda: lp.response_mode("hello harry") == "general")
check("technical question is a task response",
      lambda: lp.response_mode("what is a capacitor") == "task")
check("action request is a task response",
      lambda: lp.response_mode("fix my portfolio gallery") == "task")
check("mixed Telugu-English task is a task response",
      lambda: lp.response_mode("na portfolio open chesi error check cheyyi") == "task")

# ------------------------------------------------------------------ instruction
auto_general = lp.instruction("hello", "auto")
check("auto preference uses mixed Telugu-English for chat",
      lambda: has(auto_general, "mixed Telugu-English") and has(auto_general, "code-switching"))
check("technical request defaults to English under auto",
      lambda: has(lp.instruction("what is a capacitor", "auto"),
                  "Response language policy: English."))
check("english preference is pure English",
      lambda: has(lp.instruction("hello", "english"), "Response language policy: English.")
      and not has(lp.instruction("hello", "english"), "mixed"))
check("telugu preference is Telugu",
      lambda: has(lp.instruction("hello", "telugu"), "Response language policy: Telugu."))
check("unknown preference falls back to mixed",
      lambda: has(lp.instruction("hello", "klingon"), "mixed Telugu-English"))
check("explicit request overrides the stored preference",
      lambda: has(lp.instruction("answer in english", "telugu"),
                  "Response language policy: English."))
check("instruction forbids translating code and identifiers",
      lambda: has(auto_general, "Do not translate code"))
literal = lp.instruction("say exactly: HARRY_OK", "auto")
check("literal request forbids extra wording",
      lambda: has(literal, "exact output format") and has(literal, "Do not add a greeting"))
check("literal request in English keeps English wording",
      lambda: has(lp.instruction("say exactly this in english", "auto"), "Use English"))

# -------------------------------------------------------------- fast chat replies
ENGLISH_HELLO = "Hello! How can I help?"
check("mixed greeting is the product default",
      lambda: "Ela unnaru" in lp.general_fast_reply(ENGLISH_HELLO, "hi", "auto"))
check("english preference leaves the English reply alone",
      lambda: lp.general_fast_reply(ENGLISH_HELLO, "hi", "english") == ENGLISH_HELLO)
check("telugu preference greets in Telugu",
      lambda: lp.general_fast_reply(ENGLISH_HELLO, "namaste", "telugu")
      == "Namaskaram! Nenu Harry. Ela unnaru?")
check("'hello harry' is recognised as a greeting",
      lambda: "Ela unnaru" in lp.general_fast_reply(ENGLISH_HELLO, "hello harry", "auto"))
check("thanks mirrored in Telugu",
      lambda: lp.general_fast_reply("You're welcome!", "thanks", "telugu")
      .startswith("Dhanyavadalu"))
check("who-are-you answer names Harry",
      lambda: "Harry" in lp.general_fast_reply("I'm Harry.", "who are you", "auto"))
check("time reply is localised for Telugu",
      lambda: lp.general_fast_reply("It's 5:03 PM.", "what time is it", "telugu")
      .startswith("Samayam"))
check("task replies are never rewritten",
      lambda: lp.general_fast_reply("Done.", "fix my portfolio gallery", "telugu") == "Done.")
check("literal replies are never rewritten",
      lambda: lp.general_fast_reply("READY", "output only READY", "telugu") == "READY")
check("unknown chat replies pass through unchanged",
      lambda: lp.general_fast_reply("Some answer.", "why is the sky blue", "auto")
      == "Some answer.")

print(f"\n===== RESULT: {len(PASS)} passed, {len(FAIL)} failed =====")
for failure in FAIL:
    print(f"  FAILED: {failure}")
sys.exit(1 if FAIL else 0)

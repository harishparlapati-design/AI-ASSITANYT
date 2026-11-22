"""Response-language routing shared by Harry's text and voice pipeline.

General conversation defaults to natural Telugu-English code-switching. Tasks
and technical requests default to English. A direct language request in the
current message takes precedence for that response; the dashboard preference
controls the default style and is stored in the normal user config.
"""

import re

# The only place a response language is described. Everything below picks a key.
_STYLES = {
    "english": ("English", "Use clear, natural English."),
    "telugu": ("Telugu", "Use natural Telugu in the user's script style."),
    "mixed": ("mixed Telugu-English",
              "Use natural conversational Telugu-English code-switching."),
}

_TECHNICAL = re.compile(
    r"\b(?:python|javascript|typescript|java|html|css|code|coding|program|"
    r"debug|refactor|compile|build|test|repository|repo|git|github|api|"
    r"endpoint|traceback|exception|dependency|deploy|server|database|sql|"
    r"voltage|current|resistance|capacitor|inductor|circuit|physics|"
    r"equation|derivative|integral|matrix|statistics|algorithm|architecture|"
    r"diagnostic|system check|health check|research|citation|source code)\b",
    re.IGNORECASE,
)
_TASK_ACTION = re.compile(
    r"\b(?:create|write|edit|change|update|fix|remove|delete|rename|move|copy|"
    r"install|run|start|stop|open|close|save|commit|push|pull|branch|inspect|"
    r"check|verify|test|search|research|find|compare|analy[sz]e|summari[sz]e|"
    r"configure|set up|schedule|automate|execute|implement|upgrade|redesign)\b",
    re.IGNORECASE,
)
_ENGLISH_REQUEST = re.compile(
    r"\b(?:reply|respond|answer|speak|explain|write|say)\b.{0,45}\benglish\b|"
    r"\benglish only\b|\bin english\b", re.IGNORECASE,
)
_TELUGU_REQUEST = re.compile(
    r"\b(?:reply|respond|answer|speak|explain|write|say)\b.{0,45}\b(?:telugu|"
    r"telugu-only|telugu only)\b|\bin telugu\b|\bతెలుగు(?:లో|లోనే| మాత్రమే)?\b",
    re.IGNORECASE,
)
_MIXED_REQUEST = re.compile(
    r"\b(?:reply|respond|answer|speak|explain|write|say)\b.{0,55}\b(?:mixed|"
    r"mix|telugu[ -]english|tenglish)\b|\bcode[ -]?switch\b",
    re.IGNORECASE,
)
_EXACT_OUTPUT = re.compile(
    r"\b(?:exactly|verbatim|only output|output only|return only)\b", re.I
)
_EXACT_POLICY = (
    "Follow the requested exact output format. "
)
_NO_STYLE_IMPOSED = (
    "Do not impose a language style on a quoted literal; reproduce the "
    "requested literal exactly."
)


def explicit_language(text):
    """Return an explicit one-turn language override, if present."""
    text = str(text or "")
    if _ENGLISH_REQUEST.search(text):
        return "english"
    if _TELUGU_REQUEST.search(text):
        return "telugu"
    if _MIXED_REQUEST.search(text):
        return "mixed"
    return None


def requires_literal_output(text):
    """Whether the user explicitly constrained the output to be literal."""
    return bool(_EXACT_OUTPUT.search(str(text or "")))


def response_mode(text):
    """Classify a message as ordinary conversation or a task/technical request."""
    text = str(text or "").strip()
    try:
        from .agents import select_agent
        if select_agent(text).key != "general":
            return "task"
    except Exception:
        pass
    if _TECHNICAL.search(text) or _TASK_ACTION.search(text):
        return "task"
    return "general"


def _preference_style(preference):
    """Map the stored preference onto a style key (unknown values = mixed)."""
    return preference if preference in _STYLES else "mixed"


def instruction(text, preference="auto"):
    """Build the language instruction appended to the model's system prompt."""
    override = explicit_language(text)
    if requires_literal_output(text):
        rule = (_NO_STYLE_IMPOSED if override is None else
                f"Use {_STYLES[override][0]} for any generated wording.")
        return (_EXACT_POLICY + rule + " Do not add a greeting, explanation, "
                "translation, or text outside the requested output.")

    if override:
        key = override
    elif response_mode(text) == "task":
        key = "english"
    else:
        key = _preference_style(preference)
    language, style = _STYLES[key]
    # Kept short on purpose: every prompt character is expensive on CPU-only
    # inference, and the policy only has to say which language wins.
    return (
        f"Response language policy: {language}. {style} Do not translate code, "
        "commands, filenames, identifiers or quoted text, and do not switch "
        "languages mid-response. An explicit language request overrides this."
    )


_GREETING = re.compile(
    r"(?:hi+|hello+|hey+|hai|namaste|namaskaram|"
    r"good\s+(?:morning|afternoon|evening|night))"
    r"(?:[\s,]+harry)?[\s!.?]*", re.IGNORECASE,
)
_THANKS = re.compile(r"(?:ok\s+)?(?:thanks|thank\s+you|thank\s+u|thx|tq)[\s!.?]*",
                     re.IGNORECASE)
_BYE = re.compile(r"(?:ok\s+)?(?:bye|goodbye|good\s+bye|see\s+you)[\s!.?]*",
                  re.IGNORECASE)
_WHO = re.compile(r"(?:who\s+are\s+you|what(?:'s| is)\s+your\s+name|your\s+name)"
                  r"[\s?.!]*", re.IGNORECASE)
_TIME_QUESTION = re.compile(
    r"\b(?:what(?:'s| is)? the time|what time|time now|current time|samayam)\b",
    re.IGNORECASE,
)

# Fixed wording per intent; ``mixed`` is the product's general-conversation
# default. English preference returns Brain's own English reply unchanged.
_FIXED = {
    "greeting": {
        "mixed": "Hello! Ela unnaru? What can I help you with today?",
        "telugu": "Namaskaram! Nenu Harry. Ela unnaru?",
    },
    "thanks": {
        "mixed": "You're welcome! Inka emaina help kavala?",
        "telugu": "Dhanyavadalu! Inka emaina help kavala?",
    },
    "bye": {
        "mixed": "Goodbye! Malli matladham.",
        "telugu": "Sare, malli kaluddam. Bye!",
    },
    "who": {
        "mixed": "I'm Harry, your personal AI assistant. Nenu questions answer "
                 "cheyyagalanu, tools use cheyyagalanu, and remember what you tell me.",
        "telugu": "Nenu Harry, mee personal AI assistant-ni.",
    },
}


def general_fast_reply(reply, text, preference="auto"):
    """Apply the language policy to Brain's deterministic general-chat replies."""
    if requires_literal_output(text) or response_mode(text) == "task":
        return reply
    override = explicit_language(text)
    if override == "english" or (override is None and preference == "english"):
        return reply
    telugu = override == "telugu" or (override is None and preference == "telugu")
    style = "telugu" if telugu else "mixed"
    low = str(text or "").strip().lower()

    for pattern, kind in ((_GREETING, "greeting"), (_THANKS, "thanks"),
                          (_BYE, "bye"), (_WHO, "who")):
        if pattern.fullmatch(low):
            return _FIXED[kind][style]
    if _TIME_QUESTION.search(low):
        if telugu:
            return reply.replace("It's ", "Samayam ").rstrip(".") + "."
        return reply.rstrip(".") + " — mee local time."
    if re.search(r"\b(?:today|date|ee roju)\b", low) and reply.startswith("Today is "):
        value = reply[len("Today is "):].rstrip(".")
        return f"Ee roju {value}." if telugu else f"Today is {value} — ee roju."
    return reply

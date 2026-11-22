"""Response-language routing shared by Harry's text and voice pipeline.

General conversation defaults to natural Telugu-English code-switching. Tasks
and technical requests default to English. A direct language request in the
current message takes precedence for that response; the dashboard preference
controls the default style and is stored in the normal user config.
"""

import re


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


def instruction(text, preference="auto"):
    """Build the language instruction appended to the model's system prompt."""
    override = explicit_language(text)
    if requires_literal_output(text):
        if override == "english":
            language_rule = "Use English for any generated wording."
        elif override == "telugu":
            language_rule = "Use Telugu for any generated wording."
        elif override == "mixed":
            language_rule = "Use natural Telugu-English for any generated wording."
        else:
            language_rule = (
                "Do not impose a language style on a quoted literal; reproduce "
                "the requested literal exactly."
            )
        return (
            "Follow the requested exact output format. " + language_rule + " "
            "Do not add a greeting, explanation, translation, or text outside "
            "the requested output."
        )

    mode = response_mode(text)
    if override == "english":
        language = "English"
        style = "Use clear, natural English."
    elif override == "telugu":
        language = "Telugu"
        style = "Use natural Telugu in the user's script style."
    elif override == "mixed":
        language = "mixed Telugu-English"
        style = "Use natural conversational Telugu-English code-switching."
    elif mode == "task":
        language = "English"
        style = "Use pure English for this task or technical response."
    elif preference == "english":
        language = "English"
        style = "Use clear, natural English."
    elif preference == "telugu":
        language = "Telugu"
        style = "Use natural Telugu in the user's script style."
    else:
        # auto and mixed both implement the product's general-conversation
        # default; task responses above remain English unless explicitly set.
        language = "mixed Telugu-English"
        style = "Use natural conversational Telugu-English code-switching."

    return (
        f"Response language policy: {language}. {style} Keep the user's meaning. "
        "Do not translate code, commands, filenames, technical identifiers, or "
        "quoted text. Do not switch languages mid-response. An explicit language "
        "request in the current user message overrides the default policy."
    )


def general_fast_reply(reply, text, preference="auto"):
    """Apply the language policy to Brain's deterministic general-chat replies."""
    if requires_literal_output(text) or response_mode(text) == "task":
        return reply
    override = explicit_language(text)
    if override == "english" or (preference == "english" and override is None):
        return reply
    low = str(text or "").strip().lower()
    telugu = override == "telugu" or preference == "telugu"
    if re.fullmatch(r"(?:hi|hello|hey|good morning|good afternoon|good evening)[!. ]*", low):
        return "Namaskaram! Nenu Harry. Ela unnaru?" if telugu else (
            "Hello! Ela unnaru? What can I help you with today?"
        )
    if re.fullmatch(r"(?:thanks|thank you|thx)[!. ]*", low):
        return "Dhanyavadalu! Inka emaina help kavala?" if telugu else (
            "You're welcome! Inka emaina help kavala?"
        )
    if re.fullmatch(r"(?:bye|goodbye|see you)[!. ]*", low):
        return "Sare, malli kaluddam. Bye!" if telugu else "Goodbye! Malli matladham."
    if re.fullmatch(r"(?:who are you|what is your name|what's your name)[?!. ]*", low):
        return ("Nenu Harry, mee personal AI assistant-ni." if telugu else
                "I'm Harry, your personal AI assistant. Nenu questions answer cheyyagalanu, "
                "tools use cheyyagalanu, and remember what you tell me.")
    if re.search(r"\b(?:what(?:'s| is)? the time|what time|time now|current time|samayam)\b", low):
        return (reply.replace("It's ", "Samayam ").rstrip(".") + ".") if telugu else (
            reply.rstrip(".") + " — mee local time."
        )
    if re.search(r"\b(?:today|date|ee roju)\b", low) and reply.startswith("Today is "):
        value = reply[len("Today is "):].rstrip(".")
        return f"Ee roju {value}." if telugu else f"Today is {value} — ee roju."
    return reply

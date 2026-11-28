"""Human-aware communication layer.

Two jobs, both deterministic - no model call and no guessing hidden state:

1. read the human side of a message: frustration, urgency, confusion, praise and
   how much detail was asked for;
2. put that reading into the prompt *and* shape the finished answer to match.

Rules this module keeps to:

* Only explicit signals count (\"still broken\", \"asap\", \"in short\"). A mood is
  never invented from topic or word count.
* Nothing is stored as a \"happiness score\". The reading is attached to the run
  record for one request, so the owner can see exactly what the reply adapted to.
* The owner's preference (``communication.verbosity`` in config.json) decides the
  default detail level; an explicit request in the message overrides it.
* Shaping never invents content, never marks work as done, and never removes a
  fact: it only drops filler, closing pleasantries and - in concise mode - the
  tail of a long answer, saying plainly that detail is available on request.
"""

import re

VERBOSITY_LEVELS = ("concise", "balanced", "detailed")

_FRUSTRATION = re.compile(
    r"\b(?:still (?:not|doesn'?t|won'?t|isn'?t)|again and again|broken|useless|"
    r"rubbish|waste of time|why (?:do|does|did|is|are) (?:it|this|that|you) "
    r"(?:always|keep|still)|not working|doesn'?t work|didn'?t work|failed again|"
    r"frustrat\w*|annoy\w*|angry|fed up|sick of|nonsense|stupid|"
    r"i already (?:told|said|asked))\b",
    re.IGNORECASE,
)
_URGENCY = re.compile(
    r"\b(?:asap|as soon as possible|right now|urgent\w*|hurry|immediately|"
    r"quickly|in a hurry|no time|deadline|real quick)\b",
    re.IGNORECASE,
)
_CONFUSION = re.compile(
    r"\b(?:i (?:don'?t|do not|cannot|can'?t) understand|confus\w*|"
    r"what do you mean|unclear|makes no sense|explain (?:that|it) again|"
    r"lost me)\b",
    re.IGNORECASE,
)
_PRAISE = re.compile(
    r"\b(?:great|perfect|excellent|awesome|brilliant|nice work|well done|"
    r"that worked|works now|thank you|thanks|love it)\b",
    re.IGNORECASE,
)

_SADNESS = re.compile(
    r"\b(?:i(?:'m| am) (?:\w+ )?(?:sad|depressed|miserable|exhausted|burnt out|"
    r"heartbroken)|feeling (?:low|awful|terrible|down)|bad day|awful day|"
    r"lost (?:my|a) (?:job|dog|cat|father|mother|friend)|passed away|grieving|"
    r"lonely|hopeless)\b",
    re.IGNORECASE,
)
# Crisis language gets a deterministic answer. A small local model must never be
# the only thing standing between a person in danger and useful advice.
_CRISIS = re.compile(
    r"\b(?:suicid\w*|kill (?:myself|me)|end (?:my|it) (?:life|all)|self[- ]?harm|"
    r"hurt (?:myself|me)|want to die|don'?t want to (?:live|be here)|"
    r"no reason to live|overdose)\b",
    re.IGNORECASE,
)

_CONCISE_REQUEST = re.compile(
    r"\b(?:briefly|in short|short answer|one line|one sentence|tl;?dr|"
    r"just the answer|keep it short|no detail)\b",
    re.IGNORECASE,
)
_DETAILED_REQUEST = re.compile(
    r"\b(?:in detail|detailed|thorough\w*|step by step|elaborate|"
    r"explain (?:fully|everything)|full explanation|walk me through)\b",
    re.IGNORECASE,
)

# Filler that adds nothing for any reader. Removed, never replaced by other text.
_FILLER_OPENERS = (
    re.compile(r"^(?:sure|certainly|of course|absolutely|indeed)"
               r"\s*[!,.—:-]+\s*", re.IGNORECASE),
    re.compile(r"^(?:great|good) question\s*[!,.—:-]*\s*", re.IGNORECASE),
    re.compile(r"^(?:happy to help|no problem|glad to help)"
               r"\s*[!,.—:-]*\s*", re.IGNORECASE),
    re.compile(r"^as an ai(?: language model)?[^.!?\n]*[.!?]\s*", re.IGNORECASE),
    re.compile(r"^i hope (?:this|that) helps[.!]?\s*", re.IGNORECASE),
)
_AI_DISCLAIMERS = re.compile(
    r"(?:^|\n)\s*(?:as an ai(?: language model)?[^.\n]*\.|"
    r"i(?:'m| am) (?:just )?an ai[^.\n]*\.)\s*",
    re.IGNORECASE,
)
_OFFER_TAIL = re.compile(
    r"\n+\s*(?:let me know|feel free to|hope (?:this|that) helps|"
    r"would you like me to|do you want me to|i can also)[^\n]*$",
    re.IGNORECASE,
)
_DEFAULT_PREFERENCE = "balanced"


# --------------------------------------------------------------------- reading

def detect(text):
    """Read the explicit human signals in one message.

    Returns scores rather than labels so a caller can tell one exasperated
    sentence from a repeated complaint (0, 1 or 2).
    """
    text = str(text or "")
    frustration = min(2, len(_FRUSTRATION.findall(text)))
    urgency = min(2, len(_URGENCY.findall(text)))
    signals = []
    if frustration:
        signals.append("frustration")
    if urgency:
        signals.append("urgency")
    if _CONFUSION.search(text):
        signals.append("confusion")
    if _PRAISE.search(text):
        signals.append("praise")
    if _SADNESS.search(text):
        signals.append("sadness")
    crisis = bool(_CRISIS.search(text))
    if crisis:
        signals.append("crisis")
    return {
        "frustration": frustration,
        "urgency": urgency,
        "confusion": bool(_CONFUSION.search(text)),
        "praise": bool(_PRAISE.search(text)),
        "sadness": bool(_SADNESS.search(text)),
        "crisis": crisis,
        "signals": signals,
    }


def preference():
    """The owner's configured default detail level, validated."""
    try:
        from . import config
        value = str((config.CONFIG.get("communication") or {})
                    .get("verbosity") or "").strip().lower()
    except Exception:
        value = ""
    return value if value in VERBOSITY_LEVELS else _DEFAULT_PREFERENCE


def verbosity(text, preferred=None):
    """Resolve how much detail to give: explicit request beats the preference."""
    preferred = preferred if preferred in VERBOSITY_LEVELS else preference()
    text = str(text or "")
    if _CONCISE_REQUEST.search(text):
        return "concise"
    if _DETAILED_REQUEST.search(text):
        return "detailed"
    return preferred


def read(text, preferred=None):
    """One call for the whole reading: signals plus the detail level."""
    state = detect(text)
    state["verbosity"] = verbosity(text, preferred)
    return state


# ----------------------------------------------------------------- prompt side

def prompt_guidance(state, intent=None):
    """Instruction block telling the model how to address this person now."""
    state = state or {}
    lines = []
    level = state.get("verbosity") or _DEFAULT_PREFERENCE
    if level == "concise":
        lines.append("Answer in at most three sentences with no preamble.")
    elif level == "detailed":
        lines.append("Give a thorough answer: every step that matters, in order.")
    if state.get("frustration", 0) >= 1:
        lines.append("The user is frustrated: lead with the fix or the direct "
                     "answer, and do not apologise repeatedly or sound cheerful.")
    if state.get("urgency", 0) >= 1:
        lines.append("The user needs this quickly: answer first, then any "
                     "detail, and say plainly if something will take time.")
    if state.get("confusion"):
        lines.append("Explain in plain language and define any term you use.")
    if state.get("praise"):
        lines.append("Acknowledge the praise in a few words, then continue.")
    if state.get("sadness"):
        lines.append("The user sounds low: acknowledge that in one plain "
                     "sentence before any technical detail, and do not "
                     "minimise it or change the subject.")
    if state.get("crisis"):
        lines.append("The user may be in danger: be calm and direct, encourage "
                     "them to contact local emergency services or a crisis line "
                     "now, and do not moralise or claim to be a professional.")
    lines.append("Do not claim feelings, consciousness, love or loyalty, and "
                 "never state an emotion as a fact about the user.")
    if intent == "task":
        lines.append("Report only what actually happened: the steps performed "
                     "and their results. Never claim work you did not do.")
    if not lines:
        return ""
    return "Communication policy: " + " ".join(lines)


# ----------------------------------------------------------------- answer side

def _strip_fillers(text):
    changed = True
    while changed:
        changed = False
        for pattern in _FILLER_OPENERS:
            new = pattern.sub("", text, count=1)
            if new != text:
                text, changed = new, True
    return text


def _outside_code(text, transform):
    """Apply ``transform`` to prose lines only, leaving code fences untouched."""
    out, inside = [], False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            inside = not inside
            out.append(line)
            continue
        out.append(line if inside else transform(line))
    return "\n".join(out)


def _capitalise(text):
    for index, char in enumerate(text):
        if char.isalpha():
            return text[:index] + char.upper() + text[index + 1:]
    return text


def _cut_at_sentence(text, limit):
    """Longest prefix ending on a sentence boundary at or before ``limit``."""
    window = text[:limit]
    for match in re.finditer(r"[.!?](?=\s|$)", window):
        cut = match.end()
    try:
        return window[:cut].strip()
    except UnboundLocalError:
        return ""


def shape_answer(answer, state=None, intent=None):
    """Shape a finished answer to the reader, without changing its facts."""
    text = str(answer or "")
    if not text.strip():
        return text
    state = state or {}
    original = text

    text = _AI_DISCLAIMERS.sub("\n", text)
    text = _strip_fillers(text).lstrip()
    text = _outside_code(text, lambda line: re.sub(r"[ \t]+$", "", line))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    quick = (state.get("urgency", 0) >= 1
             or state.get("frustration", 0) >= 1
             or state.get("verbosity") == "concise")
    if quick:
        text = _OFFER_TAIL.sub("", text).strip()

    if state.get("verbosity") == "concise" and "```" not in text and len(text) > 700:
        cut = _cut_at_sentence(text, 700)
        if cut and len(cut) < len(text):
            text = cut + " (Ask for the full detail if you need it.)"

    if state.get("frustration", 0) >= 2:
        text = _outside_code(text, lambda line: line.replace("!", "."))

    text = text.strip()
    if not text:
        return original
    return _capitalise(text)


def crisis_reply():
    """A fixed, careful answer for crisis language. No model, no improvisation."""
    return (
        "I'm really sorry you're in this much pain. I'm not able to be the "
        "help you need for this, and I don't want to pretend otherwise.\n\n"
        "Please contact your local emergency number now if you are in immediate "
        "danger, or a crisis line in your country \u2014 they answer quickly and "
        "talking to them is free. If you can, tell one person nearby who can stay "
        "with you, and put some distance between yourself and anything you could "
        "use to hurt yourself.\n\n"
        "I can stay with the practical parts: finding the right number for where "
        "you are, or drafting what to say when you call."
    )


def opening_line(state, intent=None):
    """A short human acknowledgement for openings that need one, else ''.

    Used where the product speaks first (a development plan, a blocked task)
    rather than as decoration on every reply.
    """
    state = state or {}
    if state.get("frustration", 0) >= 1:
        return "Understood - here is what happens next."
    if state.get("urgency", 0) >= 1:
        return "Here it is, short version first."
    return ""

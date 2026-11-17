"""Specialist agent layer.

The main Harry brain stays the single coordinator: it receives every request,
keeps the conversation coherent and owns the task lifecycle. Specialists are
*roles* it adopts for one task - not separate processes competing for control.

Each agent contributes:
  - a system prompt (how to behave)
  - a model class from harry_core.llm (coding tasks use the coder model, etc.)
  - preferred tool names (used as a planning hint; never a hard restriction,
    so a specialist can still use any tool it genuinely needs)

Selection is deterministic (keyword scoring) so it works fully offline and does
not cost a model round-trip. A request that matches no specialist uses the
general assistant.
"""

import logging
import re
from dataclasses import dataclass, field

from . import llm

log = logging.getLogger("harry.agents")


@dataclass(frozen=True)
class Agent:
    key: str
    name: str
    description: str
    system_prompt: str
    task_class: str = llm.CHAT
    preferred_tools: tuple = field(default_factory=tuple)

    def to_dict(self):
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "task_class": self.task_class,
            "preferred_tools": list(self.preferred_tools),
        }


GENERAL = Agent(
    key="general",
    name="Personal Assistant",
    description="Everyday conversation, questions, planning and coordination.",
    system_prompt=(
        "You are Harry, the user's personal AI assistant and the coordinator of "
        "specialist agents. Answer directly and naturally in 1-4 short sentences. "
        "Reply in the language of the user's latest message; only mix Telugu/Hindi "
        "with English when that message is itself mixed. Never invent facts."
    ),
    preferred_tools=("memory_search", "system_info", "task_list"),
)

CODING = Agent(
    key="coding",
    name="Coding Agent",
    description="Reads repositories, writes and edits code, runs tests and builds.",
    system_prompt=(
        "You are the coding specialist of a personal AI assistant. Work like a "
        "careful senior engineer: inspect before changing, make the smallest "
        "correct edit, never rewrite a whole repository, then run a check and "
        "report exactly what changed and what was verified. Never claim a test "
        "passed unless you ran it and saw the output."
    ),
    task_class=llm.CODING,
    preferred_tools=("file_tree", "search_files", "read_file", "write_file",
                     "edit_file", "terminal", "run_python"),
)

MATH = Agent(
    key="math",
    name="Mathematics Agent",
    description="Calculations, algebra, calculus, matrices, statistics.",
    system_prompt=(
        "You are the mathematics specialist. Use the math tool for every "
        "calculation instead of computing mentally, show the important steps, "
        "and state the final answer clearly. If a value cannot be computed "
        "exactly, say so and give the numeric approximation."
    ),
    preferred_tools=("math", "run_python"),
)

ENGINEERING = Agent(
    key="engineering",
    name="Engineering Agent",
    description="Physics and electrical/electronics calculations with formulas.",
    system_prompt=(
        "You are the engineering specialist for electrical, electronics and "
        "physics problems. Use the math tool's engineering formulas for numeric "
        "answers, keep units explicit, and explain the reasoning briefly."
    ),
    preferred_tools=("math", "run_python"),
)

RESEARCH = Agent(
    key="research",
    name="Research Agent",
    description="Web search, multi-source reading, sourced research reports.",
    system_prompt=(
        "You are the research specialist. Gather several independent sources, "
        "distinguish verified facts (stated by a cited source) from your own "
        "inferences, cite every source by number and URL, and never fabricate a "
        "source or a quotation. If the internet is unavailable, say so plainly."
    ),
    preferred_tools=("deep_research", "web_search", "fetch_page", "download_file"),
)

FILES = Agent(
    key="files",
    name="File Management Agent",
    description="Searching, reading, organising and summarising local documents.",
    system_prompt=(
        "You are the file and document specialist. Find the exact files first, "
        "read before you write, keep originals unless the user asks otherwise, "
        "and confirm before deleting or moving anything."
    ),
    preferred_tools=("search_files", "file_tree", "read_file", "read_document",
                     "summarize_document", "write_file", "edit_file"),
)

COMPUTER = Agent(
    key="computer",
    name="Computer Automation Agent",
    description="Applications, windows, mouse/keyboard, screen understanding.",
    system_prompt=(
        "You are the desktop automation specialist. Prefer deterministic actions "
        "(open app, run command, clipboard) over blind mouse clicking, look at "
        "the screen before acting when it matters, and never perform destructive "
        "system actions without explicit approval."
    ),
    preferred_tools=("open_app", "close_app", "window_list", "clipboard",
                     "terminal", "computer_control", "understand_screen"),
)

MEMORY = Agent(
    key="memory",
    name="Memory Agent",
    description="Storing, retrieving, correcting and forgetting personal memory.",
    system_prompt=(
        "You are the memory specialist. Store only durable, useful facts and "
        "preferences, avoid duplicates, update outdated entries instead of adding "
        "new ones, and delete anything the user asks you to forget."
    ),
    preferred_tools=("memory_store", "memory_search", "memory_forget"),
)

DIAGNOSTICS = Agent(
    key="diagnostics",
    name="System Diagnostics Agent",
    description="Health checks of models, voice, memory, tools and network.",
    system_prompt=(
        "You are the system diagnostics specialist. Report what actually works, "
        "what is missing and the exact next step to fix it. Never report a "
        "component as healthy without evidence."
    ),
    preferred_tools=("system_info", "process_list", "terminal"),
)

AGENTS = {a.key: a for a in (
    GENERAL, CODING, MATH, ENGINEERING, RESEARCH, FILES, COMPUTER, MEMORY, DIAGNOSTICS,
)}

# Keyword scoring per specialist. Matching is intentionally conservative: a
# false positive only changes the prompt style, never what a tool may do.
_PATTERNS = {
    "coding": re.compile(
        r"\b(code|coding|program|script|python|javascript|typescript|java\b|html|css|"
        r"bug|debug|refactor|compile|build|test|repo|repository|git|github|function|"
        r"class|api|endpoint|traceback|exception|import|dependency|deploy)\b", re.I),
    "math": re.compile(
        r"\b(solve|equation|derivative|integral|differentiate|integrate|matrix|"
        r"determinant|eigen|algebra|calculus|statistics|probability|mean|median|"
        r"standard deviation|variance|logarithm|factorial|simplify|expand|"
        r"math|mathematics|calculate|compute|evaluate|graph|plot|convert)\b", re.I),
    "engineering": re.compile(
        r"\b(voltage|current|resistance|resistor|capacitor|inductor|impedance|ohm|"
        r"circuit|electronics|electrical|transistor|diode|amplifier|op-?amp|"
        r"power|frequency|wavelength|torque|force|energy|momentum|physics|"
        r"stress|strain|efficiency|gear|motor|transformer)\b", re.I),
    "research": re.compile(
        r"\b(research|search the web|search online|google|look up|latest news|news|"
        r"sources?|cite|citation|compare.*(sources|websites)|find out about|"
        r"what(?:'s| is) happening|browse)\b", re.I),
    "files": re.compile(
        r"\b(files?|folder|directory|document|documents|pdf|docx|spreadsheet|excel|"
        r"csv|rename|move|copy|organi[sz]e|summari[sz]e|rename|backup)\b", re.I),
    "computer": re.compile(
        r"\b(open (?:the )?(?:app|application|program|window)|close (?:the )?(?:app|"
        r"application|program|window)|screenshot|screen|click|type into|keyboard|"
        r"mouse|clipboard|window|desktop|taskbar|automate|macro)\b", re.I),
    "memory": re.compile(
        r"\b(remember|memor(?:y|ies)|forget|what do you know about me|my preference|"
        r"my favourite|my favorite|note that)\b", re.I),
    "diagnostics": re.compile(
        r"\b(system check|diagnostic|diagnostics|health check|is everything working|"
        r"what(?:'s| is) broken|status of (?:the )?(?:system|models|tools|voice)|"
        r"why (?:are|is) you (?:not|n't) working)\b", re.I),
}

# Extra weight for a few unambiguous phrases, so "open my portfolio code" is a
# coding task rather than a desktop automation task.
_STRONG = {
    "diagnostics": re.compile(r"\bsystem check\b|\bdiagnostics?\b", re.I),
    "memory": re.compile(r"^\s*(?:remember|forget)\b", re.I),
    "research": re.compile(r"\b(search the web|research|latest news)\b", re.I),
    "coding": re.compile(r"\b(traceback|refactor|debug|unit tests?)\b", re.I),
}

_SCOPE_NOTE = (
    " You are one specialist inside a single coordinated assistant; the main "
    "brain owns the task lifecycle, so do only your part and report evidence."
)


def select_agent(text):
    """Pick the best specialist for a request (deterministic, offline)."""
    text = str(text or "")
    if not text.strip():
        return GENERAL
    scores = {}
    for key, pattern in _PATTERNS.items():
        hits = len(pattern.findall(text))
        if hits:
            scores[key] = hits
        strong = _STRONG.get(key)
        if strong and strong.search(text):
            scores[key] = scores.get(key, 0) + 3
    if not scores:
        return GENERAL
    best_key = max(scores, key=lambda k: (scores[k], -_PRIORITY.index(k)))
    agent = AGENTS[best_key]
    log.debug("agent selected: %s for %r", agent.key, text[:60])
    return agent


# Tie-break order: the more specific role wins over the broader one.
_PRIORITY = ["diagnostics", "memory", "coding", "math", "engineering", "research",
             "files", "computer", "general"]


def prompt_for(agent):
    """Full system prompt for a specialist, including the coordination note."""
    return agent.system_prompt + _SCOPE_NOTE


def catalog_hint(agent):
    """Planning hint: which tools this specialist usually needs."""
    if agent is GENERAL:
        return "any available tool"
    return ", ".join(agent.preferred_tools)


def list_agents():
    return [a.to_dict() for a in AGENTS.values()]


def describe():
    """One-line-per-agent summary for diagnostics/debugging."""
    return "\n".join(f"- {a.name} ({a.key}): {a.description}"
                     for a in AGENTS.values())

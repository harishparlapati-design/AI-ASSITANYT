"""Harry core package - a modular personal AI assistant agent architecture.

Subsystems are lazily importable so that optional heavy/missing dependencies
(speech, vision, browser automation, etc.) never break the core agent.
"""

__version__ = "2.1.0"

__all__ = [
    "config",
    "logging_setup",
    "events",
    "llm",
    "memory",
    "security",
    "tools",
    "builtin_tools",
    "task_engine",
    "agents",
    "math_tools",
    "research",
    "documents",
    "speech",
    "scheduler",
    "diagnostics",
    "brain",
    "ui",
]

from . import config
from . import logging_setup
from . import events

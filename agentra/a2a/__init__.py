"""A2A (Agent2Agent) protocol foundation: spec-aligned models and read-only discovery."""

from agentra.a2a.agent_card import (
    A2A_PROTOCOL_VERSION,
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
)
from agentra.a2a.artifact import Artifact
from agentra.a2a.message import Message
from agentra.a2a.part import DataPart, FilePart, Part, TextPart
from agentra.a2a.task import Task, TaskState, TaskStatus

__all__ = [
    "A2A_PROTOCOL_VERSION",
    "AgentCapabilities",
    "AgentCard",
    "AgentProvider",
    "AgentSkill",
    "Artifact",
    "Message",
    "DataPart",
    "FilePart",
    "Part",
    "TextPart",
    "Task",
    "TaskState",
    "TaskStatus",
]

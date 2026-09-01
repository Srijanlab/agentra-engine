"""A2A (Agent2Agent) protocol foundation: spec-aligned models, discovery, and an in-process bus."""

from agentra.a2a.agent_card import (
    A2A_PROTOCOL_VERSION,
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
)
from agentra.a2a.artifact import Artifact
from agentra.a2a.dispatch import A2ADispatcher, TaskHandler, UnknownAgentError
from agentra.a2a.message import Message
from agentra.a2a.params import MessageSendConfiguration, MessageSendParams, send_params, text_message
from agentra.a2a.part import DataPart, FilePart, Part, TextPart
from agentra.a2a.task import Task, TaskState, TaskStatus

__all__ = [
    "A2A_PROTOCOL_VERSION",
    "AgentCapabilities",
    "AgentCard",
    "AgentProvider",
    "AgentSkill",
    "Artifact",
    "A2ADispatcher",
    "TaskHandler",
    "UnknownAgentError",
    "Message",
    "MessageSendConfiguration",
    "MessageSendParams",
    "send_params",
    "text_message",
    "DataPart",
    "FilePart",
    "Part",
    "TextPart",
    "Task",
    "TaskState",
    "TaskStatus",
]

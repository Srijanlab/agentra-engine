"""A2A `message/send` parameter models plus small constructors for text messages."""

from __future__ import annotations

from typing import Optional
from uuid import uuid4

from pydantic import Field

from agentra.a2a._base import A2ABaseModel
from agentra.a2a.message import Message
from agentra.a2a.part import DataPart, Part, TextPart


class MessageSendConfiguration(A2ABaseModel):
    """Optional per-request delivery preferences for `message/send`."""

    accepted_output_modes: list[str] = Field(default_factory=lambda: ["text/plain"])
    blocking: bool = True
    history_length: Optional[int] = None


class MessageSendParams(A2ABaseModel):
    """The payload a client submits to an agent to create or continue a Task."""

    message: Message
    configuration: Optional[MessageSendConfiguration] = None
    metadata: Optional[dict] = None


def text_message(text: str, *, role: str = "user", data: Optional[dict] = None,
                 context_id: Optional[str] = None, task_id: Optional[str] = None) -> Message:
    """Build a Message from a text string and an optional structured-data Part."""
    parts: list[Part] = [TextPart(text=text)]
    if data is not None:
        parts.append(DataPart(data=data))
    return Message(
        role=role,  # type: ignore[arg-type]
        parts=parts,
        message_id=uuid4().hex,
        context_id=context_id,
        task_id=task_id,
    )


def send_params(text: str, *, data: Optional[dict] = None, context_id: Optional[str] = None) -> MessageSendParams:
    """Convenience: wrap a text (+data) message in MessageSendParams."""
    return MessageSendParams(message=text_message(text, data=data, context_id=context_id))

"""A2A Message model exchanged between a client and a remote agent."""

from __future__ import annotations

from typing import Literal, Optional
from uuid import uuid4

from pydantic import Field

from agentra.a2a._base import A2ABaseModel
from agentra.a2a.part import Part


class Message(A2ABaseModel):
    """A single turn of communication carrying one or more content Parts."""

    role: Literal["user", "agent"]
    parts: list[Part]
    message_id: str = Field(default_factory=lambda: uuid4().hex)
    context_id: Optional[str] = None
    task_id: Optional[str] = None
    reference_task_ids: Optional[list[str]] = None
    extensions: Optional[list[str]] = None
    metadata: Optional[dict] = None
    kind: Literal["message"] = "message"

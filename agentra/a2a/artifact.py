"""A2A Artifact model: a tangible output produced by an agent while working a Task."""

from __future__ import annotations

from typing import Optional
from uuid import uuid4

from pydantic import Field

from agentra.a2a._base import A2ABaseModel
from agentra.a2a.part import Part


class Artifact(A2ABaseModel):
    """A named collection of Parts representing one agent-produced result."""

    artifact_id: str = Field(default_factory=lambda: uuid4().hex)
    name: Optional[str] = None
    description: Optional[str] = None
    parts: list[Part]
    extensions: Optional[list[str]] = None
    metadata: Optional[dict] = None

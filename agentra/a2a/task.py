"""A2A Task, TaskStatus, and the TaskState lifecycle enum."""

from __future__ import annotations

import enum
from typing import Literal, Optional

from agentra.a2a._base import A2ABaseModel
from agentra.a2a.artifact import Artifact
from agentra.a2a.message import Message


class TaskState(str, enum.Enum):
    """The lifecycle states a Task can occupy, per the A2A spec (v0.2.x)."""

    submitted = "submitted"
    working = "working"
    input_required = "input-required"
    completed = "completed"
    canceled = "canceled"
    failed = "failed"
    rejected = "rejected"
    auth_required = "auth-required"
    unknown = "unknown"


class TaskStatus(A2ABaseModel):
    """The current state of a Task plus an optional status message and timestamp."""

    state: TaskState
    message: Optional[Message] = None
    timestamp: Optional[str] = None


class Task(A2ABaseModel):
    """A stateful unit of work processed by an agent on behalf of a client."""

    id: str
    context_id: str
    status: TaskStatus
    history: Optional[list[Message]] = None
    artifacts: Optional[list[Artifact]] = None
    metadata: Optional[dict] = None
    kind: Literal["task"] = "task"

"""A lightweight in-process dispatcher that routes A2A tasks to registered agent handlers."""

from __future__ import annotations

import datetime as _dt
from collections.abc import Awaitable, Callable
from uuid import uuid4

from agentra.a2a.message import Message
from agentra.a2a.params import MessageSendParams
from agentra.a2a.task import Task, TaskState, TaskStatus

TaskHandler = Callable[[Task], Awaitable[Task]]

_TERMINAL = {TaskState.completed, TaskState.failed, TaskState.canceled, TaskState.rejected}


class UnknownAgentError(KeyError):
    """Raised when a task is dispatched to an agent id that has no registered handler."""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _status(state: TaskState, message: Message | None = None) -> TaskStatus:
    return TaskStatus(state=state, message=message, timestamp=_now())


class A2ADispatcher:
    """Additive collaboration layer: an in-process registry mapping agent ids to task handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, agent_id: str, handler: TaskHandler) -> None:
        self._handlers[agent_id] = handler

    def unregister(self, agent_id: str) -> None:
        self._handlers.pop(agent_id, None)

    def has(self, agent_id: str) -> bool:
        return agent_id in self._handlers

    def registered_agent_ids(self) -> list[str]:
        return sorted(self._handlers)

    async def dispatch(self, agent_id: str, params: MessageSendParams, *, context_id: str | None = None) -> Task:
        """Route one A2A task to `agent_id`'s handler and return the resulting terminal Task."""
        handler = self._handlers.get(agent_id)
        if handler is None:
            raise UnknownAgentError(agent_id)

        ctx = context_id or params.message.context_id or uuid4().hex
        inbound = params.message.model_copy(update={"context_id": ctx})
        task = Task(
            id=uuid4().hex,
            context_id=ctx,
            status=_status(TaskState.working, inbound),
            history=[inbound],
            metadata={"agent_id": agent_id},
        )
        try:
            result = await handler(task)
        except Exception as exc:  # noqa: BLE001 - a handler failure becomes a failed Task, never a crash
            note = Message(role="agent", parts=[], metadata={"error": repr(exc)})
            return task.model_copy(update={"status": _status(TaskState.failed, note)})

        if result.status.state not in _TERMINAL:
            result = result.model_copy(update={"status": _status(TaskState.completed)})
        return result

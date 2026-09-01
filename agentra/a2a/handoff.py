"""Wrap the existing implementation -> testing handoff as an end-to-end A2A task exchange."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from agentra.a2a.artifact import Artifact
from agentra.a2a.dispatch import A2ADispatcher, TaskHandler
from agentra.a2a.message import Message
from agentra.a2a.params import MessageSendParams
from agentra.a2a.part import DataPart, TextPart
from agentra.a2a.task import Task, TaskState, TaskStatus

RunLocal = Callable[..., Awaitable[Any]]

TESTING_AGENT_ID = "testing"


def implementation_artifact(*, feature: str, branch: str, summary: str,
                            files_changed: Optional[list[str]] = None) -> Artifact:
    """Represent the Implementation Agent's committed change as an A2A Artifact."""
    return Artifact(
        artifact_id=uuid4().hex,
        name="implementation-result",
        description=f"Implementation of {feature!r} on branch {branch}",
        parts=[
            TextPart(text=summary),
            DataPart(data={"feature": feature, "branch": branch, "files_changed": files_changed or []}),
        ],
    )


def request_testing_message(impl: Artifact, *, context_id: Optional[str] = None) -> Message:
    """Build the A2A Message the Implementation Agent sends to ask Testing to verify its change."""
    data = next((p.data for p in impl.parts if isinstance(p, DataPart)), {})
    return Message(
        role="agent",
        parts=[
            TextPart(text=f"Please run the local test/QA pass for {data.get('feature', 'this change')!r}."),
            DataPart(data={"handoff": "implementation->testing", **data}),
        ],
        message_id=uuid4().hex,
        context_id=context_id,
    )


def verdict_artifact(result: Any) -> Artifact:
    """Turn a testing AgentResult (duck-typed: .ok / .text / .json_data) into an A2A Artifact."""
    return Artifact(
        artifact_id=uuid4().hex,
        name="local-test-verdict",
        description="Testing Agent local QA verdict",
        parts=[
            TextPart(text=getattr(result, "text", "") or ""),
            DataPart(data={"ok": bool(getattr(result, "ok", False)),
                           "verdict": getattr(result, "json_data", None) or {}}),
        ],
    )


def make_testing_handler(*, repo: Path, codebase_summary: str, mem: Any = None,
                         run_local: Optional[RunLocal] = None) -> TaskHandler:
    """Adapt `agents.testing.run_local` into an A2A task handler (dependency-injected for tests)."""

    async def handler(task: Task) -> Task:
        runner = run_local
        if runner is None:
            from agentra.agents import testing

            runner = testing.run_local
        result = await runner(repo, codebase_summary, mem)
        state = TaskState.completed if getattr(result, "ok", False) else TaskState.failed
        return task.model_copy(update={
            "status": TaskStatus(state=state),
            "artifacts": [verdict_artifact(result)],
        })

    return handler


async def run_implementation_to_testing_handoff(
    dispatcher: A2ADispatcher, *, feature: str, branch: str, summary: str,
    files_changed: Optional[list[str]] = None, context_id: Optional[str] = None,
) -> Task:
    """Submit an implementation result to the registered `testing` handler as an A2A task."""
    impl = implementation_artifact(feature=feature, branch=branch, summary=summary, files_changed=files_changed)
    message = request_testing_message(impl, context_id=context_id)
    return await dispatcher.dispatch(TESTING_AGENT_ID, MessageSendParams(message=message), context_id=context_id)

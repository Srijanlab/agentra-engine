"""A2A in-process dispatcher: contract round-trip, routing, handoff, and legacy-path safety."""

import asyncio
import inspect
from dataclasses import fields
from pathlib import Path

import pytest

from agentra.a2a import (
    A2ADispatcher,
    MessageSendParams,
    UnknownAgentError,
    send_params,
    text_message,
)
from agentra.a2a.handoff import (
    TESTING_AGENT_ID,
    make_testing_handler,
    run_implementation_to_testing_handoff,
)
from agentra.a2a.params import MessageSendConfiguration
from agentra.a2a.task import Task, TaskState, TaskStatus


# -- contract serialization / round-trip --------------------------------------


def test_message_send_params_roundtrip():
    params = MessageSendParams(
        message=text_message("verify my change", data={"branch": "dev/x"}),
        configuration=MessageSendConfiguration(blocking=True),
    )
    restored = MessageSendParams.model_validate_json(params.model_dump_json(by_alias=True))
    assert restored.model_dump(by_alias=True) == params.model_dump(by_alias=True)
    assert restored.message.parts[0].text == "verify my change"


def test_task_roundtrip_via_dispatcher_output():
    async def echo(task: Task) -> Task:
        return task.model_copy(update={"status": TaskStatus(state=TaskState.completed)})

    d = A2ADispatcher()
    d.register("codebase", echo)
    task = asyncio.run(d.dispatch("codebase", send_params("scan the repo")))
    restored = Task.model_validate_json(task.model_dump_json(by_alias=True))
    assert restored.status.state == "completed"
    assert restored.context_id == task.context_id


# -- dispatcher routing ------------------------------------------------------


def test_dispatch_routes_to_the_registered_handler():
    seen = {}

    async def handler(task: Task) -> Task:
        seen["agent"] = task.metadata["agent_id"]
        seen["text"] = task.history[0].parts[0].text
        return task.model_copy(update={"status": TaskStatus(state=TaskState.completed)})

    d = A2ADispatcher()
    d.register("discovery", handler)
    d.register("implementation", handler)
    asyncio.run(d.dispatch("discovery", send_params("rank opportunities")))

    assert seen == {"agent": "discovery", "text": "rank opportunities"}
    assert d.registered_agent_ids() == ["discovery", "implementation"]
    assert d.has("discovery") and not d.has("nope")


def test_dispatch_unknown_agent_raises():
    d = A2ADispatcher()
    with pytest.raises(UnknownAgentError):
        asyncio.run(d.dispatch("not-a-real-agent", send_params("hi")))


def test_dispatch_handler_exception_becomes_failed_task():
    async def boom(_task: Task) -> Task:
        raise RuntimeError("kaboom")

    d = A2ADispatcher()
    d.register("testing", boom)
    task = asyncio.run(d.dispatch("testing", send_params("run tests")))
    assert task.status.state == TaskState.failed
    assert "kaboom" in task.status.message.metadata["error"]


def test_dispatch_defaults_non_terminal_handler_result_to_completed():
    async def forgetful(task: Task) -> Task:
        return task  # still in `working`

    d = A2ADispatcher()
    d.register("feedback", forgetful)
    task = asyncio.run(d.dispatch("feedback", send_params("assess feedback")))
    assert task.status.state == TaskState.completed


# -- implementation -> testing handoff (end-to-end contract proof) ------------


def test_implementation_to_testing_handoff_end_to_end(tmp_path: Path):
    class FakeResult:
        ok = True
        text = "All green: 12 passed"
        json_data = {"lint_status": "pass", "typecheck_status": "pass"}

    calls = []

    async def fake_run_local(repo, codebase_summary, mem=None):
        calls.append((repo, codebase_summary, mem))
        return FakeResult()

    d = A2ADispatcher()
    d.register(
        TESTING_AGENT_ID,
        make_testing_handler(repo=tmp_path, codebase_summary="fastapi app", run_local=fake_run_local),
    )

    task = asyncio.run(
        run_implementation_to_testing_handoff(
            d, feature="Add search", branch="dev/search", summary="added /search endpoint",
            files_changed=["server.py"],
        )
    )

    assert calls == [(tmp_path, "fastapi app", None)]
    assert task.status.state == TaskState.completed
    assert task.artifacts and task.artifacts[0].name == "local-test-verdict"
    verdict = next(p for p in task.artifacts[0].parts if getattr(p, "kind", None) == "data")
    assert verdict.data == {"ok": True, "verdict": {"lint_status": "pass", "typecheck_status": "pass"}}
    # survives a full JSON round-trip
    assert Task.model_validate_json(task.model_dump_json(by_alias=True)).artifacts[0].parts[0].text.startswith("All green")


def test_handoff_failing_tests_produce_a_failed_task(tmp_path: Path):
    class FailResult:
        ok = False
        text = "2 failed"
        json_data = None

    async def fake_run_local(repo, codebase_summary, mem=None):
        return FailResult()

    d = A2ADispatcher()
    d.register(TESTING_AGENT_ID, make_testing_handler(repo=tmp_path, codebase_summary="x", run_local=fake_run_local))
    task = asyncio.run(run_implementation_to_testing_handoff(d, feature="f", branch="b", summary="s"))
    assert task.status.state == TaskState.failed
    assert task.artifacts[0].parts[1].data["ok"] is False


# -- legacy path is unaffected (additive layer only) --------------------------


def test_run_agent_signature_and_agent_result_shape_unchanged():
    from agentra.agents import base

    sig = inspect.signature(base.run_agent)
    assert list(sig.parameters) == [
        "prompt", "system_prompt", "cwd", "allowed_tools", "permission_mode",
        "max_turns", "allow_prod", "retry_on_contradictory_result",
        "agent_label", "resume", "mcp_servers",
    ]
    assert [f.name for f in fields(base.AgentResult)] == [
        "ok", "text", "json_data", "cost_usd", "turns", "session_id", "auth_failure",
        "push_failed", "input_tokens", "output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
    ]


def test_brain_and_orchestrator_do_not_depend_on_the_a2a_layer():
    import agentra.agents.brain.tools as brain_tools
    import agentra.orchestrator as orchestrator

    for mod in (brain_tools, orchestrator):
        src = inspect.getsource(mod)
        assert "agentra.a2a" not in src, f"{mod.__name__} must not import the additive A2A layer"


def test_importing_a2a_has_no_side_effects_on_the_agent_catalog():
    from agentra.agents import catalog

    before = dict(catalog.AGENT_METADATA)
    import agentra.a2a  # noqa: F401
    from agentra.a2a import cards

    cards.build_agent_cards()
    assert catalog.AGENT_METADATA == before

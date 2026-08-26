"""Requirements Agent (agents/requirements.py), wired into implement_feature
as an automatic first phase (brain.py), and its downstream effects:

- A finalized spec is generated and passed to Implementation Agent as extra
  context (implementation.py's new `spec` param), and set on
  session.current_spec for verify_pre_prod to use later instead of
  cb_summary (testing.py's run_pre_prod is deliberately black-box -- see its
  own docstring).
- If the tracking issue (resolves_id/sub_feature_of) already has a
  persisted spec (Memory.get_spec), it's reused verbatim -- Requirements
  Agent is not called again -- so a resumed cycle doesn't pay to regenerate
  (and potentially drift from) a spec an earlier, already-shipped part was
  built against.
- A fresh spec is persisted onto the tracking issue (Memory.record_spec) so
  a *future* resumed call can reuse it the same way.
- Requirements Agent failing is best-effort: implementation still proceeds
  (with spec="").

No real LLM call: implementation.run and requirements.run are both
monkeypatched.
"""

import asyncio
from pathlib import Path

from agentra import registry
from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


def _session(tmp_path: Path, **overrides) -> brain.OrchestratorSession:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    defaults = dict(
        repo=repo,
        objective="test objective",
        env=EnvironmentConfig(),
        mem=Memory(repo),
        run_id="testrun1",
        cb_summary="a codebase summary",
    )
    defaults.update(overrides)
    return brain.OrchestratorSession(**defaults)


def _tool(session, name):
    tools = brain._tools_for(session)
    return next(t for t in tools if t.name == name)


def _patch_registry(monkeypatch):
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)


def _fake_impl_result(**overrides) -> AgentResult:
    defaults = dict(ok=True, text="done", json_data={"feature": "A feature", "status": "implemented"}, cost_usd=0.01, turns=2)
    defaults.update(overrides)
    return AgentResult(**defaults)


def test_format_spec_renders_spec_and_acceptance_criteria():
    text = brain._format_spec({"spec": "Add a health endpoint.", "acceptance_criteria": ["GET /health returns 200", "Body is 'ok'"]})

    assert "Add a health endpoint." in text
    assert "GET /health returns 200" in text
    assert "Body is 'ok'" in text


def test_format_spec_handles_no_acceptance_criteria():
    text = brain._format_spec({"spec": "Just a description."})

    assert "Just a description." in text
    assert "Acceptance criteria" not in text


def test_implement_feature_generates_and_persists_a_new_spec(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)

    async def fake_requirements_run(repo, objective, brief, cb_summary):
        return AgentResult(
            ok=True, text="spec text",
            json_data={"spec": "Fix the sort order.", "acceptance_criteria": ["Newest run appears first"]},
            cost_usd=0.02, turns=4,
        )

    impl_calls = []

    async def fake_implementation_run(repo, objective, brief, cb_summary, env, feature_branch, resume=False, spec="", session_id=None):
        impl_calls.append(spec)
        return _fake_impl_result()

    monkeypatch.setattr(brain.requirements, "run", fake_requirements_run)
    monkeypatch.setattr(brain.implementation, "run", fake_implementation_run)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 13, "board_issue_number": 13})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "clear_known_bug", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "record_in_progress_branch", lambda *a, **k: None)
    persisted = []
    monkeypatch.setattr(session.mem, "get_spec", lambda issue_number: None)
    monkeypatch.setattr(session.mem, "record_spec", lambda issue_number, spec: persisted.append((issue_number, spec)))

    asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "fix it", "resolves_origin": "known_bug", "resolves_id": "13", "sub_feature_of": "", "more_parts_expected": False}
        )
    )

    assert len(impl_calls) == 1
    assert "Fix the sort order." in impl_calls[0]
    assert "Newest run appears first" in impl_calls[0]
    assert persisted == [(13, {"spec": "Fix the sort order.", "acceptance_criteria": ["Newest run appears first"]})]
    assert session.current_spec is not None
    assert "Fix the sort order." in session.current_spec


def test_implement_feature_reuses_an_existing_spec_without_calling_requirements_agent(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)

    monkeypatch.setattr(
        brain.requirements, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call Requirements Agent"))
    )
    impl_calls = []

    async def fake_implementation_run(repo, objective, brief, cb_summary, env, feature_branch, resume=False, spec="", session_id=None):
        impl_calls.append(spec)
        return _fake_impl_result()

    monkeypatch.setattr(brain.implementation, "run", fake_implementation_run)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 13, "board_issue_number": 13})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "clear_known_bug", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "record_in_progress_branch", lambda *a, **k: None)
    monkeypatch.setattr(
        session.mem, "get_spec",
        lambda issue_number: {"spec": "Existing spec from an earlier attempt.", "acceptance_criteria": ["It still works"]},
    )
    monkeypatch.setattr(
        session.mem, "record_spec", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not re-record an existing spec"))
    )

    asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "resume it", "resolves_origin": "known_bug", "resolves_id": "13", "sub_feature_of": "", "more_parts_expected": False}
        )
    )

    assert len(impl_calls) == 1
    assert "Existing spec from an earlier attempt." in impl_calls[0]


def test_implement_feature_proceeds_without_a_spec_if_requirements_agent_fails(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)

    async def failing_requirements_run(*a, **k):
        return AgentResult(ok=False, text="could not produce a spec", json_data=None, cost_usd=0.01, turns=2)

    impl_calls = []

    async def fake_implementation_run(repo, objective, brief, cb_summary, env, feature_branch, resume=False, spec="", session_id=None):
        impl_calls.append(spec)
        return _fake_impl_result()

    monkeypatch.setattr(brain.requirements, "run", failing_requirements_run)
    monkeypatch.setattr(brain.implementation, "run", fake_implementation_run)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 30, "board_issue_number": 30})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)

    result = asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "my own idea", "resolves_origin": "", "resolves_id": "", "sub_feature_of": "", "more_parts_expected": False}
        )
    )

    assert result.get("is_error") is not True
    assert impl_calls == [""]
    assert session.current_spec is None


def test_verify_pre_prod_uses_current_spec_not_codebase_summary(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, pre_prod_url="https://preview.example.com", current_spec="Spec: the finalized spec text")

    captured = {}

    async def fake_run_pre_prod(repo, spec, preview_url, run_id, session_id=None):
        captured["spec"] = spec
        return AgentResult(ok=True, text="ok", json_data={"status": "pass", "reachable": True, "feature_verified": True}, cost_usd=0.01, turns=2)

    monkeypatch.setattr(brain.testing, "run_pre_prod", fake_run_pre_prod)

    asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert captured["spec"] == "Spec: the finalized spec text"


def test_verify_pre_prod_falls_back_to_codebase_summary_without_a_spec(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, pre_prod_url="https://preview.example.com")
    assert session.current_spec is None

    captured = {}

    async def fake_run_pre_prod(repo, spec, preview_url, run_id, session_id=None):
        captured["spec"] = spec
        return AgentResult(ok=True, text="ok", json_data={"status": "pass"}, cost_usd=0.01, turns=2)

    monkeypatch.setattr(brain.testing, "run_pre_prod", fake_run_pre_prod)

    asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert captured["spec"] == session.cb_summary

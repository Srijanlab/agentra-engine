"""Covers GitHub issue #6: run_local_tests/verify_pre_prod used to only surface
something the Testing Agent noticed incidentally (unrelated to the specific thing
being verified) through the free-text `notes` field, which nothing ever read back --
so on an otherwise-passing run, a real observed bug was silently lost forever.

agents/testing.py's LOCAL_SYSTEM_PROMPT/PRE_PROD_SYSTEM_PROMPT both ask the Testing
Agent for a structured `incidental_findings` list, but only verify_pre_prod's tool
handler actually files them (and outright failures) via mem.record_known_bug --
run_local_tests deliberately does neither anymore. Local test failures/findings are
frequently pre-existing, unrelated test-infra debt rather than a new defect (see
brain.py's run_local_tests comment, and the false positive that became GitHub issue
#17) -- a failure there still fully gates deploy_pre_prod via session.tests_passed,
it just no longer auto-files a bug report every time an already-known-broken local
test runs again. verify_pre_prod checks the actual deployed artifact, where a
failure or incidental finding is a much stronger signal of something real and novel.

Drives the real tool handlers returned by brain._tools_for (not just the
_file_incidental_findings helper in isolation), with testing.run_local/run_pre_prod
and Memory.record_known_bug/record_failure monkeypatched -- no real LLM call, no
real GitHub Issues call, matching the pattern in tests/test_brain_blocking_bugs.py.
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


def _fake_result(status: str, incidental_findings=None, **extra) -> AgentResult:
    data = {"status": status, **extra}
    if incidental_findings is not None:
        data["incidental_findings"] = incidental_findings
    return AgentResult(ok=True, text="```json\n{}\n```", json_data=data, cost_usd=0.01, turns=1)


def _patch_common(monkeypatch):
    known_bug_calls = []
    failure_calls = []
    monkeypatch.setattr(
        Memory,
        "record_known_bug",
        lambda self, run_id, severity, diagnosis, proposed_fix, **kw: known_bug_calls.append(
            {"run_id": run_id, "severity": severity, "diagnosis": diagnosis, "proposed_fix": proposed_fix, **kw}
        ),
    )
    monkeypatch.setattr(
        Memory,
        "record_failure",
        lambda self, run_id, step_name, text, **kw: failure_calls.append(
            {"run_id": run_id, "step_name": step_name, "text": text}
        ),
    )
    return known_bug_calls, failure_calls


def _tool(session, name):
    tools = brain._tools_for(session)
    return next(t for t in tools if t.name == name)


# -- (a) verify_pre_prod: status="pass" AND non-empty incidental_findings files each one --


def test_run_local_tests_never_files_incidental_findings_even_on_pass(tmp_path, monkeypatch):
    known_bug_calls, failure_calls = _patch_common(monkeypatch)
    findings = [
        {"diagnosis": "login page favicon is broken", "severity": "low", "proposed_fix": "add favicon.ico"},
        {"diagnosis": "console warning about deprecated API on /settings", "severity": "medium"},
    ]

    async def fake_run_local(repo, cb_summary, mem=None, session_id=None):
        return _fake_result("pass", incidental_findings=findings, lint_status="pass", typecheck_status="pass")

    monkeypatch.setattr(brain.testing, "run_local", fake_run_local)

    session = _session(tmp_path)
    result = asyncio.run(_tool(session, "run_local_tests").handler({}))

    assert result.get("is_error") is not True
    assert session.tests_passed is True
    # run_local_tests never files anything, regardless of what the Testing
    # Agent reported incidentally -- only verify_pre_prod does (see below).
    assert known_bug_calls == []
    assert failure_calls == []


def test_verify_pre_prod_pass_with_incidental_findings_files_known_bugs(tmp_path, monkeypatch):
    known_bug_calls, failure_calls = _patch_common(monkeypatch)
    findings = [{"diagnosis": "footer link 404s on the live site", "severity": "low"}]

    async def fake_run_pre_prod(repo, cb_summary, preview_url, run_id, session_id=None, **_kw):
        return _fake_result("pass", incidental_findings=findings, reachable=True, feature_verified=True)

    monkeypatch.setattr(brain.testing, "run_pre_prod", fake_run_pre_prod)

    session = _session(tmp_path, pre_prod_url="https://preview.example.com")
    result = asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert session.pre_prod_verified is True
    assert len(known_bug_calls) == 1
    assert known_bug_calls[0]["diagnosis"] == "footer link 404s on the live site"
    assert known_bug_calls[0]["source"] == "testing-agent-pre-prod"
    assert failure_calls == []


# -- (b) empty/absent incidental_findings is a no-op ---------------------------------


def test_run_local_tests_pass_without_incidental_findings_is_a_noop(tmp_path, monkeypatch):
    known_bug_calls, failure_calls = _patch_common(monkeypatch)

    async def fake_run_local(repo, cb_summary, mem=None, session_id=None):
        return _fake_result("pass", lint_status="pass", typecheck_status="pass")  # key absent entirely

    monkeypatch.setattr(brain.testing, "run_local", fake_run_local)

    session = _session(tmp_path)
    asyncio.run(_tool(session, "run_local_tests").handler({}))

    assert known_bug_calls == []
    assert failure_calls == []


def test_verify_pre_prod_pass_with_empty_incidental_findings_is_a_noop(tmp_path, monkeypatch):
    known_bug_calls, failure_calls = _patch_common(monkeypatch)

    async def fake_run_pre_prod(repo, cb_summary, preview_url, run_id, session_id=None, **_kw):
        return _fake_result("pass", incidental_findings=[], reachable=True, feature_verified=True)

    monkeypatch.setattr(brain.testing, "run_pre_prod", fake_run_pre_prod)

    session = _session(tmp_path, pre_prod_url="https://preview.example.com")
    asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert known_bug_calls == []
    assert failure_calls == []


# -- (c) existing pass/fail behavior is otherwise unchanged --------------------------


def test_run_local_tests_fail_does_not_file_a_bug_or_call_record_failure(tmp_path, monkeypatch):
    known_bug_calls, failure_calls = _patch_common(monkeypatch)
    findings = [{"diagnosis": "unrelated broken favicon", "severity": "low"}]

    async def fake_run_local(repo, cb_summary, mem=None, session_id=None):
        return _fake_result(
            "fail", incidental_findings=findings, failed_tests=["test_x"], lint_status="pass", typecheck_status="pass"
        )

    monkeypatch.setattr(brain.testing, "run_local", fake_run_local)

    session = _session(tmp_path)
    result = asyncio.run(_tool(session, "run_local_tests").handler({}))

    # The failure still gates deploy_pre_prod...
    assert result.get("is_error") is True
    assert session.tests_passed is False
    # ...but neither the outright-failure path nor incidental findings file a
    # GitHub bug anymore -- a local test failure/finding might just be
    # pre-existing debt, not something this run caused.
    assert failure_calls == []
    assert known_bug_calls == []


def test_run_local_tests_fail_without_incidental_findings_unchanged(tmp_path, monkeypatch):
    known_bug_calls, failure_calls = _patch_common(monkeypatch)

    async def fake_run_local(repo, cb_summary, mem=None, session_id=None):
        return _fake_result("fail", failed_tests=["test_x"], lint_status="pass", typecheck_status="fail")

    monkeypatch.setattr(brain.testing, "run_local", fake_run_local)

    session = _session(tmp_path)
    result = asyncio.run(_tool(session, "run_local_tests").handler({}))

    assert result.get("is_error") is True
    assert session.tests_passed is False
    assert failure_calls == []
    assert known_bug_calls == []


def test_verify_pre_prod_fail_unchanged_without_incidental_findings(tmp_path, monkeypatch):
    known_bug_calls, failure_calls = _patch_common(monkeypatch)

    async def fake_run_pre_prod(repo, cb_summary, preview_url, run_id, session_id=None, **_kw):
        return _fake_result("fail", reachable=True, feature_verified=False)

    monkeypatch.setattr(brain.testing, "run_pre_prod", fake_run_pre_prod)

    session = _session(tmp_path, pre_prod_url="https://preview.example.com")
    result = asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert result.get("is_error") is True
    assert session.pre_prod_verified is False
    assert len(failure_calls) == 1
    assert failure_calls[0]["step_name"] == "pre-prod-verification"
    assert known_bug_calls == []


# -- direct coverage of the helper itself ---------------------------------------------


def test_file_incidental_findings_skips_malformed_entries(tmp_path, monkeypatch):
    _, _ = _patch_common(monkeypatch)
    known_bug_calls = []
    monkeypatch.setattr(
        Memory,
        "record_known_bug",
        lambda self, run_id, severity, diagnosis, proposed_fix, **kw: known_bug_calls.append(diagnosis),
    )
    mem = Memory(tmp_path)
    data = {
        "incidental_findings": [
            {"diagnosis": "real finding", "severity": "high"},
            {"severity": "low"},  # no diagnosis/description -- skipped
            "not even a dict",  # skipped
            {},
        ]
    }
    filed = brain._file_incidental_findings(mem, "run1", data, source="testing-agent-local")
    assert filed == 1
    assert known_bug_calls == ["real finding"]

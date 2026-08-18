"""Part 2/3 of GitHub issue #2: surface an itemized test report (each test
case/acceptance criterion + pass/fail) to a human reviewing a run before
they click Promote.

agents/testing.py's run_pre_prod already independently verifies a live
pre-prod deployment; this extends its PRE_PROD_SYSTEM_PROMPT/JSON output
with a per-criterion `test_cases` breakdown and persists it to a new
report.json test artifact (same test_artifacts/{run_id}/ directory, same
durability tier, as the existing screenshot.png -- see test_screenshot.py).
server.py's new GET /runs/{run_key}/test-report route serves that back to
the dashboard, following the exact same 404-if-absent pattern as the
existing /runs/{run_key}/screenshot route.

No real LLM call here -- run_agent is monkeypatched, matching the pattern
in test_screenshot.py's run_pre_prod wiring tests.
"""

import asyncio
import json

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.agents import screenshot, testing
from agentra.agents.base import AgentResult
from agentra.memory import Memory


def _mock_screenshot(monkeypatch, ok: bool = True):
    async def fake_capture(url, out_path, timeout_ms=20000):
        if ok:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"fake-png")
        return ok, str(out_path) if ok else "boom"

    monkeypatch.setattr(screenshot, "capture", fake_capture)


def _mock_run_agent(monkeypatch, json_data, ok=True):
    async def fake_run_agent(**kwargs):
        return AgentResult(ok=ok, text="```json\n" + json.dumps(json_data) + "\n```", json_data=json_data, cost_usd=0.02, turns=3)

    monkeypatch.setattr(testing, "run_agent", fake_run_agent)


# ── agents/testing.py: report persisted alongside the screenshot ──────────


def test_run_pre_prod_persists_itemized_test_cases(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _mock_screenshot(monkeypatch, ok=True)
    data = {
        "status": "pass",
        "reachable": True,
        "feature_verified": True,
        "test_cases": [
            {"criterion": "App responds with a healthy status code", "result": "pass", "evidence": "curl -> 200"},
            {"criterion": "Clicking Submit shows a confirmation banner", "result": "pass", "evidence": "drove the UI, banner appeared"},
            {"criterion": "Invalid input shows an error message", "result": "fail", "evidence": "no error shown for bad input"},
        ],
        "notes": "One criterion failed.",
    }
    _mock_run_agent(monkeypatch, data)

    result = asyncio.run(testing.run_pre_prod(repo, "spec text", "https://preview.example.com", "run42"))

    assert result.ok is True
    path = testing.report_path(repo, "run42")
    assert path.exists()
    persisted = json.loads(path.read_text())
    assert persisted["status"] == "pass"
    assert persisted["screenshot_captured"] is True
    assert len(persisted["test_cases"]) == 3
    assert persisted["test_cases"][2] == {
        "criterion": "Invalid input shows an error message",
        "result": "fail",
        "evidence": "no error shown for bad input",
    }


def test_run_pre_prod_persists_incidental_findings_alongside_test_cases(tmp_path, monkeypatch):
    # brain.py's _file_incidental_findings separately files each of these as
    # a backlog bug regardless of this report -- but a human reviewing THIS
    # run before promoting it should see them here too, not just in the
    # itemized pass/fail cases, so a full "test report package" (cases +
    # anything else noticed) is visible in one place.
    repo = tmp_path / "repo"
    repo.mkdir()
    _mock_screenshot(monkeypatch, ok=True)
    data = {
        "status": "pass",
        "reachable": True,
        "feature_verified": True,
        "test_cases": [{"criterion": "App responds", "result": "pass", "evidence": "curl -> 200"}],
        "incidental_findings": [
            {"diagnosis": "Stale favicon on the login page", "severity": "low", "proposed_fix": "Regenerate favicon.ico"},
        ],
        "notes": "all good",
    }
    _mock_run_agent(monkeypatch, data)

    asyncio.run(testing.run_pre_prod(repo, "spec text", "https://preview.example.com", "run43"))

    persisted = json.loads(testing.report_path(repo, "run43").read_text())
    assert persisted["incidental_findings"] == [
        {"diagnosis": "Stale favicon on the login page", "severity": "low", "proposed_fix": "Regenerate favicon.ico"},
    ]


def test_run_pre_prod_persists_empty_incidental_findings_when_none_reported(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _mock_screenshot(monkeypatch, ok=True)
    _mock_run_agent(monkeypatch, {"status": "pass", "test_cases": [{"criterion": "x", "result": "pass", "evidence": "y"}]})

    asyncio.run(testing.run_pre_prod(repo, "spec text", "https://preview.example.com", "run44"))

    persisted = json.loads(testing.report_path(repo, "run44").read_text())
    assert persisted["incidental_findings"] == []


def test_run_pre_prod_records_screenshot_capture_failure_in_report(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _mock_screenshot(monkeypatch, ok=False)
    _mock_run_agent(monkeypatch, {"status": "pass", "test_cases": [{"criterion": "x", "result": "pass", "evidence": "y"}]})

    asyncio.run(testing.run_pre_prod(repo, "spec text", "https://preview.example.com", "run42"))

    persisted = json.loads(testing.report_path(repo, "run42").read_text())
    assert persisted["screenshot_captured"] is False


def test_run_pre_prod_writes_no_report_when_agent_returns_no_json(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _mock_screenshot(monkeypatch, ok=True)

    async def fake_run_agent(**kwargs):
        return AgentResult(ok=False, text="agent turn raised: boom", json_data=None, cost_usd=0.0, turns=0)

    monkeypatch.setattr(testing, "run_agent", fake_run_agent)

    asyncio.run(testing.run_pre_prod(repo, "spec text", "https://preview.example.com", "run42"))

    assert not testing.report_path(repo, "run42").exists()


# ── server.py's /runs/{run_key}/test-report endpoint ───────────────────────


def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "PAUSE_PATH", home / "paused.json")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")
    server._active_runs.clear()
    server._app_locks.clear()


def _register_run(tmp_path, monkeypatch, run_key="run42", source="on-demand"):
    _isolate_registry(tmp_path, monkeypatch)
    repo = tmp_path / "myapp"
    repo.mkdir()
    Memory(repo).set_objective("Ship things.")
    registry.register_app("myapp", str(repo), repo_url="https://github.com/acme/myapp.git", branch="main")
    registry.record_run(
        run_key, app="myapp", source=source, status="completed",
        started_at=0, objective="Ship things.", loop_id="loop1",
    )
    return repo


def test_get_run_test_report_returns_itemized_test_cases(tmp_path, monkeypatch):
    repo = _register_run(tmp_path, monkeypatch)
    report_path = testing.report_path(repo, "run42")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        "status": "pass",
        "reachable": True,
        "feature_verified": True,
        "test_cases": [
            {"criterion": "reachable", "result": "pass", "evidence": "200 OK"},
            {"criterion": "criterion A", "result": "pass", "evidence": "checked A"},
            {"criterion": "criterion B", "result": "fail", "evidence": "checked B, broken"},
        ],
        "notes": "",
        "screenshot_captured": True,
    }))

    response = TestClient(server.app).get("/runs/run42/test-report")

    assert response.status_code == 200
    body = response.json()
    assert len(body["test_cases"]) == 3
    assert all("criterion" in tc and "result" in tc for tc in body["test_cases"])
    assert body["test_cases"][2]["result"] == "fail"


def test_get_run_test_report_returns_incidental_findings_alongside_test_cases(tmp_path, monkeypatch):
    repo = _register_run(tmp_path, monkeypatch)
    report_path = testing.report_path(repo, "run42")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        "status": "pass",
        "test_cases": [{"criterion": "reachable", "result": "pass", "evidence": "200 OK"}],
        "incidental_findings": [
            {"diagnosis": "Stale favicon", "severity": "low", "proposed_fix": "Regenerate favicon.ico"},
        ],
        "notes": "",
        "screenshot_captured": True,
    }))

    response = TestClient(server.app).get("/runs/run42/test-report")

    assert response.status_code == 200
    body = response.json()
    assert body["incidental_findings"] == [
        {"diagnosis": "Stale favicon", "severity": "low", "proposed_fix": "Regenerate favicon.ico"},
    ]


def test_get_run_test_report_404s_when_no_report_captured(tmp_path, monkeypatch):
    _register_run(tmp_path, monkeypatch)

    response = TestClient(server.app).get("/runs/run42/test-report")

    assert response.status_code == 404


def test_get_run_test_report_404s_for_promote_source_run(tmp_path, monkeypatch):
    # A run whose source is 'promote' never ran live pre-prod verification
    # at all -- no report.json was ever written for it.
    _register_run(tmp_path, monkeypatch, run_key="promruns", source="promote")

    response = TestClient(server.app).get("/runs/promruns/test-report")

    assert response.status_code == 404


def test_get_run_test_report_404s_for_unknown_run_key(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)

    response = TestClient(server.app).get("/runs/nonexistent/test-report")

    assert response.status_code == 404

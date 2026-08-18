"""testing.run_pre_prod's test report packet (_write_report) -- a durable
.agentra/test_artifacts/{run_id}/report.md synthesizing the verification
verdict, written alongside the screenshot already captured there. Generic to
every pre-prod verification, not just agentra-fixing-agentra's self-hosted
path -- see agents/testing.py's own docstring on _write_report.

No real LLM call or screenshot capture: run_agent and screenshot.capture are
both monkeypatched.
"""

import asyncio
from pathlib import Path

from agentra.agents import testing
from agentra.agents.base import AgentResult


def _patch_agent_and_screenshot(monkeypatch, *, json_data, screenshot_ok=True):
    async def fake_run_agent(**kwargs):
        return AgentResult(ok=True, text="verified", json_data=json_data, cost_usd=0.01, turns=3)

    async def fake_capture(url, path):
        if screenshot_ok:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake-png-bytes")
        return screenshot_ok, "" if screenshot_ok else "capture failed"

    monkeypatch.setattr(testing, "run_agent", fake_run_agent)
    monkeypatch.setattr("agentra.agents.screenshot.capture", fake_capture)


def test_run_pre_prod_writes_a_report_with_verdict_and_findings(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_agent_and_screenshot(
        monkeypatch,
        json_data={
            "status": "pass",
            "reachable": True,
            "feature_verified": True,
            "notes": "all good",
            "incidental_findings": [{"severity": "low", "diagnosis": "stale favicon", "proposed_fix": "update it"}],
        },
    )

    asyncio.run(testing.run_pre_prod(repo, "Spec: does the thing", "http://agentra-preprod-run1:8080", "run1"))

    report = (testing.screenshot_path(repo, "run1").parent / "report.md").read_text()
    assert "run1" in report
    assert "http://agentra-preprod-run1:8080" in report
    assert "Spec: does the thing" in report
    assert "status: pass" in report
    assert "feature_verified: True" in report
    assert "stale favicon" in report
    assert "screenshot.png" in report


def test_run_pre_prod_writes_a_report_even_when_the_screenshot_capture_fails(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_agent_and_screenshot(monkeypatch, json_data={"status": "fail", "reachable": False}, screenshot_ok=False)

    asyncio.run(testing.run_pre_prod(repo, "Spec: does the thing", "http://agentra-preprod-run1:8080", "run1"))

    report = (testing.screenshot_path(repo, "run1").parent / "report.md").read_text()
    assert "status: fail" in report
    assert "screenshot.png" not in report

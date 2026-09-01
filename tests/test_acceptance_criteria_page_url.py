"""GitHub issue #108: acceptance_criteria may carry a per-criterion page_url so the
pre-prod Testing Agent screenshots the exact page. Legacy list[str] specs still work."""

import asyncio
from pathlib import Path

from agentra.agents import testing
from agentra.agents.base import AgentResult
from agentra.agents.brain.tools import _format_spec
from agentra.agents.spec import criterion_page_url, criterion_text, page_urls


def test_normalizers_handle_both_shapes():
    assert criterion_text("GET /apps returns 200") == "GET /apps returns 200"
    assert criterion_page_url("GET /apps returns 200") == ""
    assert criterion_text({"text": "dashboard shows Backlog tab", "page_url": "/"}) == "dashboard shows Backlog tab"
    assert criterion_page_url({"text": "x", "page_url": "/backlog"}) == "/backlog"


def test_page_urls_is_distinct_and_ordered():
    criteria = [
        "GET /health returns 200",
        {"text": "a", "page_url": "/runs"},
        {"text": "b", "page_url": "/runs"},
        {"text": "c", "page_url": "/"},
    ]
    assert page_urls(criteria) == ["/runs", "/"]
    assert page_urls(None) == []


def test_format_spec_renders_the_route_hint():
    spec = {
        "spec": "add a Backlog tab",
        "acceptance_criteria": [
            "GET /apps returns 200",
            {"text": "the dashboard shows a Backlog tab", "page_url": "/"},
        ],
    }
    text = _format_spec(spec)
    assert "- GET /apps returns 200" in text
    assert "- the dashboard shows a Backlog tab  (route: /)" in text


def test_format_spec_still_handles_legacy_list_of_strings():
    text = _format_spec({"spec": "x", "acceptance_criteria": ["does a thing", "does another"]})
    assert "- does a thing" in text and "- does another" in text


def _patch(monkeypatch, shots: list):
    async def fake_capture(url, out_path, timeout_ms=20000):
        shots.append((url, out_path.name))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"png")
        return True, str(out_path)

    async def fake_run_agent(**kwargs):
        fake_run_agent.prompt = kwargs["prompt"]
        return AgentResult(ok=True, text="ok", json_data={"status": "pass"}, cost_usd=0.0, turns=1)

    monkeypatch.setattr("agentra.agents.screenshot.capture", fake_capture)
    monkeypatch.setattr(testing, "run_agent", fake_run_agent)
    return fake_run_agent


def test_run_pre_prod_screenshots_each_ui_criterion_page(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    shots: list = []
    agent = _patch(monkeypatch, shots)

    criteria = [
        "GET /health returns 200",
        {"text": "Backlog tab renders", "page_url": "/backlog"},
        {"text": "run row expands", "page_url": "/runs/42"},
    ]
    asyncio.run(testing.run_pre_prod(
        repo, "spec text", "https://preview.example.com", "run1", acceptance_criteria=criteria,
    ))

    urls = [u for u, _ in shots]
    assert "https://preview.example.com" in urls
    assert "https://preview.example.com/backlog" in urls
    assert "https://preview.example.com/runs/42" in urls
    assert "screenshot-backlog.png" in [n for _, n in shots]
    assert "one per acceptance-criterion page" in agent.prompt


def test_run_pre_prod_without_page_urls_takes_only_the_root_shot(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    shots: list = []
    _patch(monkeypatch, shots)

    asyncio.run(testing.run_pre_prod(
        repo, "spec text", "https://preview.example.com", "run2",
        acceptance_criteria=["GET /apps returns 200"],
    ))

    assert shots == [("https://preview.example.com", "screenshot.png")]

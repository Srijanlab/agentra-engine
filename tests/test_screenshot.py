"""Tests for agents/screenshot.py (deterministic pre-prod screenshot
capture) and its wiring into agents/testing.py's run_pre_prod() and
server.py's screenshot-serving endpoint. Playwright itself is mocked --
no real browser launch in these tests, just verifying the plumbing:
right URL/path passed in, failures degrade gracefully, the LLM prompt is
told what happened, and the dashboard endpoint serves the resulting file.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.agents import screenshot, testing
from agentra.agents.base import AgentResult
from agentra.memory import Memory


def _mock_playwright(monkeypatch, *, goto_error: Exception | None = None):
    """Patches playwright.async_api.async_playwright so screenshot.capture()
    runs its real control flow against fake browser/page objects instead of
    launching a real Chromium."""
    page = AsyncMock()
    if goto_error:
        page.goto.side_effect = goto_error
    browser = AsyncMock()
    browser.new_page.return_value = page
    chromium = AsyncMock()
    chromium.launch.return_value = browser

    pw_instance = MagicMock()
    pw_instance.chromium = chromium

    pw_cm = AsyncMock()
    pw_cm.__aenter__.return_value = pw_instance
    pw_cm.__aexit__.return_value = False

    fake_module = MagicMock()
    fake_module.async_playwright.return_value = pw_cm

    import sys

    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_module)
    return page


def test_capture_saves_screenshot_on_success(tmp_path, monkeypatch):
    page = _mock_playwright(monkeypatch)
    out_path = tmp_path / "shots" / "screenshot.png"

    ok, detail = asyncio.run(screenshot.capture("https://example.com", out_path))

    assert ok is True
    assert detail == str(out_path)
    page.goto.assert_awaited_once()
    assert page.goto.await_args.args[0] == "https://example.com"
    page.screenshot.assert_awaited_once()
    assert page.screenshot.await_args.kwargs["path"] == str(out_path)
    assert page.screenshot.await_args.kwargs["full_page"] is True


def test_capture_creates_parent_directory(tmp_path, monkeypatch):
    _mock_playwright(monkeypatch)
    out_path = tmp_path / "a" / "b" / "c" / "screenshot.png"

    asyncio.run(screenshot.capture("https://example.com", out_path))

    assert out_path.parent.exists()


def test_capture_returns_false_on_navigation_failure(tmp_path, monkeypatch):
    _mock_playwright(monkeypatch, goto_error=RuntimeError("net::ERR_CONNECTION_REFUSED"))
    out_path = tmp_path / "screenshot.png"

    ok, detail = asyncio.run(screenshot.capture("https://example.com", out_path))

    assert ok is False
    assert "ERR_CONNECTION_REFUSED" in detail


def test_capture_returns_false_when_playwright_not_installed(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "playwright.async_api":
            raise ImportError("no module named playwright")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    out_path = tmp_path / "screenshot.png"

    ok, detail = asyncio.run(screenshot.capture("https://example.com", out_path))

    assert ok is False
    assert "not installed" in detail


# ── run_pre_prod() wiring: screenshot capture happens deterministically,   ──
# ── the LLM is told what happened either way, and a capture failure never ──
# ── blocks the rest of verification.                                      ──


def test_run_pre_prod_captures_screenshot_before_the_agent_turn(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    captured = {}

    async def fake_capture(url, out_path, timeout_ms=20000):
        captured["url"] = url
        captured["out_path"] = out_path
        return True, str(out_path)

    monkeypatch.setattr(screenshot, "capture", fake_capture)

    fake_result = AgentResult(ok=True, text="ok", json_data={"status": "pass"}, cost_usd=0.01, turns=1)
    prompts = {}

    async def fake_run_agent(**kwargs):
        prompts["prompt"] = kwargs["prompt"]
        return fake_result

    monkeypatch.setattr(testing, "run_agent", fake_run_agent)

    result = asyncio.run(testing.run_pre_prod(repo, "summary", "https://preview.example.com", "run42"))

    assert result is fake_result
    assert captured["url"] == "https://preview.example.com"
    assert captured["out_path"] == testing.screenshot_path(repo, "run42")
    assert "already captured" in prompts["prompt"]


def test_run_pre_prod_tells_the_agent_when_screenshot_capture_failed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    async def fake_capture(url, out_path, timeout_ms=20000):
        return False, "boom"

    monkeypatch.setattr(screenshot, "capture", fake_capture)

    prompts = {}

    async def fake_run_agent(**kwargs):
        prompts["prompt"] = kwargs["prompt"]
        return AgentResult(ok=True, text="ok", json_data={"status": "pass"}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(testing, "run_agent", fake_run_agent)

    asyncio.run(testing.run_pre_prod(repo, "summary", "https://preview.example.com", "run42"))

    assert "Screenshot capture failed (boom)" in prompts["prompt"]
    assert "don't let it block" in prompts["prompt"]


# ── server.py's screenshot endpoint ────────────────────────────────────────


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


def test_get_run_screenshot_serves_the_file(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = tmp_path / "myapp"
    repo.mkdir()
    Memory(repo).set_objective("Ship things.")
    registry.register_app("myapp", str(repo), repo_url="https://github.com/acme/myapp.git", branch="main")
    registry.record_run(
        "run42", app="myapp", source="on-demand", status="completed",
        started_at=0, objective="Ship things.", loop_id="loop1",
    )
    shot_path = testing.screenshot_path(repo, "run42")
    shot_path.parent.mkdir(parents=True, exist_ok=True)
    shot_path.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-bytes")

    response = TestClient(server.app).get("/runs/run42/screenshot")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == b"\x89PNG\r\n\x1a\nfake-png-bytes"


def test_get_run_screenshot_404s_when_none_captured(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = tmp_path / "myapp"
    repo.mkdir()
    Memory(repo).set_objective("Ship things.")
    registry.register_app("myapp", str(repo), repo_url="https://github.com/acme/myapp.git", branch="main")
    registry.record_run(
        "run42", app="myapp", source="on-demand", status="completed",
        started_at=0, objective="Ship things.", loop_id="loop1",
    )

    response = TestClient(server.app).get("/runs/run42/screenshot")

    assert response.status_code == 404


def test_get_run_screenshot_404s_for_unknown_run_key(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)

    response = TestClient(server.app).get("/runs/nonexistent/screenshot")

    assert response.status_code == 404

"""server/routes/loops.py: loop list/detail + the Langfuse trace proxy."""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import langfuse_api, registry, server


def _isolate(tmp_path: Path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", home / "loops.json")
    server._active_runs.clear()


def test_loop_detail_route(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    registry.record_run("r1", app="app", status="completed", started_at=time.time())
    loop_id = registry.bind_loop("app", 12, title="t")
    registry.record_run("r1", loop_id=loop_id, issue_number="12")

    client = TestClient(server.app)
    assert client.get("/loops/does-not-exist").status_code == 404
    detail = client.get(f"/loops/{loop_id}").json()
    assert detail["loop_id"] == loop_id
    assert [r["run_key"] for r in detail["runs"]] == ["r1"]


def test_run_trace_route_proxies_langfuse(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    registry.record_run("r1", app="app", status="completed", started_at=time.time(), langfuse_trace_id="trace-xyz")

    seen = {}

    def fake_fetch(trace_id):
        seen["id"] = trace_id
        return {"id": trace_id, "observations": [{"type": "GENERATION", "name": "claude"}]}

    monkeypatch.setattr(langfuse_api, "fetch_trace", fake_fetch)

    body = TestClient(server.app).get("/runs/r1/trace").json()
    assert seen["id"] == "trace-xyz"
    assert body["observations"][0]["name"] == "claude"


def test_run_trace_route_404_when_no_trace_recorded(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    registry.record_run("r1", app="app", status="completed", started_at=time.time())

    assert TestClient(server.app).get("/runs/r1/trace").status_code == 404

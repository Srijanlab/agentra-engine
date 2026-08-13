"""Regression tests for the storage-audit finding that per-run logs were
the one data type with no durable copy anywhere: .agentra/logs/ is
gitignored on purpose, and REPOS_ROOT itself is VM-local-only (registry.py)
-- a run's log was permanently gone the moment its VM/container instance
rebuilt (which happens on every redeploy). Memory.log() now best-effort
mirrors each line into Firestore's run_logs collection, and server.py's
stream_run_logs falls back to that mirror when the local file is missing.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.memory import Memory
from agentra.server import _strip_log_timestamp


def test_log_mirrors_to_firestore_when_configured(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_db = MagicMock()
    monkeypatch.setattr(registry, "_db", fake_db)

    mem = Memory(repo)
    mem.log("run123", "cycle start")

    fake_db.collection.assert_any_call("run_logs")
    doc_ref = fake_db.collection.return_value.document.return_value
    assert doc_ref.set.call_count == 1
    call_kwargs = doc_ref.set.call_args
    assert call_kwargs.kwargs.get("merge") is True
    # Local file still gets written regardless -- Firestore is a mirror,
    # not a replacement, so a live-tailing dashboard reader on the same
    # instance keeps working exactly as before.
    assert (repo / ".agentra" / "logs" / "run123.log").exists()


def test_log_does_not_raise_when_firestore_write_fails(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_db = MagicMock()
    fake_db.collection.return_value.document.return_value.set.side_effect = RuntimeError("boom")
    monkeypatch.setattr(registry, "_db", fake_db)

    mem = Memory(repo)
    mem.log("run123", "cycle start")  # must not raise

    assert (repo / ".agentra" / "logs" / "run123.log").exists()


def test_log_skips_firestore_when_not_configured(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(registry, "_db", None)

    mem = Memory(repo)
    path = mem.log("run123", "cycle start")  # must not raise

    assert path.exists()


def test_strip_log_timestamp_removes_a_real_iso_timestamp_prefix():
    # Memory.log()'s actual on-disk format: "[<iso ts>] [Agent Label] rest" --
    # the outer timestamp bracket must go so logLineParser.ts's LABEL_RE (which
    # matches the FIRST bracketed group as the agent tag) sees "[Agent Label]",
    # not the timestamp.
    raw = "[2026-08-13T17:03:05.183079+00:00] [Orchestrator] assistant text: hello"
    assert _strip_log_timestamp(raw) == "[Orchestrator] assistant text: hello"


def test_strip_log_timestamp_leaves_a_non_timestamp_line_unchanged():
    assert _strip_log_timestamp("[Orchestrator] assistant text: hello") == "[Orchestrator] assistant text: hello"
    assert _strip_log_timestamp("no brackets at all") == "no brackets at all"
    assert _strip_log_timestamp("[t1] cycle start") == "[t1] cycle start"


def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "PAUSE_PATH", home / "paused.json")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")
    server._active_runs.clear()
    server._app_locks.clear()


def test_stream_run_logs_falls_back_to_firestore_when_local_file_missing(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, "_db", None)  # local-only registry for run/app bookkeeping

    repo = tmp_path / "myapp"
    repo.mkdir()
    Memory(repo).set_objective("Ship things.")
    registry.register_app("myapp", str(repo), repo_url="https://github.com/acme/myapp.git", branch="main")
    registry.record_run(
        "run123", app="myapp", source="on-demand", status="completed",
        started_at=0, objective="Ship things.", loop_id="loop1",
    )
    # No local log file was ever written for this run_key (simulating a
    # redeploy that wiped REPOS_ROOT since the run finished) -- only
    # Firestore has it.
    fake_doc = MagicMock()
    fake_doc.exists = True
    fake_doc.to_dict.return_value = {"lines": ["[t1] cycle start", "[t2] cycle complete"]}
    fake_db = MagicMock()
    fake_db.collection.return_value.document.return_value.get.return_value = fake_doc
    monkeypatch.setattr(server.registry, "firestore_client", lambda: fake_db)

    client = TestClient(server.app)
    with client.stream("GET", "/runs/run123/logs") as response:
        body = "".join(response.iter_text())

    assert "cycle start" in body
    assert "cycle complete" in body
    assert "event: done" in body


def test_stream_run_logs_strips_the_timestamp_from_a_locally_tailed_line(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, "_db", None)

    repo = tmp_path / "myapp"
    repo.mkdir()
    Memory(repo).set_objective("Ship things.")
    registry.register_app("myapp", str(repo), repo_url="https://github.com/acme/myapp.git", branch="main")
    registry.record_run(
        "run456", app="myapp", source="on-demand", status="completed",
        started_at=0, objective="Ship things.", loop_id="loop1",
    )
    mem = Memory(repo)
    mem.log("run456", "[Orchestrator] assistant text: hello")

    client = TestClient(server.app)
    with client.stream("GET", "/runs/run456/logs") as response:
        body = "".join(response.iter_text())

    # The real bug: without stripping, this line would stream as
    # "[<iso-timestamp>] [Orchestrator] assistant text: hello" -- the
    # frontend's agent-tag matcher (logLineParser.ts's LABEL_RE) would then
    # pick up the timestamp as the "agent" instead of "Orchestrator", and
    # the line would fail to match any known pattern and render as noise.
    assert '"line": "[Orchestrator] assistant text: hello"' in body
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", body)  # no leftover ISO timestamp anywhere in the payload

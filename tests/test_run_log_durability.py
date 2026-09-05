"""Regression tests for the storage-audit finding that per-run logs were
the one data type with no durable copy anywhere: .agentra/logs/ is
gitignored on purpose, and REPOS_ROOT itself is VM-local-only (registry.py)
-- a run's log was permanently gone the moment its VM/container instance
rebuilt (which happens on every redeploy).

Bug #77: Memory.log() used to do a synchronous durable read+write on
*every* call (every SDK stream event, ~200-300ms), blocking the async agent
loop. It now only appends to a local per-run buffer; the registry gets a
single full-item write via a periodic safety flush (line/time threshold) and
once more when the run reaches a terminal state (Memory.finalize_run_log).
server.py's stream_run_logs falls back to that durable copy only when the
local file is missing.

Uses a real (moto-mocked) DynamoDB table rather than a hand-shaped fake --
the whole point of these tests is real persistence semantics (single
full-item overwrite, not a per-line write), not just a truthiness branch.
"""

import re

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from agentra import registry, server
import agentra.memory as memory_module
from agentra.registry import _dynamo
from agentra.memory import Memory
from agentra.server import _strip_log_timestamp


@pytest.fixture
def ddb_run_logs(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AGENTRA_DYNAMODB_TABLE_PREFIX", "")
        resource = boto3.resource("dynamodb", region_name="us-west-2")
        resource.create_table(
            TableName="run-logs",
            KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # apps/runs share the same _ddb sentinel (already ported in earlier
        # phases) -- register_app()/get_app_repo()/record_run() all need
        # somewhere to write even though this file is only testing run-logs.
        resource.create_table(
            TableName="apps",
            KeySchema=[{"AttributeName": "name", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "name", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        resource.create_table(
            TableName="runs",
            KeySchema=[{"AttributeName": "run_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "run_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(registry, "_ddb", resource)
        _dynamo._table_cache.clear()
        yield resource
        _dynamo._table_cache.clear()


def test_log_always_appends_to_the_local_file(tmp_path, ddb_run_logs):
    repo = tmp_path / "repo"
    repo.mkdir()

    mem = Memory(repo)
    for i in range(5):
        mem.log("run123", f"line {i}")

    log_path = repo / ".agentra" / "logs" / "run123.log"
    assert log_path.exists()
    assert len(log_path.read_text().splitlines()) == 5


def test_log_does_not_write_to_the_registry_per_call(tmp_path, ddb_run_logs):
    repo = tmp_path / "repo"
    repo.mkdir()

    mem = Memory(repo)
    for i in range(10):
        mem.log("run123", f"line {i}")

    # No per-line read+write: well under the periodic-flush thresholds, so
    # the table should not have been touched at all yet.
    assert _dynamo.get_item(_dynamo.table("run-logs"), {"run_id": "run123"}) is None


def test_finalize_run_log_writes_once_with_the_full_buffered_log(tmp_path, ddb_run_logs):
    repo = tmp_path / "repo"
    repo.mkdir()

    mem = Memory(repo)
    for i in range(10):
        mem.log("run123", f"line {i}")
    mem.finalize_run_log("run123")

    item = _dynamo.get_item(_dynamo.table("run-logs"), {"run_id": "run123"})
    assert len(item["lines"]) == 10
    assert item["lines"][0].endswith("line 0")
    assert item["lines"][-1].endswith("line 9")

    # A second finalize on the (now-cleared) buffer is a no-op -- nothing
    # left to flush, so the stored item is untouched (not overwritten empty).
    mem.finalize_run_log("run123")
    item_again = _dynamo.get_item(_dynamo.table("run-logs"), {"run_id": "run123"})
    assert item_again == item


def test_finalize_run_log_caps_at_500_lines(tmp_path, ddb_run_logs):
    repo = tmp_path / "repo"
    repo.mkdir()

    mem = Memory(repo)
    for i in range(600):
        mem.log("run123", f"line {i}")
    mem.finalize_run_log("run123")

    item = _dynamo.get_item(_dynamo.table("run-logs"), {"run_id": "run123"})
    assert len(item["lines"]) == 500
    assert item["lines"][-1].endswith("line 599")


def test_periodic_safety_flush_fires_once_the_line_threshold_is_crossed(tmp_path, ddb_run_logs, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(memory_module, "RUN_LOG_FLUSH_MAX_LINES", 5)

    mem = Memory(repo)
    for i in range(4):
        mem.log("run123", f"line {i}")
    assert _dynamo.get_item(_dynamo.table("run-logs"), {"run_id": "run123"}) is None

    mem.log("run123", "line 4")  # crosses the (patched) 5-line threshold

    item = _dynamo.get_item(_dynamo.table("run-logs"), {"run_id": "run123"})
    assert len(item["lines"]) == 5


def test_periodic_safety_flush_fires_once_the_time_threshold_is_crossed(tmp_path, ddb_run_logs, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(memory_module, "RUN_LOG_FLUSH_INTERVAL_SECONDS", 60)

    fake_clock = {"t": 1000.0}
    monkeypatch.setattr(memory_module.time, "monotonic", lambda: fake_clock["t"])

    mem = Memory(repo)
    mem.log("run123", "line 0")
    assert _dynamo.get_item(_dynamo.table("run-logs"), {"run_id": "run123"}) is None

    fake_clock["t"] += 61  # crosses the (patched) 60s threshold
    mem.log("run123", "line 1")

    item = _dynamo.get_item(_dynamo.table("run-logs"), {"run_id": "run123"})
    assert item is not None


def test_log_does_not_raise_when_the_registry_write_fails(tmp_path, ddb_run_logs, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(_dynamo, "put_item", _boom)

    mem = Memory(repo)
    mem.log("run123", "cycle start")  # must not raise
    mem.finalize_run_log("run123")  # must not raise either

    assert (repo / ".agentra" / "logs" / "run123.log").exists()


def test_log_skips_the_registry_when_not_configured(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(registry, "_ddb", None)

    mem = Memory(repo)
    path = mem.log("run123", "cycle start")  # must not raise
    mem.finalize_run_log("run123")  # must not raise either

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
    server._active_runs.clear()
    server._app_locks.clear()


def test_stream_run_logs_falls_back_to_the_registry_when_local_file_missing(tmp_path, ddb_run_logs, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, "_db", None)  # local-only for run/app bookkeeping -- only run-logs is DynamoDB-backed here

    repo = tmp_path / "myapp"
    repo.mkdir()
    Memory(repo).set_objective("Ship things.")
    registry.register_app("myapp", str(repo), repo_url="https://github.com/acme/myapp.git", branch="main")
    registry.record_run(
        "run123", app="myapp", source="on-demand", status="completed",
        started_at=0, objective="Ship things.", loop_id="loop1",
    )
    # No local log file was ever written for this run_key (simulating a
    # redeploy that wiped REPOS_ROOT since the run finished) -- only the
    # registry has it.
    _dynamo.put_item(_dynamo.table("run-logs"), {"run_id": "run123", "lines": ["[t1] cycle start", "[t2] cycle complete"]})

    client = TestClient(server.app)
    with client.stream("GET", "/runs/run123/logs") as response:
        body = "".join(response.iter_text())

    assert "cycle start" in body
    assert "cycle complete" in body
    assert "event: done" in body


def test_stream_run_logs_strips_the_timestamp_from_a_locally_tailed_line(tmp_path, ddb_run_logs, monkeypatch):
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

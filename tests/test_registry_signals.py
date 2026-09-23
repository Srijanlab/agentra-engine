"""registry.signals — the durable event log GET /signals reads from, replacing
the dead AGENTRA_HOME/server.log path that _server_log() never actually wrote."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import boto3
import pytest
from moto import mock_aws

from agentra import registry
from agentra.registry import _dynamo, core, signals


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_ddb", None)
    monkeypatch.setattr(core, "AGENTRA_HOME", tmp_path / "agentra_home")


def test_list_signals_empty_when_nothing_recorded(local_env):
    assert signals.list_signals() == []


def test_record_signal_is_retrievable_with_expected_shape(local_env):
    signals.record_signal("pause", "system paused: reason=None")

    [entry] = signals.list_signals()
    assert entry["source"] == "pause"
    assert entry["message"] == "system paused: reason=None"
    assert isinstance(entry["ts"], float)


def test_list_signals_most_recent_first(local_env):
    signals.record_signal("pause", "first", ts=1.0)
    signals.record_signal("resume", "second", ts=2.0)

    result = signals.list_signals()

    assert [e["source"] for e in result] == ["resume", "pause"]


def test_list_signals_respects_limit(local_env):
    for i in range(5):
        signals.record_signal("scheduled", f"event {i}", ts=float(i))

    result = signals.list_signals(limit=2)

    assert len(result) == 2
    assert [e["message"] for e in result] == ["event 4", "event 3"]


def test_record_signal_caps_at_200_most_recent(local_env):
    for i in range(250):
        signals.record_signal("scheduled", f"event {i}", ts=float(i))

    result = signals.list_signals(limit=1000)

    assert len(result) == 200
    assert result[0]["message"] == "event 249"
    assert result[-1]["message"] == "event 50"


def test_server_log_persists_a_retrievable_signal(local_env):
    from agentra.server.utils import _server_log

    _server_log("llm-backend", "llm backend set to 'nim'")

    [entry] = registry.list_signals()
    assert entry["source"] == "llm-backend"
    assert entry["message"] == "llm backend set to 'nim'"


def test_server_log_swallows_storage_errors(monkeypatch, caplog):
    from agentra.server.utils import _server_log

    def boom(*_a, **_k):
        raise RuntimeError("signals table down")

    monkeypatch.setattr(registry, "record_signal", boom)

    with caplog.at_level(logging.WARNING, logger="agentra.server"):
        assert _server_log("pause", "system paused") is None

    assert any("signals table down" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


class _AtomicTable:
    """Serializes update_item like real DynamoDB does per item, since moto's in-memory update races across threads."""

    def __init__(self, tbl):
        self._tbl = tbl
        self._lock = threading.Lock()

    def update_item(self, **kwargs):
        with self._lock:
            return self._tbl.update_item(**kwargs)

    def __getattr__(self, name):
        return getattr(self._tbl, name)


@pytest.fixture
def ddb_env(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AGENTRA_DYNAMODB_TABLE_PREFIX", "sigtest-")
        monkeypatch.setenv("AGENTRA_AWS_REGION", "us-west-2")
        resource = boto3.resource("dynamodb", region_name="us-west-2")
        resource.create_table(
            TableName="sigtest-system",
            KeySchema=[{"AttributeName": "key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "key", "AttributeType": "S"}],
            ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
        )
        monkeypatch.setattr(core, "_ddb", resource)
        _dynamo._table_cache.clear()
        _dynamo._table_cache["sigtest-system"] = _AtomicTable(resource.Table("sigtest-system"))
        yield
        _dynamo._table_cache.clear()


def test_dynamo_concurrent_appends_all_persist(ddb_env):
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda i: signals.record_signal("resume", f"event {i}", ts=float(i)), range(50)))

    assert {e["message"] for e in signals.list_signals(limit=1000)} == {f"event {i}" for i in range(50)}


def test_dynamo_caps_at_200_newest_first(ddb_env):
    for i in range(210):
        signals.record_signal("scheduled", f"event {i}", ts=float(i))

    result = signals.list_signals(limit=1000)

    assert len(result) == 200
    assert result[0]["message"] == "event 209"
    assert result[-1]["message"] == "event 10"
    assert [e["ts"] for e in result] == sorted((e["ts"] for e in result), reverse=True)

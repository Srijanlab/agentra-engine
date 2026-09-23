"""registry.signals — the durable event log GET /signals reads from, replacing
the dead AGENTRA_HOME/server.log path that _server_log() never actually wrote."""

import pytest

from agentra import registry
from agentra.registry import core, signals


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

"""GET /signals reads from the durable registry-backed event log (registry.signals),
not the dead AGENTRA_HOME/server.log path -- every mutating route that calls
_server_log() must show up here in real deployments, not just local dev fixtures."""

from test_server_triggers import _client, _isolate


def test_signals_empty_list_when_no_events(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    resp = _client().get("/signals")

    assert resp.status_code == 200
    body = resp.json()
    assert body["signals"] == []


def test_pause_then_signals_contains_pause_entry(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    client = _client()

    resp = client.post("/system/pause", json={"reason": "maintenance"})
    assert resp.status_code == 200
    assert resp.json() == {"paused": True}

    signals = client.get("/signals").json()["signals"]
    assert any(s["source"] == "pause" for s in signals)
    for entry in signals:
        assert {"ts", "source", "message"} <= entry.keys()


def test_pause_then_resume_orders_most_recent_first(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    client = _client()

    assert client.post("/system/pause", json={"reason": "x"}).status_code == 200
    assert client.post("/system/resume").json() == {"paused": False}

    signals = client.get("/signals").json()["signals"]
    assert signals[0]["source"] == "resume"
    assert any(s["source"] == "pause" for s in signals[1:])


def test_signals_limit_returns_most_recent_only(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    client = _client()

    client.post("/system/pause", json={"reason": "x"})
    client.post("/system/resume")

    signals = client.get("/signals?limit=1").json()["signals"]

    assert len(signals) == 1
    assert signals[0]["source"] == "resume"


def test_llm_backend_change_is_reflected_in_signals(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    client = _client()

    resp = client.post("/system/llm-backend", json={"backend": "nim"})
    assert resp.status_code == 200

    signals = client.get("/signals").json()["signals"]
    assert any(s["source"] == "llm-backend" for s in signals)

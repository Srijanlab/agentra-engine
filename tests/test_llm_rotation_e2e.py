"""In-process end-to-end checks of the LLM rotation RPCs."""

from collections import Counter

import pytest
from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.registry import core, llm_pool

TOKEN = "test-internal-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(registry, "_ddb", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "_LLM_BACKEND_PATH", home / "llm_backend.json")
    monkeypatch.setattr(core, "_llm_backend_cache", None)
    monkeypatch.setenv("AGENTRA_INTERNAL_TOKEN", TOKEN)
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    return TestClient(server.app)


def _rpc(client, method, *args, token=TOKEN):
    body = {"target": "registry", "method": method, "args": list(args), "kwargs": {}}
    return client.post("/internal/rpc", json=body, headers={"Authorization": f"Bearer {token}"})


def test_set_then_get_rotation_returns_backends_and_index(client):
    assert _rpc(client, "set_llm_rotation", ["claude", "nim", "gemini"]).status_code == 200
    got = _rpc(client, "get_llm_rotation").json()["result"]
    assert set(got) == {"backends", "current_index"}
    assert got == {"backends": ["claude", "nim", "gemini"], "current_index": 0}


def test_unknown_provider_is_400_and_rotation_unchanged(client):
    _rpc(client, "set_llm_rotation", ["claude", "nim"])
    _rpc(client, "select_llm_provider")
    before = _rpc(client, "get_llm_rotation").json()["result"]
    assert _rpc(client, "set_llm_rotation", ["claude", "bogus"]).status_code == 400
    assert _rpc(client, "get_llm_rotation").json()["result"] == before


def test_wrong_or_missing_token_never_changes_rotation(client):
    _rpc(client, "set_llm_rotation", ["claude", "nim"])
    assert _rpc(client, "set_llm_rotation", ["claude", "bogus"], token="wrong").status_code == 401
    body = {"target": "registry", "method": "get_llm_rotation", "args": [], "kwargs": {}}
    assert client.post("/internal/rpc", json=body).status_code == 401
    assert _rpc(client, "get_llm_rotation").json()["result"]["backends"] == ["claude", "nim"]


def test_index_advances_per_selection_mod_pool_size(client):
    _rpc(client, "set_llm_rotation", ["claude", "nim", "gemini"])
    for expected in (1, 2, 0, 1):
        _rpc(client, "select_llm_provider")
        assert _rpc(client, "get_llm_rotation").json()["result"]["current_index"] == expected


@pytest.mark.parametrize("pool", [["claude", "nim", "gemini"], ["claude", "nim"]])
def test_simulated_24h_selects_each_backend_equally(client, monkeypatch, pool):
    _rpc(client, "set_llm_rotation", *[pool])
    start = llm_pool.time.time()
    clock = {"now": start}
    monkeypatch.setattr(llm_pool.time, "time", lambda: clock["now"])
    picks = []
    for hour in range(24):
        clock["now"] = start + hour * 3600
        picks.append(_rpc(client, "select_llm_provider").json()["result"]["backend"])
    counts = Counter(picks)
    assert set(counts) == set(pool)
    assert set(counts.values()) == {24 // len(pool)}


def test_debug_rotation_reflects_rpc_set_without_credentials(client):
    assert _rpc(client, "set_llm_rotation", ["claude", "nim", "gemini"]).status_code == 200
    expected = {"backends": ["claude", "nim", "gemini"], "current_index": 0}
    resp = client.get("/debug/llm-rotation")
    assert resp.status_code == 200
    assert resp.json() == expected
    assert _rpc(client, "get_llm_rotation").json()["result"] == expected


def test_debug_rotation_is_read_only(client):
    _rpc(client, "set_llm_rotation", ["claude", "nim", "gemini"])
    bodies = [client.get("/debug/llm-rotation").json() for _ in range(3)]
    assert bodies == [{"backends": ["claude", "nim", "gemini"], "current_index": 0}] * 3
    for method in (client.post, client.put, client.delete):
        assert method("/debug/llm-rotation").status_code == 405
    assert client.get("/debug/llm-rotation").json() == bodies[0]


def test_debug_rotation_and_llm_pool_both_need_sign_in(client, monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    assert client.get("/debug/llm-rotation").status_code == 401
    assert client.get("/system/llm-pool").status_code == 401


def test_unauthenticated_set_rpc_leaves_debug_rotation_unchanged(client):
    _rpc(client, "set_llm_rotation", ["claude", "nim"])
    body = {"target": "registry", "method": "set_llm_rotation", "args": [["gemini"]], "kwargs": {}}
    assert client.post("/internal/rpc", json=body).status_code == 401
    assert client.get("/debug/llm-rotation").json()["backends"] == ["claude", "nim"]

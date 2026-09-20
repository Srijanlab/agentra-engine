"""registry.llm_pool: ordered provider pool, cooldown state, fallback selection, RPC + REST."""

import json

import pytest
from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.registry import core, llm_pool

TOKEN = "test-internal-token"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(registry, "_ddb", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "_LLM_BACKEND_PATH", home / "llm_backend.json")
    monkeypatch.setattr(core, "_llm_backend_cache", None)
    monkeypatch.setenv("AGENTRA_INTERNAL_TOKEN", TOKEN)
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    return home


@pytest.fixture
def client(home):
    return TestClient(server.app)


def _rpc(client, method, *args, **kwargs):
    body = {"target": "registry", "method": method, "args": list(args), "kwargs": kwargs}
    return client.post("/internal/rpc", json=body, headers={"Authorization": f"Bearer {TOKEN}"})


def test_default_pool_is_the_single_backend(home):
    assert registry.get_llm_rotation() == {"backends": ["claude"], "current_index": 0}


def test_set_rotation_persists_under_rotation_key_and_keeps_backend(home):
    registry.set_llm_backend("nim")
    registry.set_llm_rotation(["claude", "nim", "gemini"])
    registry.set_llm_backend("claude")
    data = json.loads((home / "llm_backend.json").read_text())
    assert data["rotation"] == ["claude", "nim", "gemini"]
    assert data["backend"] == "claude"


@pytest.mark.parametrize("bad", [["claude", "bogus"], [], "claude", ["nim", "nim"], None])
def test_invalid_rotation_rejected_and_unchanged(home, bad):
    registry.set_llm_rotation(["claude", "nim"])
    with pytest.raises(registry.InvalidLLMPool):
        registry.set_llm_rotation(bad)
    assert registry.get_llm_rotation()["backends"] == ["claude", "nim"]


def test_selection_round_robins_and_wraps(home):
    registry.set_llm_rotation(["claude", "nim", "gemini"])
    picks = [registry.select_llm_provider()["backend"] for _ in range(6)]
    assert picks == ["claude", "nim", "gemini", "claude", "nim", "gemini"]


def test_selection_skips_cooling_provider(home):
    registry.set_llm_rotation(["claude", "nim", "gemini"])
    registry.report_llm_provider_failure("claude", retry_after_seconds=600)
    picks = [registry.select_llm_provider() for _ in range(3)]
    assert [p["backend"] for p in picks] == ["nim", "gemini", "nim"]
    assert not any(p["cooling_down"] for p in picks)


def test_all_cooling_returns_soonest_available(home):
    registry.set_llm_rotation(["claude", "nim"])
    registry.report_llm_provider_failure("claude", retry_after_seconds=600)
    registry.report_llm_provider_failure("nim", retry_after_seconds=60)
    pick = registry.select_llm_provider()
    assert pick["backend"] == "nim"
    assert pick["cooling_down"] is True
    assert 0 < pick["retry_after_seconds"] <= 60


def test_failure_backoff_grows_and_success_clears(home):
    registry.set_llm_rotation(["claude", "nim"])
    first = registry.report_llm_provider_failure("claude")
    second = registry.report_llm_provider_failure("claude")
    assert second["failures"] == 2
    assert second["rate_limited_until"] - second["last_failure_at"] == pytest.approx(
        2 * (first["rate_limited_until"] - first["last_failure_at"])
    )
    registry.report_llm_provider_success("claude")
    health = registry.get_llm_provider_health()["claude"]
    assert health["failures"] == 0 and health["cooling_down"] is False


def test_cooldown_expires(home, monkeypatch):
    registry.set_llm_rotation(["claude", "nim"])
    registry.report_llm_provider_failure("claude", retry_after_seconds=10)
    now = llm_pool.time.time()
    monkeypatch.setattr(llm_pool.time, "time", lambda: now + 11)
    assert registry.get_llm_provider_health()["claude"]["cooling_down"] is False


def test_unknown_provider_failure_rejected(home):
    with pytest.raises(registry.InvalidLLMPool):
        registry.report_llm_provider_failure("bogus")
    with pytest.raises(registry.InvalidLLMPool):
        registry.report_llm_provider_failure("claude", retry_after_seconds="soon")


def test_rpc_round_trip(client):
    assert _rpc(client, "set_llm_rotation", ["claude", "nim", "gemini"]).status_code == 200
    got = _rpc(client, "get_llm_rotation").json()["result"]
    assert got == {"backends": ["claude", "nim", "gemini"], "current_index": 0}
    assert _rpc(client, "select_llm_provider").json()["result"]["backend"] == "claude"
    _rpc(client, "report_llm_provider_failure", "nim", retry_after_seconds=300)
    assert _rpc(client, "select_llm_provider").json()["result"]["backend"] == "gemini"
    assert _rpc(client, "get_llm_provider_health").json()["result"]["nim"]["cooling_down"] is True


def test_rpc_invalid_rotation_is_400_and_unchanged(client):
    _rpc(client, "set_llm_rotation", ["claude", "nim"])
    assert _rpc(client, "set_llm_rotation", ["claude", "bogus"]).status_code == 400
    assert _rpc(client, "get_llm_rotation").json()["result"]["backends"] == ["claude", "nim"]


def test_rest_endpoints(client):
    assert client.put("/system/llm-pool", json={"backends": ["claude", "openai"]}).status_code == 200
    assert client.put("/system/llm-pool", json={"backends": ["nope"]}).status_code == 400
    assert client.post("/system/llm-pool/next").json()["backend"] == "claude"
    resp = client.post("/system/llm-pool/openai/throttle", json={"retry_after_seconds": 120})
    assert resp.json()["cooling_down"] is True
    pool = client.get("/system/llm-pool").json()
    assert pool["backends"] == ["claude", "openai"]
    assert pool["health"]["openai"]["failures"] == 1
    assert client.post("/system/llm-pool/nope/throttle").status_code == 400
    assert client.post("/system/llm-pool/openai/success").json()["cooling_down"] is False

"""agentra.agents.base._sdk_env: extra env handed to the Claude Agent SDK subprocess --
empty for the default Claude backend, NIM proxy pointers when the toggle selects "nim"."""

from agentra.agents import base


def test_claude_backend_returns_empty(monkeypatch):
    monkeypatch.setattr("agentra.registry.get_llm_backend", lambda: "claude")
    monkeypatch.setenv("NIM_PROXY_URL", "http://agentra-nim-nginx")
    assert base._sdk_env() == {}


def test_nim_backend_points_at_proxy(monkeypatch):
    monkeypatch.setattr("agentra.registry.get_llm_backend", lambda: "nim")
    monkeypatch.setenv("NIM_PROXY_URL", "http://agentra-nim-nginx")
    monkeypatch.delenv("NIM_PROXY_API_KEY", raising=False)
    assert base._sdk_env() == {
        "ANTHROPIC_BASE_URL": "http://agentra-nim-nginx",
        "ANTHROPIC_API_KEY": "nim-proxy",
    }


def test_nim_backend_without_proxy_url_returns_empty(monkeypatch):
    monkeypatch.setattr("agentra.registry.get_llm_backend", lambda: "nim")
    monkeypatch.delenv("NIM_PROXY_URL", raising=False)
    assert base._sdk_env() == {}

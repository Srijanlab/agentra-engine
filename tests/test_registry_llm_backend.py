"""registry.get_llm_backend/set_llm_backend: global Claude-vs-NIM toggle, set from the
dashboard and read on every agent turn (agentra/agents/base.py:_sdk_env)."""

import pytest

from agentra import registry
from agentra.registry import core


def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    home.mkdir()
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "_LLM_BACKEND_PATH", home / "llm_backend.json")
    monkeypatch.setattr(core, "_llm_backend_cache", None)


def test_default_backend_is_claude(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    assert registry.get_llm_backend() == "claude"


def test_set_then_get_round_trips(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    registry.set_llm_backend("nim")
    assert registry.get_llm_backend() == "nim"


def test_set_clears_the_ttl_cache(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    assert registry.get_llm_backend() == "claude"  # populates cache
    registry.set_llm_backend("nim")
    assert registry.get_llm_backend() == "nim"
    registry.set_llm_backend("claude")
    assert registry.get_llm_backend() == "claude"


def test_invalid_backend_rejected(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        registry.set_llm_backend("gpt5")


def test_unknown_persisted_value_falls_back_to_claude(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    (tmp_path / "agentra_home" / "llm_backend.json").write_text('{"backend": "bogus"}')
    assert registry.get_llm_backend() == "claude"

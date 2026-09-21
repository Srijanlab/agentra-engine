"""Ordered LLM provider pool with per-provider cooldown state, stored in DynamoDB or local JSON."""

from __future__ import annotations

import json
import time

from agentra.registry import core

_DDB_KEY = "llm_pool"
_BASE_COOLDOWN_SECONDS = 60
_MAX_COOLDOWN_SECONDS = 3600


class InvalidLLMPool(ValueError):
    """Raised when a rotation or provider name is not a known backend."""


def _validate_provider(provider: str) -> str:
    if provider not in core.VALID_LLM_BACKENDS:
        raise InvalidLLMPool(f"unknown llm backend {provider!r} -- expected one of {core.VALID_LLM_BACKENDS}")
    return provider


def _validate_pool(backends: object) -> list[str]:
    if not isinstance(backends, list) or not backends:
        raise InvalidLLMPool("backends must be a non-empty list")
    for name in backends:
        _validate_provider(name)
    if len(set(backends)) != len(backends):
        raise InvalidLLMPool("backends must not contain duplicates")
    return list(backends)


def _load() -> dict:
    if core._ddb is not None:
        from agentra.registry import _dynamo

        return _dynamo.get_item(_dynamo.table("system"), {"key": _DDB_KEY}) or {}
    if core._LLM_BACKEND_PATH.exists():
        return json.loads(core._LLM_BACKEND_PATH.read_text())
    return {}


def _save(state: dict) -> None:
    pool_state = {k: state[k] for k in ("rotation", "current_index", "health") if k in state}
    if core._ddb is not None:
        from agentra.registry import _dynamo

        _dynamo.put_item(_dynamo.table("system"), {"key": _DDB_KEY, **pool_state})
        return
    on_disk = json.loads(core._LLM_BACKEND_PATH.read_text()) if core._LLM_BACKEND_PATH.exists() else {}
    core._LLM_BACKEND_PATH.parent.mkdir(parents=True, exist_ok=True)
    core._LLM_BACKEND_PATH.write_text(json.dumps({**on_disk, **pool_state}, indent=2))


def _pool(state: dict) -> list[str]:
    rotation = [b for b in state.get("rotation") or [] if b in core.VALID_LLM_BACKENDS]
    return rotation or [core.get_llm_backend()]


def _health_entry(state: dict, provider: str) -> dict:
    entry = (state.get("health") or {}).get(provider) or {}
    return {
        "failures": int(entry.get("failures", 0)),
        "rate_limited_until": float(entry.get("rate_limited_until", 0)),
        "last_failure_at": entry.get("last_failure_at"),
    }


def get_llm_rotation() -> dict:
    """The ordered provider pool and the index the next selection starts from."""
    state = _load()
    pool = _pool(state)
    return {"backends": pool, "current_index": int(state.get("current_index", 0)) % len(pool)}


def set_llm_rotation(backends: list[str]) -> dict:
    """Replaces the provider pool (validated, no duplicates) and restarts the rotation at index 0."""
    pool = _validate_pool(backends)
    state = _load()
    _save({**state, "rotation": pool, "current_index": 0})
    return {"backends": pool, "current_index": 0}


def get_llm_provider_health() -> dict[str, dict]:
    """Per pool provider: failures, rate_limited_until, last_failure_at, cooling_down."""
    state = _load()
    now = time.time()
    out = {}
    for provider in _pool(state):
        entry = _health_entry(state, provider)
        out[provider] = {**entry, "cooling_down": entry["rate_limited_until"] > now}
    return out


def select_llm_provider() -> dict:
    """Next provider round-robin, skipping any in cooldown; if all are cooling, the one that frees up first."""
    state = _load()
    pool = _pool(state)
    now = time.time()
    start = int(state.get("current_index", 0)) % len(pool)
    order = [(start + i) % len(pool) for i in range(len(pool))]
    until = {i: _health_entry(state, pool[i])["rate_limited_until"] for i in order}
    chosen = next((i for i in order if until[i] <= now), None)
    cooling = chosen is None
    if cooling:
        chosen = min(order, key=lambda i: until[i])
    _save({**state, "current_index": (chosen + 1) % len(pool)})
    return {
        "backend": pool[chosen],
        "index": chosen,
        "cooling_down": cooling,
        "retry_after_seconds": max(0.0, until[chosen] - now) if cooling else 0.0,
    }


def report_llm_provider_failure(provider: str, retry_after_seconds: float | None = None) -> dict:
    """Records a throttle/failure and starts a cooldown (the given retry-after, else exponential backoff)."""
    _validate_provider(provider)
    state = _load()
    entry = _health_entry(state, provider)
    failures = entry["failures"] + 1
    try:
        cooldown = None if retry_after_seconds is None else float(retry_after_seconds)
    except (TypeError, ValueError) as exc:
        raise InvalidLLMPool("retry_after_seconds must be a number") from exc
    if cooldown is None or cooldown < 0:
        cooldown = min(_BASE_COOLDOWN_SECONDS * 2 ** (failures - 1), _MAX_COOLDOWN_SECONDS)
    now = time.time()
    updated = {"failures": failures, "rate_limited_until": now + cooldown, "last_failure_at": now}
    _save({**state, "health": {**(state.get("health") or {}), provider: updated}})
    return {**updated, "cooling_down": True}


def report_llm_provider_success(provider: str) -> dict:
    """Clears a provider's failure count and cooldown."""
    _validate_provider(provider)
    state = _load()
    cleared = {"failures": 0, "rate_limited_until": 0.0, "last_failure_at": None}
    _save({**state, "health": {**(state.get("health") or {}), provider: cleared}})
    return {**cleared, "cooling_down": False}

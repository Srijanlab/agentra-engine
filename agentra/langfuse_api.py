"""langfuse_api.py — read-side proxy to Langfuse so the dashboard never holds keys."""

from __future__ import annotations

import os

import httpx

from agentra.registry import _cache

_TIMEOUT = 15.0


def enabled() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


def _base() -> str:
    return (os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com").rstrip("/")


def _auth() -> tuple[str, str]:
    return (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])


def fetch_trace(trace_id: str) -> dict | None:
    """The full trace with its nested observations, or None if not found / not
    configured. Cached briefly -- a finished trace is immutable."""
    if not enabled():
        return None
    return _cache.get_or_set(f"lf-trace:{trace_id}", lambda: _get(f"/api/public/traces/{trace_id}"), ttl=30)


def list_recent_generations(app: str | None = None, limit: int = 100) -> list[dict]:
    """Agent-turn history for the dashboard's AgentsPanel (GET /agent-steps),
    read straight from Langfuse's own observations -- every agent turn already
    emits one `generation` observation via agents/base.py's
    _emit_agent_observability (model, tokens, cost, turns, ok, summary) as a
    side effect of run_agent(), so this is a different read of data Langfuse
    already has, not a separate write path (which the old agent_steps
    Firestore/DynamoDB table was -- pure duplicate bookkeeping, now removed).
    `run_id` has no Langfuse equivalent (that's agentra's own id, stored on the
    *run* record, not the observation) -- filled with the observation's
    traceId as a best-effort stand-in; AgentsPanel.tsx, the only consumer,
    never reads it."""
    if not enabled():
        return []
    params: dict = {"type": "GENERATION", "limit": limit}
    if app is not None:
        params["userId"] = app
    data = _get("/api/public/observations", params=params)
    if data is None:
        return []
    out = []
    for obs in data.get("data", []):
        metadata = obs.get("metadata") or {}
        level = obs.get("level")
        out.append({
            "app": app,
            "run_id": obs.get("traceId"),
            "agent": obs.get("name"),
            "ok": None if level is None else level != "ERROR",
            "cost_usd": obs.get("totalCost") or obs.get("calculatedTotalCost") or 0.0,
            "turns": metadata.get("turns"),
            "summary": (obs.get("output") or "") if isinstance(obs.get("output"), str) else "",
            "ts": obs.get("startTime"),
            "input_tokens": obs.get("inputTokens"),
            "output_tokens": obs.get("outputTokens"),
            "cache_read_input_tokens": metadata.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": metadata.get("cache_creation_input_tokens"),
        })
    return out


def _get(path: str, params: dict | None = None) -> dict | None:
    try:
        resp = httpx.get(f"{_base()}{path}", auth=_auth(), params=params, timeout=_TIMEOUT)
    except httpx.HTTPError:
        return None
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()

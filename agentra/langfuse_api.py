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


def _get(path: str) -> dict | None:
    try:
        resp = httpx.get(f"{_base()}{path}", auth=_auth(), timeout=_TIMEOUT)
    except httpx.HTTPError:
        return None
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()

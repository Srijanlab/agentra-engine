"""observability.py — Langfuse tracing for the Claude Agent SDK.

init_observability() wires the OpenInference Claude Agent SDK instrumentor into
Langfuse. It's a no-op without LANGFUSE_* credentials, so local dev and tests are
unaffected. Import and call it once, AFTER environment variables are loaded.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("agentra.observability")

_initialized = False

# Redact obvious secrets from anything we explicitly hand to Langfuse.
_SECRET_RE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-ant-[A-Za-z0-9-]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|Bearer\s+[A-Za-z0-9._-]{20,})",
    re.DOTALL,
)


def _mask(data):
    if isinstance(data, str):
        return _SECRET_RE.sub("[REDACTED]", data)
    if isinstance(data, dict):
        return {k: _mask(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_mask(v) for v in data]
    return data


def enabled() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


def init_observability() -> None:
    """Idempotent. Safe to call when Langfuse isn't configured (does nothing)."""
    global _initialized
    if _initialized or not enabled():
        return

    # langfuse-cli / SDK read LANGFUSE_HOST; we standardise on LANGFUSE_BASE_URL.
    if os.environ.get("LANGFUSE_BASE_URL") and not os.environ.get("LANGFUSE_HOST"):
        os.environ["LANGFUSE_HOST"] = os.environ["LANGFUSE_BASE_URL"]

    try:
        from langfuse import Langfuse
        from openinference.instrumentation.claude_agent_sdk import ClaudeAgentSDKInstrumentor

        Langfuse(mask=_mask)  # singleton; get_client() returns it
        ClaudeAgentSDKInstrumentor().instrument()
        _initialized = True
        logger.info("Langfuse tracing enabled (%s)", os.environ.get("LANGFUSE_HOST"))
    except Exception:
        logger.warning("Langfuse init failed -- continuing without tracing", exc_info=True)


def flush() -> None:
    if not _initialized:
        return
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:
        logger.debug("Langfuse flush failed", exc_info=True)


# --- safe re-exports: passthrough no-ops when langfuse isn't installed ----------
try:
    from langfuse import get_client, observe, propagate_attributes  # noqa: F401
except Exception:  # pragma: no cover
    from contextlib import nullcontext

    class _NoopClient:
        def __getattr__(self, _):
            return lambda *a, **k: None

    def get_client():  # type: ignore[misc]
        return _NoopClient()

    def propagate_attributes(**_):  # type: ignore[misc]
        return nullcontext()

    def observe(func=None, **_):  # type: ignore[misc]
        if callable(func):
            return func

        def _wrap(f):
            return f

        return _wrap

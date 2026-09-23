"""Utility functions for environment variable access shared by the engine.

This module centralises logic related to the beta/pre‑prod environment
switch ``AGENTRA_PREFLIGHT_DEMO``.  When the flag is set to ``"true"`` the
beta deployment is expected to provide its own DynamoDB table prefix and
internal token.  The functions return the appropriate values or ``None`` if
unset.

The helper functions are intentionally lightweight so they can be imported
from any module without causing circular dependencies.
"""

from __future__ import annotations

import os

def _is_preflight_demo() -> bool:
    """Return True when the beta mode is enabled via ``AGENTRA_PREFLIGHT_DEMO``."""
    val = os.environ.get("AGENTRA_PREFLIGHT_DEMO", "").strip().lower()
    return val in {"1", "true", "yes", "on"}

def get_dynamo_table_prefix() -> str:
    """Return the table prefix to use for DynamoDB operations."""
    if _is_preflight_demo():
        return os.environ.get("BETA_DYNAMODB_TABLE_PREFIX", "") or "betadata-"
    return os.environ.get("AGENTRA_DYNAMODB_TABLE_PREFIX", "") or ""

def get_internal_token() -> str | None:
    """Return the internal bearer token for the engine."""
    if _is_preflight_demo():
        return os.environ.get("BETA_INTERNAL_TOKEN")
    return os.environ.get("AGENTRA_INTERNAL_TOKEN")

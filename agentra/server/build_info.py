"""Process-stable deployed commit SHA shared by every endpoint that reports it."""

from __future__ import annotations

import os
from functools import lru_cache


@lru_cache(maxsize=1)
def build_commit() -> str:
    """Deployed git SHA (VERCEL_GIT_COMMIT_SHA, else AGENTRA_BUILD_SHA), resolved once per process, or "" when unknown."""
    raw = os.environ.get("VERCEL_GIT_COMMIT_SHA") or os.environ.get("AGENTRA_BUILD_SHA") or ""
    return raw.strip()


def reset_build_commit_cache() -> None:
    """Forget the memoised commit so the next call re-reads the environment (tests only)."""
    build_commit.cache_clear()

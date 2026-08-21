"""server/state.py — global process state for the FastAPI server."""

from __future__ import annotations

import asyncio
from typing import Any

# Locks per app name, keeping concurrent autonomous/debug/promote runs for the
_app_locks: dict[str, asyncio.Lock] = {}

# Process-local cache of currently executing runs. Useful for quick polling
_active_runs: dict[str, dict[str, Any]] = {}

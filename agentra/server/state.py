"""server/state.py — global process state for the FastAPI server.

Holds process-local states like active locks and running task caches.
"""

from __future__ import annotations

import asyncio
from typing import Any

# Locks per app name, keeping concurrent autonomous/debug/promote runs for the
# same app from stomping on the same git repo folder.
_app_locks: dict[str, asyncio.Lock] = {}

# Process-local cache of currently executing runs. Useful for quick polling
# of run_key states within the same process.
_active_runs: dict[str, dict[str, Any]] = {}

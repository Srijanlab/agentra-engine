"""Read-only computation of when an app is next due for a scheduled cycle."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from agentra import environments, registry

_DEFAULT_GAP_SECONDS = 60.0
_CYCLE_RUN_SOURCES = ("scheduled", "on-demand")
_IN_FLIGHT = ("queued", "running")


def continuous_gap_seconds() -> float:
    """Pause between back-to-back cycles (`AGENTRA_CONTINUOUS_GAP_SECONDS`, default 60)."""
    try:
        value = float(os.environ.get("AGENTRA_CONTINUOUS_GAP_SECONDS", ""))
    except ValueError:
        return _DEFAULT_GAP_SECONDS
    return value if value >= 0 else _DEFAULT_GAP_SECONDS


@dataclass(frozen=True)
class ScheduleStatus:
    """Snapshot of an app's scheduled-cycle cadence, timing, and queue state."""

    app: str
    cadence_hours: float
    last_scheduled_run_at: float | None
    next_due_at: float | None
    due_in_seconds: float | None
    schedule_enabled: bool
    paused: bool
    cycle_job_pending: bool
    cycle_job_claimed: bool
    open_job_pending: bool
    continuous: bool = False
    gap_seconds: float = _DEFAULT_GAP_SECONDS
    cycle_in_flight: bool = False

    @property
    def queued(self) -> bool:
        """A cycle job for this app is already pending or claimed by the loop."""
        return self.cycle_job_pending or self.cycle_job_claimed

    @property
    def due_now(self) -> bool:
        """The app is currently due for a scheduled cycle (schedule on, not paused)."""
        if self.paused or not self.schedule_enabled:
            return False
        return self.due_in_seconds is None or self.due_in_seconds <= 0

    @property
    def next_scheduled_run_at(self) -> float | None:
        """Next scheduled cycle time, or None when paused / disabled / in flight / no prior run."""
        if self.paused or not self.schedule_enabled:
            return None
        if self.continuous:
            return None if self.cycle_in_flight else self.next_due_at
        if self.last_scheduled_run_at is None:
            return None
        return self.next_due_at


def compute_schedule_status(app: str, repo: Path) -> ScheduleStatus:
    """Compute the schedule-due snapshot shared by the cron enqueue path and GET /apps/{app}/schedule."""
    env = environments.load(repo) or environments.EnvironmentConfig()
    cadence_hours = env.schedule_hours
    last = registry.last_run_at(app, source="scheduled")
    now = time.time()
    pending_jobs = registry.list_jobs(status="pending")
    claimed_jobs = registry.list_jobs(status="claimed")
    cycle_job_pending = _targets_app(pending_jobs, app, kind="cycle")
    cycle_job_claimed = _targets_app(claimed_jobs, app, kind="cycle")
    gap = continuous_gap_seconds()
    in_flight = False
    if env.schedule_continuous:
        cycle_runs = _cycle_runs(app)
        in_flight = cycle_job_pending or cycle_job_claimed or any(r.get("status") in _IN_FLIGHT for r in cycle_runs)
        due_in, next_due_at = _continuous_due(cycle_runs, in_flight, gap, now)
    else:
        due_in = None if last is None else cadence_hours * 3600 - (now - last)
        next_due_at = None if last is None else last + cadence_hours * 3600
    return ScheduleStatus(
        app=app,
        cadence_hours=cadence_hours,
        last_scheduled_run_at=last,
        next_due_at=next_due_at,
        due_in_seconds=due_in,
        schedule_enabled=cadence_hours > 0 or env.schedule_continuous,
        paused=registry.is_paused() is not None,
        cycle_job_pending=cycle_job_pending,
        cycle_job_claimed=cycle_job_claimed,
        open_job_pending=_targets_app(pending_jobs, app),
        continuous=env.schedule_continuous,
        gap_seconds=gap,
        cycle_in_flight=in_flight,
    )


def _cycle_runs(app: str) -> list[dict]:
    return [r for r in registry.list_runs(limit=200) if r.get("app") == app and r.get("source") in _CYCLE_RUN_SOURCES]


def _continuous_due(cycle_runs: list[dict], in_flight: bool, gap: float, now: float) -> tuple[float | None, float | None]:
    """(due_in_seconds, next_due_at) for a continuous app: in flight is never due, else `gap` after the last activity."""
    if in_flight:
        return max(gap, 1.0), None
    if not cycle_runs:
        return None, None
    last_activity = max(r.get("updated_at") or r.get("started_at") or 0 for r in cycle_runs)
    return gap - (now - last_activity), last_activity + gap


def _targets_app(jobs: list[dict], app: str, *, kind: str | None = None) -> bool:
    """True when any job targets `app` (optionally filtered to one job kind)."""
    return any(
        j.get("payload", {}).get("app") == app and (kind is None or j.get("kind") == kind)
        for j in jobs
    )

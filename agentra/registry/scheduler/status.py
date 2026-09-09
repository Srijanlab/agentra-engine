"""Read-only computation of when an app is next due for a scheduled cycle."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from agentra import environments, registry


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
        """Next scheduled cycle time, or None when paused / disabled / no prior scheduled run."""
        if self.paused or not self.schedule_enabled or self.last_scheduled_run_at is None:
            return None
        return self.next_due_at


def compute_schedule_status(app: str, repo: Path) -> ScheduleStatus:
    """Compute the schedule-due snapshot shared by the cron enqueue path and GET /apps/{app}/schedule."""
    env = environments.load(repo) or environments.EnvironmentConfig()
    cadence_hours = env.schedule_hours
    last = registry.last_run_at(app, source="scheduled")
    now = time.time()
    due_in = None if last is None else cadence_hours * 3600 - (now - last)
    next_due_at = None if last is None else last + cadence_hours * 3600
    pending_jobs = registry.list_jobs(status="pending")
    claimed_jobs = registry.list_jobs(status="claimed")
    return ScheduleStatus(
        app=app,
        cadence_hours=cadence_hours,
        last_scheduled_run_at=last,
        next_due_at=next_due_at,
        due_in_seconds=due_in,
        schedule_enabled=cadence_hours > 0,
        paused=registry.is_paused() is not None,
        cycle_job_pending=_targets_app(pending_jobs, app, kind="cycle"),
        cycle_job_claimed=_targets_app(claimed_jobs, app, kind="cycle"),
        open_job_pending=_targets_app(pending_jobs, app),
    )


def _targets_app(jobs: list[dict], app: str, *, kind: str | None = None) -> bool:
    """True when any job targets `app` (optionally filtered to one job kind)."""
    return any(
        j.get("payload", {}).get("app") == app and (kind is None or j.get("kind") == kind)
        for j in jobs
    )

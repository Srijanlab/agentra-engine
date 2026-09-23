"""registry/jobs.py — the work queue.

A unit of work the engine can't do itself (running a cycle, a promotion, a
prod-debug pass, a human-answer resume) is recorded here; the loop claims and
executes it on its tick, then reports the outcome. The engine never executes a
job. Local-JSON fallback (`_JOBS_PATH`) for dev and tests.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

from agentra.registry import core

JOB_KINDS = ("cycle", "promote", "prod_debug", "human_resume")
_TERMINAL = ("done", "failed")
_JOB_TTL_SECONDS = 7 * 24 * 3600  # a terminal job auto-expires (DynamoDB native TTL)
_DEFAULT_MAX_ATTEMPTS = 3

logger = logging.getLogger(__name__)


def max_job_attempts() -> int:
    """Claims a job may consume before it goes terminal-failed (`AGENTRA_JOB_MAX_ATTEMPTS`)."""
    try:
        value = int(os.environ.get("AGENTRA_JOB_MAX_ATTEMPTS", ""))
    except ValueError:
        return _DEFAULT_MAX_ATTEMPTS
    return value if value > 0 else _DEFAULT_MAX_ATTEMPTS


def enqueue_job(kind: str, payload: dict, *, dedup_key: str | None = None) -> str:
    """Record `payload` as a `kind` job for the loop to claim. Returns the
    job_id. `dedup_key`: if a non-terminal job already carries it, return that
    job's id instead of enqueuing a duplicate (e.g. one promote per app)."""
    if kind not in JOB_KINDS:
        raise ValueError(f"unknown job kind {kind!r} -- expected one of {JOB_KINDS}")
    if dedup_key:
        for job in _open_jobs():
            if job.get("dedup_key") == dedup_key:
                return job["job_id"]
    job_id = uuid.uuid4().hex[:16]
    job: dict[str, Any] = {
        "job_id": job_id,
        "kind": kind,
        "payload": payload,
        "status": "pending",
        "enqueued_at": time.time(),
        "attempts": 0,
    }
    if dedup_key:
        job["dedup_key"] = dedup_key
    _put_job(job)
    return job_id


def claim_next_job() -> dict | None:
    """Atomically claim the oldest pending job (returns it with status
    "claimed"), or None. Re-queues any claimed job whose lease (last heartbeat)
    is stale first, so a crashed loop can't strand its job; a job that has used
    up its attempts goes terminal-failed instead of being re-queued."""
    _requeue_stale_claims(time.time())
    for job in _jobs_by_status("pending"):
        if int(job.get("attempts", 0)) >= max_job_attempts():
            _fail_poison_job(job)
            continue
        now = time.time()
        if _try_claim(job["job_id"], now):
            attempts = int(job.get("attempts", 0)) + 1
            job.update(status="claimed", claimed_at=now, heartbeat_at=now, attempts=attempts)
            _write_job(job["job_id"], {"attempts": attempts})
            return job
    return None


def release_job(job_id: str, *, claimed_at: float | None = None) -> bool:
    """Release a claimed job back to pending immediately, without touching its
    attempt count. For graceful shutdown: a container about to be replaced (a
    deploy, an ECS task-definition update) releases whatever job it's mid-way
    through so the replacement container can claim and retry it within
    seconds, instead of the job sitting claimed with no owner until the lease
    ceiling reclaims it (confirmed live 2026-09-23: over an hour of nothing
    running after a task-definition update replaced the container mid-cycle).

    `claimed_at`, when given, must match the job's current claimed_at or this
    is a no-op -- a compare-and-swap guard against a late release call landing
    after the lease ceiling already reclaimed this job and a different worker
    has since started a fresh attempt on it (that attempt's own claimed_at
    won't match, so this correctly leaves it alone instead of stomping it back
    to pending out from under the new owner).

    No-op (returns False) if the job is already terminal, already reclaimed
    (by lease or by this guard), or unknown."""
    job = _get_job(job_id)
    if job is None or job.get("status") != "claimed":
        return False
    if claimed_at is not None and job.get("claimed_at") != claimed_at:
        return False
    _write_job(job_id, {"status": "pending", "claimed_at": None, "heartbeat_at": None})
    return True


def touch_job(job_id: str, run_key: str | None = None) -> bool:
    """Renew a claimed job's lease; False (never raises) once it is unknown, terminal, or re-queued."""
    now = time.time()
    job = _get_job(job_id)
    if job is None or job.get("status") != "claimed":
        return False
    if not _try_touch(job_id, now):
        return False
    payload_key = (job.get("payload") or {}).get("run_key")
    if run_key and run_key != payload_key:
        logger.debug("touch_job %s: ignoring supplied run_key that differs from the job's own", job_id)
    if payload_key:
        from agentra.registry import runs

        if runs.get_run(payload_key) is not None:
            runs.record_run(payload_key, updated_at=now)
    return True


def _requeue_stale_claims(now: float) -> None:
    """Re-queue stale claimed jobs based on app heartbeat ceiling.\n    Jobs are considered stale if the time since the most recent heartbeat\n    for their app exceeds the configured stale heartbeat threshold."""
    threshold = core.stale_heartbeat_seconds()
    # Build max heartbeat per app across all claimed jobs
    app_max_heartbeat: dict[str, float] = {}
    for job in _jobs_by_status("claimed"):
        heartbeat = float(job.get("heartbeat_at") or job.get("claimed_at") or 0)
        app = job.get("payload", {}).get("app") or ""
        current = app_max_heartbeat.get(app, 0)
        if heartbeat > current:
            app_max_heartbeat[app] = heartbeat
    # Now re-evaluate each job using its app's max heartbeat
    for job in _jobs_by_status("claimed"):
        app = job.get("payload", {}).get("app") or ""
        max_hb = app_max_heartbeat.get(app, 0)
        if now - max_hb <= threshold:
            continue
        if int(job.get("attempts", 0)) >= max_job_attempts():
            _fail_poison_job(job)
        else:
            _conditional_write(
                job["job_id"], {"status": "pending", "claimed_at": None, "heartbeat_at": None}, expect_status="claimed"
            )


def _fail_poison_job(job: dict) -> None:
    """Terminal-fail a job that exhausted its attempts and raise a human gate for its run."""
    job_id, attempts = job["job_id"], int(job.get("attempts", 0))
    error = (
        f"HUMAN_INPUT_REQUIRED: {job.get('kind')} job {job_id} was claimed {attempts} times without "
        "completing -- the loop keeps dying or stalling on it"
    )
    report_job(job_id, "failed", {"error": error})
    payload = job.get("payload") or {}
    try:
        if payload.get("run_key"):
            from agentra.registry import runs

            runs.record_run(payload["run_key"], status="failed", error=error)
        _notify_poison_job(payload, error)
    except Exception:
        logger.warning("poison-job gate failed for job %s", job_id, exc_info=True)


def _notify_poison_job(payload: dict, error: str) -> None:
    from agentra.connectors import slack

    app = payload.get("app") or "unknown"
    slack.notify_human_input_required(
        app=app, run_id=payload.get("run_key") or "", question=error, channel=core.get_slack_channel(app),
    )


def report_job(job_id: str, status: str, result: dict | None = None) -> None:
    """Record a job's terminal outcome. Idempotent."""
    if status not in _TERMINAL:
        raise ValueError(f"job status must be one of {_TERMINAL}, got {status!r}")
    _write_job(job_id, {
        "status": status,
        "result": result or {},
        "reported_at": time.time(),
        "expires_at": int(time.time() + _JOB_TTL_SECONDS),
    })


def list_jobs(status: str | None = None, limit: int = 50) -> list[dict]:
    jobs = _all_jobs()
    if status is not None:
        jobs = [j for j in jobs if j.get("status") == status]
    jobs.sort(key=lambda j: j.get("enqueued_at") or 0, reverse=True)
    return jobs[:limit]


def _open_jobs() -> list[dict]:
    return [j for j in _all_jobs() if j.get("status") not in _TERMINAL]


# --- storage: DynamoDB (`agentra-jobs`) with a local-JSON fallback ------------

def _all_jobs() -> list[dict]:
    if core._ddb is not None:
        from agentra.registry import _dynamo

        return _dynamo.scan_all(_dynamo.table("jobs"))
    return list(_local_jobs().values())


def _jobs_by_status(status: str) -> list[dict]:
    """Oldest first -- the claim order."""
    if core._ddb is not None:
        from boto3.dynamodb.conditions import Key

        from agentra.registry import _dynamo

        resp = _dynamo.table("jobs").query(
            IndexName="by-status", KeyConditionExpression=Key("status").eq(status), ScanIndexForward=True
        )
        return [_dynamo.from_item(i) for i in resp.get("Items", [])]
    return sorted(
        (j for j in _local_jobs().values() if j.get("status") == status),
        key=lambda j: j.get("enqueued_at") or 0,
    )


def _put_job(job: dict) -> None:
    if core._ddb is not None:
        from agentra.registry import _dynamo

        _dynamo.put_item(_dynamo.table("jobs"), job)
        return
    jobs = _local_jobs()
    jobs[job["job_id"]] = job
    _local_save(jobs)


def _write_job(job_id: str, fields: dict) -> None:
    if core._ddb is not None:
        from agentra.registry import _dynamo

        _dynamo.merge_update(_dynamo.table("jobs"), {"job_id": job_id}, fields)
        return
    jobs = _local_jobs()
    if job_id in jobs:
        jobs[job_id].update(fields)
        _local_save(jobs)


def _get_job(job_id: str) -> dict | None:
    if core._ddb is not None:
        from agentra.registry import _dynamo

        return _dynamo.get_item(_dynamo.table("jobs"), {"job_id": job_id})
    return _local_jobs().get(job_id)


def _conditional_write(job_id: str, fields: dict, *, expect_status: str) -> bool:
    """CAS on status: apply `fields` only while the job is in `expect_status`."""
    if core._ddb is not None:
        from agentra.registry import _dynamo

        return _dynamo.try_conditional_update(
            _dynamo.table("jobs"), {"job_id": job_id}, fields,
            condition_attr="status", condition_value=expect_status,
        )
    jobs = _local_jobs()
    job = jobs.get(job_id)
    if job is None or job.get("status") != expect_status:
        return False
    job.update(fields)
    _local_save(jobs)
    return True


def _try_claim(job_id: str, now: float | None = None) -> bool:
    """CAS pending -> claimed. False (not an error) if another claimer won."""
    now = time.time() if now is None else now
    return _conditional_write(
        job_id, {"status": "claimed", "claimed_at": now, "heartbeat_at": now}, expect_status="pending"
    )


def _try_touch(job_id: str, now: float) -> bool:
    """Stamp `heartbeat_at` only while the job is still claimed."""
    return _conditional_write(job_id, {"heartbeat_at": now}, expect_status="claimed")


def _local_jobs() -> dict[str, dict]:
    if not core._JOBS_PATH.exists():
        return {}
    try:
        return json.loads(core._JOBS_PATH.read_text())
    except (ValueError, OSError):
        return {}


def _local_save(jobs: dict) -> None:
    core._JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    core._JOBS_PATH.write_text(json.dumps(jobs, indent=2))

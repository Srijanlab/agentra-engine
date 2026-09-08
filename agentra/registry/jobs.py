"""registry/jobs.py — the work queue.

A unit of work the engine can't do itself (running a cycle, a promotion, a
prod-debug pass, a human-answer resume) is recorded here; the loop claims and
executes it on its tick, then reports the outcome. The engine never executes a
job. Local-JSON fallback (`_JOBS_PATH`) for dev and tests.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from agentra.registry import core

JOB_KINDS = ("cycle", "promote", "prod_debug", "human_resume")
_TERMINAL = ("done", "failed")
_JOB_TTL_SECONDS = 7 * 24 * 3600  # a terminal job auto-expires (DynamoDB native TTL)


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
    "claimed"), or None. Re-queues any job stuck in "claimed" past the stale
    window first, so a crashed loop can't strand its job."""
    now = time.time()
    for job in _jobs_by_status("claimed"):
        if now - float(job.get("claimed_at") or 0) > core.STALE_PROCESSING_SECONDS:
            _write_job(job["job_id"], {"status": "pending", "claimed_at": None})
    for job in _jobs_by_status("pending"):
        if _try_claim(job["job_id"]):
            job.update(status="claimed", claimed_at=now, attempts=int(job.get("attempts", 0)) + 1)
            _write_job(job["job_id"], {"claimed_at": now, "attempts": job["attempts"]})
            return job
    return None


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


def _try_claim(job_id: str) -> bool:
    """CAS pending -> claimed. False (not an error) if another claimer won."""
    if core._ddb is not None:
        from agentra.registry import _dynamo

        return _dynamo.try_conditional_update(
            _dynamo.table("jobs"), {"job_id": job_id},
            {"status": "claimed", "claimed_at": time.time()},
            condition_attr="status", condition_value="pending",
        )
    jobs = _local_jobs()
    job = jobs.get(job_id)
    if job is None or job.get("status") != "pending":
        return False
    job["status"] = "claimed"
    _local_save(jobs)
    return True


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

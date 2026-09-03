"""registry/runs.py — durable run and agent-step records."""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any

from agentra.registry import _cache, core

logger = logging.getLogger(__name__)


def record_run(run_key: str, **fields: Any) -> None:
    _cache.clear()  # runs/loops summaries all shift
    if core._db is not None:
        core._db.collection("runs").document(run_key).set(fields, merge=True)
        return
    runs = _local_runs()
    runs.setdefault(run_key, {}).update(fields)
    _local_save_runs(runs)


def get_run(run_key: str) -> dict | None:
    if core._db is not None:
        return _cache.get_or_set(
            f"run:{run_key}",
            lambda: (lambda d: d.to_dict() if d.exists else None)(
                core._db.collection("runs").document(run_key).get()
            ),
            ttl=6,
        )
    return _local_runs().get(run_key)


def _stream_agent_steps(fetch_limit: int) -> list[dict]:
    from google.cloud import firestore

    docs = (
        core._db.collection("agent_steps")
        .order_by("ts", direction=firestore.Query.DESCENDING)
        .limit(fetch_limit)
        .stream()
    )
    return [d.to_dict() for d in docs]


def _stream_runs(limit: int) -> list[dict]:
    from google.cloud import firestore

    docs = (
        core._db.collection("runs")
        .order_by("started_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [{"run_key": d.id, **d.to_dict()} for d in docs]


def list_runs(limit: int = 50) -> list[dict]:
    if core._db is not None:
        return _cache.get_or_set(f"runs:{limit}", lambda: _stream_runs(limit), ttl=8)

    runs = _local_runs()
    ordered = sorted(
        ({"run_key": key, **info} for key, info in runs.items()),
        key=lambda r: r["started_at"],
        reverse=True,
    )
    return ordered[:limit]


def loop_id_for(objective: str) -> str:
    import hashlib

    return hashlib.sha1(objective.encode("utf-8")).hexdigest()[:10]


def loop_id_for_issue(app: str, issue_number: int | str) -> str:
    """A loop maps 1:1 to a tracked GitHub issue -- every run that works that issue
    (implement, resume-after-human, deploy, verify) shares this id. Falls back to
    loop_id_for(objective) at dispatch time, before the run has picked an issue."""
    import hashlib

    return hashlib.sha1(f"{app}#{issue_number}".encode("utf-8")).hexdigest()[:10]


def list_loops(app: str | None = None) -> list[dict]:
    """Group runs by loop_id and return loop summaries, newest loop first."""
    runs = [r for r in list_runs(limit=1000) if (app is None or r.get("app") == app) and r.get("loop_id")]
    steps = list_agent_steps(app=app, limit=2000)
    steps_by_run_key: dict[str, list[dict]] = {}
    for s in steps:
        steps_by_run_key.setdefault(s.get("run_id"), []).append(s)

    loops: dict[tuple[str, str], dict] = {}
    for run in runs:
        key = (run.get("app"), run["loop_id"])
        loop = loops.setdefault(
            key, {"loop_id": run["loop_id"], "app": run.get("app"), "objective": run.get("objective"), "runs": []}
        )
        loop["runs"].append(run)

    result = []
    for loop in loops.values():
        loop["runs"].sort(key=lambda r: r["started_at"], reverse=True)
        loop["started_at"] = min(r["started_at"] for r in loop["runs"])
        # A loop maps to one tracked issue/feature; surface its title from
        # whichever run recorded it (run.feature or run.result.feature).
        loop["title"] = next(
            (r.get("feature") or (r.get("result") or {}).get("feature") for r in loop["runs"]
             if r.get("feature") or (r.get("result") or {}).get("feature")),
            None,
        )
        # The tracked GitHub issue number, when a run in this loop recorded one
        # (implement_feature stamps it alongside loop_id) -- lets the dashboard
        # show the issue's own title/link instead of the long auto-generated brief.
        loop["issue_number"] = next(
            (str(r["issue_number"]) for r in loop["runs"] if r.get("issue_number")),
            None,
        )
        loop["cost_usd"] = sum((r.get("result") or {}).get("cost_usd") or 0 for r in loop["runs"])
        loop["cycles_completed"] = sum(1 for r in loop["runs"] if r.get("status") == "completed")
        loop["cycles_total"] = len(loop["runs"])

        agent_totals: dict[str, dict] = {}
        for r in loop["runs"]:
            for s in steps_by_run_key.get(r["run_key"], []):
                bucket = agent_totals.setdefault(s["agent"], {"agent": s["agent"], "count": 0, "cost_usd": 0.0})
                bucket["count"] += 1
                bucket["cost_usd"] += s.get("cost_usd") or 0
        loop["agents"] = sorted(agent_totals.values(), key=lambda a: -a["cost_usd"])
        result.append(loop)

    result.sort(key=lambda l: l["started_at"], reverse=True)
    return result


def last_run_at(app: str, source: str | None = None) -> float | None:
    matches = [r for r in list_runs(limit=200) if r.get("app") == app and (source is None or r.get("source") == source)]
    if not matches:
        return None
    return max(r["started_at"] for r in matches)


def reconcile_stale_runs() -> list[str]:
    now = time.time()
    steps_by_run: dict[str, float] = {}
    for step in list_agent_steps(limit=500):
        run_id = step.get("run_id")
        if not run_id:
            continue
        try:
            ts = dt.datetime.fromisoformat(step["ts"]).timestamp()
        except (KeyError, ValueError):
            continue
        if run_id not in steps_by_run or ts > steps_by_run[run_id]:
            steps_by_run[run_id] = ts

    marked: list[str] = []
    for run in list_runs(limit=200):
        if run.get("status") not in ("queued", "running"):
            continue
        run_key = run["run_key"]
        last_activity = steps_by_run.get(run_key, run.get("started_at", now))
        if now - last_activity > core.STALE_PROCESSING_SECONDS:
            record_run(
                run_key,
                status="failed",
                error=f"orphaned: no activity for over {core.STALE_PROCESSING_SECONDS // 60} minutes -- "
                "the process running this cycle likely died (e.g. an OOM kill or revision rollout)",
            )
            marked.append(run_key)
    return marked


def list_waiting_for_human(limit: int = 200) -> list[dict]:
    """Runs currently parked in the 'waiting_for_human' state -- backs the dashboard's 'Needs your input' panel."""
    return [r for r in list_runs(limit=limit) if r.get("status") in ("waiting_for_human", "escalated")]


def reconcile_waiting_for_human() -> list[dict]:
    """Human-in-the-loop escalation (GitHub issue #34): a run sitting in 'waiting_for_human' must never silently stay there forever with no further signal -- past core.HUMAN_INPUT_MAX_WAIT_SECONDS since it started waiting, flip it to the distinguishable 'escalated' state so a human looking at the dashboard (or a re-sent Slack message, dispatched by the caller using the human_input context this returns) can tell "still within normal wait" from "this has been sitting here too long."  Pure state transition only, no outbound calls (GitHub/Slack) -- keeps registry/ dependency-free of connectors/, same layering as the rest of this module."""
    now = time.time()
    escalated: list[dict] = []
    for run in list_runs(limit=500):
        if run.get("status") != "waiting_for_human":
            continue
        human_input = run.get("human_input") or {}
        waiting_since = human_input.get("waiting_since")
        if waiting_since is None or now - waiting_since <= core.HUMAN_INPUT_MAX_WAIT_SECONDS:
            continue
        record_run(run["run_key"], status="escalated")
        run["status"] = "escalated"
        escalated.append(run)
    return escalated


def record_agent_step(
    app: str, run_id: str, agent: str, ok: bool | None, cost_usd: float, turns: int | None, summary: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
) -> None:
    """Token fields (GitHub issue #74) are optional/keyword-only so existing
    callers that only know cost_usd/turns (e.g. orchestrator.py's fixed
    pipeline) don't need updating -- None means "not reported for this
    step", not "zero tokens used"."""
    record = {
        "app": app, "run_id": run_id, "agent": agent, "ok": ok,
        "cost_usd": cost_usd, "turns": turns, "summary": summary,
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
    }
    _cache.clear()
    if core._db is not None:
        core._db.collection("agent_steps").add(record)
        return
    core._AGENT_STEPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with core._AGENT_STEPS_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


def list_agent_steps(app: str | None = None, limit: int = 100) -> list[dict]:
    fetch_limit = limit * 5 if app is not None else limit

    if core._db is not None:
        steps = _cache.get_or_set(f"steps:{fetch_limit}", lambda: _stream_agent_steps(fetch_limit), ttl=10)
    else:
        if not core._AGENT_STEPS_PATH.exists():
            return []
        steps = [json.loads(line) for line in core._AGENT_STEPS_PATH.read_text().splitlines()]
        steps.sort(key=lambda s: s["ts"], reverse=True)
        steps = steps[:fetch_limit]

    if app is not None:
        steps = [s for s in steps if s.get("app") == app]
    return steps[:limit]


def _local_runs() -> dict[str, dict]:
    if not core._RUNS_PATH.exists():
        return {}
    return json.loads(core._RUNS_PATH.read_text())


def _local_save_runs(runs: dict[str, dict]) -> None:
    core._RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    core._RUNS_PATH.write_text(json.dumps(runs, indent=2))

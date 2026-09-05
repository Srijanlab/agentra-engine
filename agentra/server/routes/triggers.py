"""server/routes/triggers.py — schedule, alarm, Pub/Sub queue, and promote runners."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from agentra import environments, registry
from agentra.agents.brain import run_autonomous_cycle
from agentra.memory import Memory
from agentra.orchestrator import run_prod_debug_cycle, run_promote
from agentra.server.routes.human_input import dispatch_human_answer
from agentra.server.state import _active_runs
from agentra.server.utils import _lock_for, _paused_response, _server_log, _set_run

logger = logging.getLogger(__name__)

router = APIRouter()


class ScheduledTrigger(BaseModel):
    app: str | None = None
    objective: str | None = None
    feature: str | None = None
    skip_deploy: bool = False


class AlarmTrigger(BaseModel):
    app: str
    symptom: str | None = None
    objective: str | None = None


class PromoteTrigger(BaseModel):
    target_repo: str | None = None


def _branch_head_sha(repo: Path, branch: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", branch],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _record_production_release(repo: Path, run_id: str, code_repo: Path | None = None) -> list[str]:
    """`repo` is always the coordination repo -- Memory (the released.json ledger,
    status:done labels) always operates there. `code_repo` is the repo that was actually
    promoted (defaults to `repo`, i.e. a legacy single-repo app where they're the same);
    its own prod_branch is what actually moved, and what persist_audit_trail's dirty-check
    against `repo` needs to be told about is `repo`'s own branch, not code_repo's."""
    from agentra.agents import deployment

    code_repo = code_repo or repo
    env = environments.load(code_repo) or environments.EnvironmentConfig()
    mem = Memory(repo)
    prod_sha = sys.modules.get("agentra.server", None)
    if prod_sha is not None:
        _sha_fn = getattr(prod_sha, "_branch_head_sha", _branch_head_sha)
    else:
        _sha_fn = _branch_head_sha
    prod_sha = _sha_fn(code_repo, env.prod_branch)
    newly_released: list[str] = []

    for feature in mem.pending_promotion_features():
        name = feature["feature"]
        mem.record_released(name, release_run_id=run_id, commit_sha=prod_sha or feature.get("commit_sha"))
        newly_released.append(name)

    # Every shipped feature not yet marked status:done -- not just the newly-released ones
    # above. A feature already sitting in the local released.json ledger from a prior run (its
    # promotion happened before this label existed, or a previous status write failed) still
    # needs its GitHub label caught up here, same reasoning as the closed_bugs() sweep below.
    for feature in mem.shipped_features():
        if not feature.get("status_done") and feature.get("external_id", "").isdigit():
            mem.mark_status_done(int(feature["external_id"]))

    for bug in mem.closed_bugs():
        if not bug.get("status_done") and bug.get("external_id", "").isdigit():
            mem.mark_status_done(int(bug["external_id"]))

    if newly_released:
        coord_env = environments.load(repo) or environments.EnvironmentConfig()
        persist_error = deployment.persist_audit_trail(repo, coord_env.prod_branch)
        if persist_error:
            _server_log("promote", f"release ledger persisted locally but push failed: {persist_error}")
    return newly_released


def _new_run_key(app_name: str, source: str, objective: str, feature: str | None = None) -> str:
    run_key = uuid.uuid4().hex[:8]
    # Every run gets a loop entry from the very start (objective-keyed). implement_feature
    # later calls bind_loop (issue-keyed) and repoints the run once it commits to an issue;
    # runs that never do keep this objective-level anchor. Mirrors agentra-loop's _new_run_key.
    loop_id = registry.bind_loop_for_run(app_name, objective)
    _active_runs[run_key] = {
        "app": app_name,
        "source": source,
        "status": "queued",
        "started_at": time.time(),
        "objective": objective,
        "feature": feature,
        "loop_id": loop_id,
    }
    registry.record_run(run_key, **_active_runs[run_key])
    return run_key


async def _run_autonomous_background(
    run_key: str, app_name: str, repo: Path, objective: str, feature: str | None, skip_deploy: bool
) -> None:
    lock = _lock_for(app_name)
    async with lock:
        _set_run(run_key, status="running")
        try:
            env = environments.load(repo) or environments.EnvironmentConfig()
            report = await run_autonomous_cycle(
                repo, objective, env, feature=feature, skip_deploy=skip_deploy, run_id=run_key, app_name=app_name
            )
            # Human-in-the-loop escalation (GitHub issue #34): if the cycle
            _set_run(
                run_key,
                status="waiting_for_human" if report.waiting_for_human else "completed",
                ended_at=time.time(),
                cost_usd=report.cost_usd,
                summary=report.final_message,
                feature=report.feature,
            )
            _server_log(
                _active_runs[run_key]["source"],
                f"app={app_name!r} run_key={run_key} agentra_run_id={report.run_id} completed | cost=${report.cost_usd:.4f}",
            )
        except Exception as exc:
            _set_run(run_key, status="failed", ended_at=time.time(), error=str(exc), summary=str(exc)[:2000])
            _server_log(_active_runs[run_key]["source"], f"app={app_name!r} run_key={run_key} raised: {exc!r}")


async def _run_prod_debug_background(
    run_key: str, app_name: str, repo: Path, objective: str, symptom: str | None
) -> None:
    from agentra.agents import deployment

    lock = _lock_for(app_name)
    async with lock:
        _set_run(run_key, status="running")
        try:
            report = await run_prod_debug_cycle(repo, objective, symptom=symptom, run_id=run_key)
            _set_run(
                run_key,
                status="completed",
                result={
                    "run_id": report.run_id,
                    "root_cause_found": report.root_cause_found,
                    "body": report.body if hasattr(report, "body") else "",
                    "severity": report.severity,
                    "fix_attempted": report.fix_attempted,
                    "promoted_to_prod": report.promoted_to_prod,
                },
            )
            # GitHub issue #92: strictly after the status write above has durably
            # landed -- never before, and never gated on it -- so an outgoing
            # self-hosted prod container (which may be this very process) only
            # gets torn down once this run can no longer be left stuck at "running".
            deployment.trigger_deferred_teardown(getattr(report, "teardown", None))
            _server_log(
                "alarm",
                f"app={app_name!r} run_key={run_key} agentra_run_id={report.run_id} "
                f"root_cause_found={report.root_cause_found} promoted_to_prod={report.promoted_to_prod}",
            )
        except Exception as exc:
            _set_run(run_key, status="failed", error=str(exc))
            _server_log("alarm", f"app={app_name!r} run_key={run_key} raised: {exc!r}")


async def _run_promote_background(run_key: str, app_name: str, repo: Path, code_repo: Path | None = None) -> None:
    from agentra.agents import deployment

    lock = _lock_for(app_name)
    async with lock:
        _set_run(run_key, status="running")
        try:
            result = await run_promote(repo, run_id=run_key, code_repo=code_repo)
            released_features: list[str] = []
            if result["ok"]:
                released_features = _record_production_release(repo, run_key, code_repo=code_repo)
            _set_run(
                run_key,
                status="completed" if result["ok"] else "failed",
                result={
                    "run_id": result["run_id"],
                    "promoted": result["ok"],
                    "released_features": released_features,
                    "released_count": len(released_features),
                    "final_message": result["text"][:2000],
                },
            )
            # GitHub issue #92: this must be the very last thing done for this run,
            # strictly after the status write above -- _record_production_release
            # above can take arbitrarily long (one GitHub API call per pending
            # feature/bug) without risking the outgoing self-hosted container (which
            # may be running this very process) getting SIGKILLed before its status
            # is durably recorded.
            deployment.trigger_deferred_teardown(result.get("teardown"))
            _server_log(
                "promote",
                f"app={app_name!r} run_key={run_key} promoted={result['ok']} released={len(released_features)}",
            )
        except Exception as exc:
            _set_run(run_key, status="failed", error=str(exc))
            _server_log("promote", f"app={app_name!r} run_key={run_key} raised: {exc!r}")


async def _dispatch_cycle(
    app_name: str, source: str, objective_override: str | None, feature: str | None, skip_deploy: bool, enforce_schedule: bool
) -> dict:
    if registry.is_paused():
        return _paused_response(source)

    repo = registry.get_app_repo(app_name)
    if repo is None:
        _server_log(source, f"app={app_name!r} not registered -- no-op")
        return {"triggered": False, "reason": f"app {app_name!r} not registered"}

    if _lock_for(app_name).locked():
        _server_log(source, f"app={app_name!r} already has a cycle running -- skipped")
        return {"triggered": False, "reason": "a cycle for this app is already running"}

    objective = objective_override or Memory(repo).get_objective()
    if not objective:
        _server_log(source, f"app={app_name!r} has no objective set -- no-op")
        return {"triggered": False, "reason": "no objective set for this app"}

    if enforce_schedule:
        env = environments.load(repo) or environments.EnvironmentConfig()
        last = registry.last_run_at(app_name, source="scheduled")
        due_in = None if last is None else env.schedule_hours * 3600 - (time.time() - last)
        if due_in is not None and due_in > 0:
            _server_log(source, f"app={app_name!r} not due for {due_in / 3600:.1f}h more (schedule_hours={env.schedule_hours}) -- skipped")
            return {"triggered": False, "reason": "not due yet per this app's configured schedule"}

    run_key = _new_run_key(app_name, source, objective, feature=feature)
    # The engine (cloud mode, read-only fs, 30s functions) records the run but
    # can't execute a cycle -- the loop drains queued runs on its tick.
    if registry.cloud_mode():
        registry.record_run(run_key, skip_deploy=skip_deploy)
        _server_log(source, f"app={app_name!r} run_key={run_key} -- queued for the loop")
        return {"triggered": True, "run_key": run_key, "queued": True}
    _server_log(source, f"app={app_name!r} run_key={run_key} objective={objective!r} -- dispatched")
    asyncio.create_task(_run_autonomous_background(run_key, app_name, repo, objective, feature, skip_deploy))
    return {"triggered": True, "run_key": run_key}


async def _drain_queued_runs() -> None:
    """On-demand runs the engine queued (it can't run cycles itself). The loop
    claims and executes them on its scheduled tick."""
    if registry.cloud_mode():
        return  # only the loop drains
    for run in registry.list_runs(limit=40):
        run_key = run.get("run_key")
        if run.get("status") != "queued" or not run_key or run_key in _active_runs:
            continue
        if _lock_for(run.get("app") or "").locked():
            continue
        repo = registry.get_app_repo(run.get("app") or "")
        if repo is None:
            continue
        objective = run.get("objective") or Memory(repo).get_objective()
        if not objective:
            continue
        _server_log("scheduled", f"draining queued run {run_key} for app={run.get('app')!r}")
        asyncio.create_task(_run_autonomous_background(
            run_key, run["app"], repo, objective, run.get("feature"), bool(run.get("skip_deploy")),
        ))


def _reconcile_human_input_for_app(app_name: str) -> None:
    """Human-in-the-loop escalation (GitHub issue #34): the polling-based half of the GitHub-issue-comment answer channel -- there is no inbound Slack/GitHub webhook (see connectors/slack.py's module docstring and design.md), so a comment posted on a needs_human issue only gets noticed here, on the next /trigger/scheduled tick."""
    repo = registry.get_app_repo(app_name)
    if repo is None:
        return
    mem = Memory(repo)
    for run in registry.list_waiting_for_human():
        if run.get("app") != app_name or run.get("status") not in ("waiting_for_human", "escalated"):
            continue
        issue_number = (run.get("human_input") or {}).get("issue_number")
        if issue_number is None:
            continue
        try:
            answer = mem.find_unanswered_human_input_comment(issue_number)
            if not answer:
                continue
            dispatch_human_answer(app_name, repo, issue_number, answer, source="github-comment")
            _server_log(
                "scheduled",
                f"app={app_name!r} issue=#{issue_number} -- human answered via a GitHub comment, resuming",
            )
        except Exception:
            logger.warning("_reconcile_human_input_for_app: failed for app=%r issue=#%s", app_name, issue_number, exc_info=True)


def _reconcile_human_input_timeouts() -> None:
    """The other half of GitHub issue #34's max-wait handling: a run must never sit in waiting_for_human forever with no further signal."""
    try:
        escalated = registry.reconcile_waiting_for_human()
    except Exception:
        logger.warning("_reconcile_human_input_timeouts: reconcile_waiting_for_human failed", exc_info=True)
        return
    if not escalated:
        return
    from agentra import urls
    from agentra.connectors import slack

    for run in escalated:
        human_input = run.get("human_input") or {}
        slack.notify_human_input_required(
            app=run.get("app") or "",
            run_id=run.get("run_key") or "",
            question=human_input.get("question") or "(question unavailable)",
            issue_url=human_input.get("issue_url"),
            dashboard_url=urls.dashboard_run_url(run.get("run_key") or "", run.get("app") or ""),
            branch=human_input.get("branch"),
            session_id=human_input.get("session_id"),
            escalated=True,
        )
        _server_log("scheduled", f"app={run.get('app')!r} run_key={run.get('run_key')} -- waiting_for_human past max-wait, escalated")


@router.post("/trigger/scheduled")
async def trigger_scheduled(payload: ScheduledTrigger) -> dict:
    if payload.app is None:
        await _drain_queued_runs()
        results = {}
        for app_name in registry.list_apps():
            try:
                _reconcile_human_input_for_app(app_name)
            except Exception:
                logger.warning("trigger_scheduled: human-input reconciliation failed for app=%r", app_name, exc_info=True)
            results[app_name] = await _dispatch_cycle(
                app_name, "scheduled", None, None, False, enforce_schedule=True
            )
        _reconcile_human_input_timeouts()
        return {"apps": results}
    return await _dispatch_cycle(
        payload.app, "scheduled", payload.objective, payload.feature, payload.skip_deploy, enforce_schedule=True
    )


@router.post("/apps/{app_name}/run")
async def run_app_now(app_name: str, payload: ScheduledTrigger | None = None) -> dict:
    body = payload or ScheduledTrigger(app=app_name)
    return await _dispatch_cycle(
        app_name, "on-demand", body.objective, body.feature, body.skip_deploy, enforce_schedule=False
    )


@router.post("/apps/{app_name}/promote")
async def promote_app(app_name: str, payload: PromoteTrigger | None = None) -> dict:
    if app_name not in registry.list_apps():
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")
    if registry.is_paused():
        return _paused_response("promote")

    repo = registry.get_app_repo(app_name)
    if repo is None:
        raise HTTPException(status_code=409, detail=f"local checkout for {app_name!r} is missing and could not be recovered")

    code_repos = registry.get_code_repos(app_name)
    target_repo = (payload or PromoteTrigger()).target_repo
    code_repo: Path | None = None
    if code_repos:
        if not target_repo and len(code_repos) == 1:
            target_repo = next(iter(code_repos))
        if not target_repo:
            raise HTTPException(
                status_code=400,
                detail=f"app {app_name!r} has multiple code repos ({', '.join(code_repos)}) -- set target_repo.",
            )
        if target_repo not in code_repos:
            raise HTTPException(
                status_code=400,
                detail=f"target_repo={target_repo!r} is not one of {app_name!r}'s code repos: {', '.join(code_repos)}.",
            )
        code_repo = code_repos[target_repo].path
        if code_repo is None:
            raise HTTPException(status_code=409, detail=f"local checkout for {app_name!r}'s {target_repo!r} repo is missing and could not be recovered")

    if _lock_for(app_name).locked():
        _server_log("promote", f"app={app_name!r} already has a cycle running -- skipped")
        return {"triggered": False, "reason": "a cycle for this app is already running"}

    objective = Memory(repo).get_objective() or ""
    run_key = _new_run_key(app_name, "promote", objective)
    _server_log(
        "promote",
        f"app={app_name!r} run_key={run_key} target_repo={target_repo!r} -- promotion to prod dispatched",
    )
    asyncio.create_task(_run_promote_background(run_key, app_name, repo, code_repo=code_repo))
    return {"triggered": True, "run_key": run_key}


def _verify_alarm_webhook_auth(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("ALARM_WEBHOOK_PASSWORD")
    if not expected:
        return
    if authorization is None or not authorization.startswith("Basic "):
        raise HTTPException(status_code=401, detail="missing Basic auth")
    try:
        decoded = base64.b64decode(authorization.removeprefix("Basic ")).decode("utf-8")
        _username, _, password = decoded.partition(":")
    except Exception:
        raise HTTPException(status_code=401, detail="malformed Basic auth")
    if not hmac.compare_digest(password, expected):
        raise HTTPException(status_code=401, detail="invalid credentials")


@router.post("/trigger/alarm", dependencies=[Depends(_verify_alarm_webhook_auth)])
async def trigger_alarm(payload: dict) -> dict:
    if registry.is_paused():
        return _paused_response("alarm")

    incident = payload.get("incident")
    if incident is not None:
        app_name = payload.get("app")
        symptom = incident.get("summary") or incident.get("documentation", {}).get("content")
        if not app_name:
            doc_content = (incident.get("documentation") or {}).get("content", "")
            try:
                app_name = json.loads(doc_content).get("app")
            except (json.JSONDecodeError, AttributeError):
                app_name = None
        if not app_name:
            _server_log("alarm", "incident payload had no resolvable app name -- no-op")
            return {"triggered": False, "reason": "could not resolve app from incident payload"}
    else:
        parsed = AlarmTrigger.model_validate(payload)
        app_name = parsed.app
        symptom = parsed.symptom

    repo = registry.get_app_repo(app_name)
    if repo is None:
        _server_log("alarm", f"app={app_name!r} not registered -- no-op")
        return {"triggered": False, "reason": f"app {app_name!r} not registered"}

    env = environments.load(repo) or environments.EnvironmentConfig()
    if not env.alarm_enabled:
        _server_log("alarm", f"app={app_name!r} has alarms disabled -- no-op")
        return {"triggered": False, "reason": "alarms disabled for this app"}

    if _lock_for(app_name).locked():
        _server_log("alarm", f"app={app_name!r} already has a cycle running -- skipped")
        return {"triggered": False, "reason": "a cycle for this app is already running"}

    objective = (payload.get("objective") if incident is None else None) or Memory(repo).get_objective()
    if not objective:
        _server_log("alarm", f"app={app_name!r} has no objective set -- no-op")
        return {"triggered": False, "reason": "no objective set for this app"}

    run_key = _new_run_key(app_name, "alarm", objective)
    _server_log("alarm", f"app={app_name!r} run_key={run_key} symptom={symptom!r} -- dispatched to prod-debug")
    asyncio.create_task(_run_prod_debug_background(run_key, app_name, repo, objective, symptom))
    return {"triggered": True, "run_key": run_key}


@router.post("/trigger/queue")
async def trigger_queue(envelope: dict) -> dict:
    if registry.is_paused():
        _server_log("queue", "system is paused -- acking without processing")
        return {"processed": False, "reason": "system is paused"}

    message = envelope.get("message")
    if not message or "data" not in message:
        _server_log("queue", f"malformed push envelope, acking without processing: {envelope!r}")
        return {"processed": False, "reason": "malformed Pub/Sub envelope"}

    try:
        raw = base64.b64decode(message["data"]).decode("utf-8")
        request = json.loads(raw)
    except Exception as exc:
        _server_log("queue", f"could not decode message data, acking without processing: {exc!r}")
        return {"processed": False, "reason": f"could not decode message: {exc}"}

    try:
        request_id = registry.submit_request(
            app=request["app"],
            request_type=request["type"],
            title=request.get("title"),
            description=request["description"],
            severity=request.get("severity"),
            screenshot_url=request.get("screenshot_url"),
        )
    except (KeyError, ValueError) as exc:
        _server_log("queue", f"invalid request payload {request!r}, acking without processing: {exc!r}")
        return {"processed": False, "reason": str(exc)}

    summary = registry.dispatch_once()
    _server_log(
        "queue",
        f"request_id={request_id} app={request['app']!r} type={request['type']!r} -- "
        f"submitted and dispatched (processed={summary.processed} errors={summary.errors})",
    )
    return {"processed": True, "request_id": request_id, "dispatch": {"processed": summary.processed, "errors": summary.errors}}

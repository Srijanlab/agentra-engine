"""server/routes/triggers.py — schedule / alarm / queue / promote entry points.

The engine records work and enqueues a job; the loop claims and executes it on
its tick (registry/jobs.py). The engine never runs a cycle, a promotion, or a
prod-debug pass itself.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import time
import uuid
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from agentra import environments, registry
from agentra.memory import Memory
from agentra.server.routes.human_input import dispatch_human_answer
from agentra.server.state import _active_runs
from agentra.server.utils import _paused_response, _server_log

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


def _new_run_key(app_name: str, source: str, objective: str, feature: str | None = None) -> str:
    run_key = uuid.uuid4().hex[:8]
    _active_runs[run_key] = {
        "app": app_name,
        "source": source,
        "status": "queued",
        "started_at": time.time(),
        "objective": objective,
        "feature": feature,
    }
    registry.record_run(run_key, **_active_runs[run_key])
    return run_key


async def _enqueue_cycle(
    app_name: str, source: str, objective_override: str | None, feature: str | None,
    skip_deploy: bool, *, enforce_schedule: bool,
) -> dict:
    if registry.is_paused():
        return _paused_response(source)

    repo = registry.get_app_repo(app_name)
    if repo is None:
        _server_log(source, f"app={app_name!r} not registered -- no-op")
        return {"triggered": False, "reason": f"app {app_name!r} not registered"}

    objective = objective_override or Memory(repo).get_objective()
    if not objective:
        _server_log(source, f"app={app_name!r} has no objective set -- no-op")
        return {"triggered": False, "reason": "no objective set for this app"}

    if enforce_schedule:
        env = environments.load(repo) or environments.EnvironmentConfig()
        last = registry.last_run_at(app_name, source="scheduled")
        due_in = None if last is None else env.schedule_hours * 3600 - (time.time() - last)
        if due_in is not None and due_in > 0:
            _server_log(source, f"app={app_name!r} not due for {due_in / 3600:.1f}h more -- skipped")
            return {"triggered": False, "reason": "not due yet per this app's configured schedule"}

    # dedup: one open cycle job per app -- the loop runs one backlog item per run.
    if any(j.get("payload", {}).get("app") == app_name for j in registry.list_jobs(status="pending")):
        return {"triggered": False, "reason": "a cycle for this app is already queued"}

    run_key = _new_run_key(app_name, source, objective, feature=feature)
    registry.record_run(run_key, skip_deploy=skip_deploy)
    job_id = registry.enqueue_job("cycle", {
        "run_key": run_key, "app": app_name, "objective": objective,
        "feature": feature, "skip_deploy": skip_deploy,
    }, dedup_key=f"cycle:{app_name}")
    _server_log(source, f"app={app_name!r} run_key={run_key} job={job_id} -- queued for the loop")
    return {"triggered": True, "run_key": run_key, "job_id": job_id, "queued": True}


def _reconcile_human_input_for_app(app_name: str) -> None:
    """Poll a needs_human issue's comments for an answer (there is no inbound
    webhook -- see connectors/slack.py) and enqueue a resume when one lands."""
    repo = registry.get_app_repo(app_name)
    if repo is None:
        return
    mem = Memory(repo)
    for loop in registry.list_waiting_for_human():
        if loop.get("app") != app_name:
            continue
        issue_number = loop.get("issue_number") or (loop.get("human_input") or {}).get("issue_number")
        if issue_number is None:
            continue
        issue_number = int(issue_number)
        try:
            answer = mem.find_unanswered_human_input_comment(issue_number)
            if not answer:
                continue
            dispatch_human_answer(app_name, repo, issue_number, answer, source="github-comment")
            _server_log("scheduled", f"app={app_name!r} issue=#{issue_number} -- human answered via GitHub comment, resume queued")
        except Exception:
            logger.warning("_reconcile_human_input_for_app: failed for app=%r issue=#%s", app_name, issue_number, exc_info=True)


def _reconcile_human_input_timeouts() -> None:
    """A run must never sit in waiting_for_human forever -- escalate past max-wait."""
    try:
        escalated = registry.reconcile_waiting_for_human()
    except Exception:
        logger.warning("_reconcile_human_input_timeouts: reconcile_waiting_for_human failed", exc_info=True)
        return
    if not escalated:
        return
    from agentra import urls
    from agentra.connectors import slack

    for loop in escalated:
        human_input = loop.get("human_input") or {}
        ref = loop.get("last_run_key") or loop.get("loop_id") or ""
        slack.notify_human_input_required(
            app=loop.get("app") or "",
            run_id=ref,
            question=human_input.get("question") or "(question unavailable)",
            issue_url=human_input.get("issue_url"),
            dashboard_url=urls.dashboard_run_url(ref, loop.get("app") or ""),
            branch=human_input.get("branch"),
            session_id=human_input.get("session_id"),
            escalated=True,
        )
        _server_log("scheduled", f"app={loop.get('app')!r} loop={loop.get('loop_id')} -- waiting_for_human past max-wait, escalated")


async def _tick() -> dict:
    """Everything the periodic scheduler does: reconcile, then enqueue every app
    that is due for a scheduled cycle."""
    try:
        registry.reconcile_stale_runs()  # also un-sticks loops stranded at last_run_status="running"
    except Exception:
        logger.warning("tick: reconcile_stale_runs failed", exc_info=True)
    results: dict = {}
    for app_name in registry.list_apps():
        try:
            _reconcile_human_input_for_app(app_name)
        except Exception:
            logger.warning("tick: human-input reconciliation failed for app=%r", app_name, exc_info=True)
        results[app_name] = await _enqueue_cycle(app_name, "scheduled", None, None, False, enforce_schedule=True)
    _reconcile_human_input_timeouts()
    return {"apps": results}


def _verify_cron(authorization: str | None) -> None:
    secret = os.environ.get("CRON_SECRET")
    if secret and authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="bad cron secret")


@router.get("/trigger/cron")
async def trigger_cron(authorization: str | None = Header(default=None)) -> dict:
    """Vercel Cron target (GET + Bearer CRON_SECRET). Same work as
    POST /trigger/scheduled with no app."""
    _verify_cron(authorization)
    return await _tick()


@router.post("/trigger/scheduled")
async def trigger_scheduled(payload: ScheduledTrigger) -> dict:
    if payload.app is None:
        return await _tick()
    return await _enqueue_cycle(
        payload.app, "scheduled", payload.objective, payload.feature, payload.skip_deploy, enforce_schedule=True
    )


@router.post("/apps/{app_name}/run")
async def run_app_now(app_name: str, payload: ScheduledTrigger | None = None) -> dict:
    body = payload or ScheduledTrigger(app=app_name)
    return await _enqueue_cycle(
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

    objective = Memory(repo).get_objective() or ""
    run_key = _new_run_key(app_name, "promote", objective)
    job_id = registry.enqueue_job("promote", {
        "run_key": run_key, "app": app_name, "target_repo": target_repo,
    }, dedup_key=f"promote:{app_name}")
    _server_log("promote", f"app={app_name!r} run_key={run_key} job={job_id} target_repo={target_repo!r} -- promotion queued")
    return {"triggered": True, "run_key": run_key, "job_id": job_id, "queued": True}


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

    objective = (payload.get("objective") if incident is None else None) or Memory(repo).get_objective()
    if not objective:
        _server_log("alarm", f"app={app_name!r} has no objective set -- no-op")
        return {"triggered": False, "reason": "no objective set for this app"}

    run_key = _new_run_key(app_name, "alarm", objective)
    job_id = registry.enqueue_job("prod_debug", {
        "run_key": run_key, "app": app_name, "objective": objective, "symptom": symptom,
    }, dedup_key=f"prod_debug:{app_name}")
    _server_log("alarm", f"app={app_name!r} run_key={run_key} job={job_id} symptom={symptom!r} -- prod-debug queued")
    return {"triggered": True, "run_key": run_key, "job_id": job_id, "queued": True}


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
        f"request_id={request_id} app={request['app']!r} type={request['type']!r} -- submitted "
        f"(processed={summary.processed} errors={summary.errors})",
    )
    return {"processed": True, "request_id": request_id, "dispatch": {"processed": summary.processed, "errors": summary.errors}}

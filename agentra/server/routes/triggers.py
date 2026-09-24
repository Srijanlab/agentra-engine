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
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from agentra import environments, registry
from agentra.memory import Memory
from agentra.server import auth
from agentra.registry.scheduler import compute_schedule_status
from agentra.server.digests.awaiting_testing import post_awaiting_testing_digest
from agentra.server.queue_auth import verify_queue_auth
from agentra.server.routes.human_input import dispatch_human_answer
from agentra.server.audit import actor_for
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
    skip_deploy: bool, *, enforce_schedule: bool, actor: str | None = None,
) -> dict:
    if registry.is_paused():
        return _paused_response(source, actor)

    repo = registry.get_app_repo(app_name)
    if repo is None:
        _server_log(source, f"app={app_name!r} not registered -- no-op", actor=actor)
        return {"triggered": False, "reason": f"app {app_name!r} not registered"}

    objective = objective_override or Memory(repo).get_objective()
    if not objective:
        _server_log(source, f"app={app_name!r} has no objective set -- no-op", actor=actor)
        return {"triggered": False, "reason": "no objective set for this app"}

    if enforce_schedule:
        status = compute_schedule_status(app_name, repo)
        if not status.due_now:
            _server_log(source, f"app={app_name!r} not due (due_in={status.due_in_seconds}) -- skipped", actor=actor)
            return {"triggered": False, "reason": "not due yet per this app's configured schedule"}

    # dedup: one open cycle job per app (pending or claimed) -- the loop runs one backlog item per run.
    if any(
        j.get("kind") == "cycle" and j.get("payload", {}).get("app") == app_name
        for status in ("pending", "claimed")
        for j in registry.list_jobs(status=status, limit=None)
    ):
        return {"triggered": False, "reason": "a cycle for this app is already queued"}

    run_key = _new_run_key(app_name, source, objective, feature=feature)
    registry.record_run(run_key, skip_deploy=skip_deploy)
    job_id = registry.enqueue_job("cycle", {
        "run_key": run_key, "app": app_name, "objective": objective,
        "feature": feature, "skip_deploy": skip_deploy,
    }, dedup_key=f"cycle:{app_name}")
    _server_log(source, f"app={app_name!r} run_key={run_key} job={job_id} -- queued for the loop", actor=actor)
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


def _reconcile_closed_issue_loops(app_name: str) -> None:
    """Release any loop still `active`/`waiting_for_human` whose tracked issue is
    closed. A run that ends non-terminally (shipped, resume_delivery) leaves its
    loop `active`; if a human then closes the issue, nothing flips the loop and
    every later scheduled cycle re-binds to it via bind_loop_for_run -- the run
    tied to a dead issue/loop (agentra#20/#25). issue_status needs a GitHub read,
    so this lives here, not in registry/."""
    repo = registry.get_app_repo(app_name)
    if repo is None:
        return
    mem = Memory(repo)
    for loop in registry.list_loops_by_status(("active", "waiting_for_human", "escalated"), app=app_name):
        issue_number = loop.get("issue_number")
        if not issue_number:
            continue
        try:
            if mem.issue_status(str(issue_number)) != "done":
                continue
        except Exception:
            continue
        loop_id = loop["loop_id"]
        registry.set_loop_pipeline(loop_id, terminal=True, status="done", next_node="done")
        registry.set_loop_status(loop_id, "released")
        _server_log("scheduled", f"app={app_name!r} loop={loop_id} issue=#{issue_number} closed -- loop released")


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
        app_name = loop.get("app") or ""
        issue_number = human_input.get("issue_number")
        # GitHub issue #45: without these, a per-app-channel setup with no global
        # SLACK_HUMAN_INPUT_CHANNEL silently dropped this reminder, and even when
        # delivered it landed as a disconnected top-level message instead of
        # threading onto the original escalation -- unlike _escalate_to_human's
        # own notify call, which always passes both.
        slack.notify_human_input_required(
            app=app_name,
            run_id=ref,
            question=human_input.get("question") or "(question unavailable)",
            issue_url=human_input.get("issue_url"),
            dashboard_url=urls.dashboard_run_url(ref, app_name),
            branch=human_input.get("branch"),
            session_id=human_input.get("session_id"),
            escalated=True,
            channel=registry.get_slack_channel(app_name),
            thread_ts=registry.slack_thread_for(app_name, issue_number) if issue_number is not None else None,
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
        try:
            _reconcile_closed_issue_loops(app_name)
        except Exception:
            logger.warning("tick: closed-issue loop reconciliation failed for app=%r", app_name, exc_info=True)
        try:
            post_awaiting_testing_digest(app_name)
        except Exception:
            logger.warning("tick: awaiting-testing digest failed for app=%r", app_name, exc_info=True)
        results[app_name] = await _enqueue_cycle(app_name, "scheduled", None, None, False, enforce_schedule=True)
    _reconcile_human_input_timeouts()
    try:
        from agentra.server import human_gate

        human_gates = human_gate.sweep_recent_runs()
    except Exception:
        logger.warning("tick: human_gate.sweep_recent_runs failed", exc_info=True)
        human_gates = []
    return {"apps": results, "human_gates": human_gates}


def _bearer_matches(authorization: str | None, secret: str | None) -> bool:
    prefix = "Bearer "
    if not secret or not authorization or not authorization.startswith(prefix):
        return False
    return hmac.compare_digest(authorization[len(prefix):].encode(), secret.encode())


def _verify_tick_auth(authorization: str | None) -> None:
    """Accept AGENTRA_TICK_TOKEN or CRON_SECRET; AGENTRA_INTERNAL_TOKEN is only
    accepted while no tick token is configured yet.

    GitHub issue #80: an RPC token holder can trigger scheduler ticks with side
    effects -- the fix is a dedicated tick-only credential. The deployed loop
    still authenticates its own /trigger/cron poll with AGENTRA_INTERNAL_TOKEN
    (agentra-loop's engine_client.py), and no AGENTRA_TICK_TOKEN has been
    provisioned in either the engine's environment or the loop's ECS task yet.
    Rejecting AGENTRA_INTERNAL_TOKEN outright before that rollout lands would
    401 the loop's own idle-tick poll in production -- breaking scheduled
    cycles, the human-input backstop sweep, and the awaiting-testing digest.
    This keeps the internal token working until AGENTRA_TICK_TOKEN is actually
    set; setting it anywhere immediately closes the hole with no further code
    change (see test_cron_internal_token_only_while_tick_token_unset)."""
    tick = os.environ.get("AGENTRA_TICK_TOKEN")
    cron = os.environ.get("CRON_SECRET")
    internal = os.environ.get("AGENTRA_INTERNAL_TOKEN")
    if tick and _bearer_matches(authorization, tick):
        return
    if cron and _bearer_matches(authorization, cron):
        return
    if not tick and internal and _bearer_matches(authorization, internal):
        return
    if tick or cron or internal or os.environ.get("AGENTRA_DYNAMODB_TABLE_PREFIX"):
        raise HTTPException(status_code=401, detail="bad tick token")


@router.get("/trigger/cron")
async def trigger_cron(authorization: str | None = Header(default=None)) -> dict:
    """The periodic scheduler tick: reconcile stale runs/loops, poll GitHub
    comments for human answers, enqueue every app due for a scheduled cycle,
    escalate stale waiting_for_human loops. Called by the loop's drain loop on
    its idle tick (and usable as an external cron target)."""
    _verify_tick_auth(authorization)
    return await _tick()


class DeployCompletePayload(BaseModel):
    app: str
    repo: str | None = None
    sha: str | None = None


@router.post("/trigger/deploy-complete")
async def trigger_deploy_complete(
    payload: DeployCompletePayload, authorization: str | None = Header(default=None)
) -> dict:
    """GitHub issue #49 (Phase 3): a code repo's own push-deploy CI/CD workflow calls
    this as its LAST step once the deploy has actually landed, so a loop parked on
    deploy-freshness (verify_pre_prod deferred because the live build hadn't caught
    up yet) can resume within seconds instead of waiting for the next
    backlog-driven or schedule-gated cycle to happen to re-poll. Reuses the same
    dedup'd enqueue as every other on-demand trigger -- if a cycle for this app is
    already queued or running, this is a harmless no-op. `repo`/`sha` are for
    logging only; check_backlog/verify_pre_prod already compute their own expected
    SHA from git history (see agents/deployment.py's last_deploy_relevant_sha), so
    nothing here needs to trust the caller's claim of what shipped."""
    _verify_tick_auth(authorization)
    if payload.app not in registry.list_apps():
        raise HTTPException(status_code=404, detail=f"app {payload.app!r} not registered")
    result = await _enqueue_cycle(payload.app, "deploy-complete", None, None, False, enforce_schedule=False)
    _server_log(
        "deploy-complete",
        f"app={payload.app!r} repo={payload.repo!r} sha={payload.sha!r} -- {result}",
    )
    return result


@router.post("/trigger/scheduled")
async def trigger_scheduled(payload: ScheduledTrigger) -> dict:
    if payload.app is None:
        return await _tick()
    return await _enqueue_cycle(
        payload.app, "scheduled", payload.objective, payload.feature, payload.skip_deploy, enforce_schedule=True
    )


@router.post("/apps/{app_name}/run")
async def run_app_now(app_name: str, request: Request, payload: ScheduledTrigger | None = None) -> dict:
    body = payload or ScheduledTrigger(app=app_name)
    return await _enqueue_cycle(
        app_name, "on-demand", body.objective, body.feature, body.skip_deploy,
        enforce_schedule=False, actor=actor_for(request),
    )


@router.post("/apps/{app_name}/promote")
async def promote_app(app_name: str, request: Request, payload: PromoteTrigger | None = None) -> dict:
    if app_name not in registry.list_apps():
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")
    if registry.is_paused():
        return _paused_response("promote", actor_for(request))

    repo = registry.get_app_repo(app_name)
    if repo is None:
        raise HTTPException(status_code=409, detail=f"local checkout for {app_name!r} is missing and could not be recovered")

    code_repos = registry.get_code_repos(app_name)
    requested = (payload or PromoteTrigger()).target_repo
    if requested and requested not in code_repos:
        raise HTTPException(
            status_code=400,
            detail=f"target_repo={requested!r} is not one of {app_name!r}'s code repos: {', '.join(code_repos)}.",
        )
    if requested:
        target_repos: list[str] | None = [requested]
    elif len(code_repos) == 1:
        target_repos = [next(iter(code_repos))]
    else:
        # Multiple code repos and no explicit pick (issue #7): the loop, which has
        # the checkouts, promotes every code repo whose pre-prod branch is ahead of
        # its prod branch. `None` == "auto".
        target_repos = None

    objective = Memory(repo).get_objective() or ""
    run_key = _new_run_key(app_name, "promote", objective)
    job_id = registry.enqueue_job("promote", {
        "run_key": run_key, "app": app_name, "target_repos": target_repos,
    }, dedup_key=f"promote:{app_name}")
    _server_log("promote", f"app={app_name!r} run_key={run_key} job={job_id} target_repos={target_repos!r} -- promotion queued", actor=actor_for(request))
    return {"triggered": True, "run_key": run_key, "job_id": job_id, "queued": True}


def _verify_alarm_webhook_auth(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("ALARM_WEBHOOK_PASSWORD")
    if not expected:
        if registry.cloud_mode() or os.environ.get("AGENTRA_DYNAMODB_TABLE_PREFIX"):
            raise HTTPException(status_code=401, detail="alarm webhook password not configured")
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


def _app_from_incident_doc(incident: dict) -> str | None:
    doc = incident.get("documentation")
    content = doc.get("content") if isinstance(doc, dict) else None
    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    app = parsed.get("app") if isinstance(parsed, dict) else None
    return app if isinstance(app, str) and app else None


@router.post("/trigger/alarm", dependencies=[Depends(_verify_alarm_webhook_auth)])
async def trigger_alarm(payload: dict) -> dict:
    if registry.is_paused():
        return _paused_response("alarm")

    incident = payload.get("incident")
    if incident is not None:
        if not isinstance(incident, dict):
            _server_log("alarm", "incident payload was not an object -- no-op")
            return {"triggered": False, "reason": "incident payload must be an object"}
        app_name = payload.get("app")
        symptom = incident.get("summary") or None
        if not app_name:
            app_name = _app_from_incident_doc(incident)
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
        _server_log("alarm", f"app={app_name!r} has no objective set -- no-op", actor=actor)
        return {"triggered": False, "reason": "no objective set for this app"}

    run_key = _new_run_key(app_name, "alarm", objective)
    job_id = registry.enqueue_job("prod_debug", {
        "run_key": run_key, "app": app_name, "objective": objective, "symptom": symptom,
    }, dedup_key=f"prod_debug:{app_name}")
    _server_log("alarm", f"app={app_name!r} run_key={run_key} job={job_id} symptom={symptom!r} -- prod-debug queued")
    return {"triggered": True, "run_key": run_key, "job_id": job_id, "queued": True}


@router.post("/trigger/queue", dependencies=[Depends(verify_queue_auth)])
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

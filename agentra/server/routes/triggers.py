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


def _record_production_release(repo: Path, run_id: str) -> list[str]:
    from agentra.agents import deployment

    env = environments.load(repo) or environments.EnvironmentConfig()
    mem = Memory(repo)
    prod_sha = sys.modules.get("agentra.server", None)
    if prod_sha is not None:
        _sha_fn = getattr(prod_sha, "_branch_head_sha", _branch_head_sha)
    else:
        _sha_fn = _branch_head_sha
    prod_sha = _sha_fn(repo, env.prod_branch)
    newly_released: list[str] = []

    for feature in mem.pending_promotion_features():
        name = feature["feature"]
        mem.record_released(name, release_run_id=run_id, commit_sha=prod_sha or feature.get("commit_sha"))
        newly_released.append(name)
        if not feature.get("status_done") and feature.get("external_id", "").isdigit():
            mem.mark_status_done(int(feature["external_id"]))

    for bug in mem.closed_bugs():
        if not bug.get("status_done") and bug.get("external_id", "").isdigit():
            mem.mark_status_done(int(bug["external_id"]))

    if newly_released:
        persist_error = deployment.persist_audit_trail(repo, env.prod_branch)
        if persist_error:
            _server_log("promote", f"release ledger persisted locally but push failed: {persist_error}")
    return newly_released


def _new_run_key(app_name: str, source: str, objective: str) -> str:
    run_key = uuid.uuid4().hex[:8]
    _active_runs[run_key] = {
        "app": app_name,
        "source": source,
        "status": "queued",
        "started_at": time.time(),
        "objective": objective,
        "loop_id": registry.loop_id_for(objective),
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
                repo, objective, env, feature=feature, skip_deploy=skip_deploy, run_id=run_key
            )
            _set_run(
                run_key,
                status="completed",
                result={
                    "run_id": report.run_id,
                    "actions": report.actions,
                    "final_message": report.final_message,
                    "cost_usd": report.cost_usd,
                    "feature": report.feature,
                },
            )
            _server_log(
                _active_runs[run_key]["source"],
                f"app={app_name!r} run_key={run_key} agentra_run_id={report.run_id} completed | cost=${report.cost_usd:.4f}",
            )
        except Exception as exc:
            _set_run(run_key, status="failed", error=str(exc))
            _server_log(_active_runs[run_key]["source"], f"app={app_name!r} run_key={run_key} raised: {exc!r}")


async def _run_prod_debug_background(
    run_key: str, app_name: str, repo: Path, objective: str, symptom: str | None
) -> None:
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
            _server_log(
                "alarm",
                f"app={app_name!r} run_key={run_key} agentra_run_id={report.run_id} "
                f"root_cause_found={report.root_cause_found} promoted_to_prod={report.promoted_to_prod}",
            )
        except Exception as exc:
            _set_run(run_key, status="failed", error=str(exc))
            _server_log("alarm", f"app={app_name!r} run_key={run_key} raised: {exc!r}")


async def _run_promote_background(run_key: str, app_name: str, repo: Path) -> None:
    lock = _lock_for(app_name)
    async with lock:
        _set_run(run_key, status="running")
        try:
            result = await run_promote(repo, run_id=run_key)
            released_features: list[str] = []
            if result["ok"]:
                released_features = _record_production_release(repo, run_key)
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

    run_key = _new_run_key(app_name, source, objective)
    _server_log(source, f"app={app_name!r} run_key={run_key} objective={objective!r} -- dispatched")
    asyncio.create_task(_run_autonomous_background(run_key, app_name, repo, objective, feature, skip_deploy))
    return {"triggered": True, "run_key": run_key}


@router.post("/trigger/scheduled")
async def trigger_scheduled(payload: ScheduledTrigger) -> dict:
    if payload.app is None:
        results = {}
        for app_name in registry.list_apps():
            results[app_name] = await _dispatch_cycle(
                app_name, "scheduled", None, None, False, enforce_schedule=True
            )
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
async def promote_app(app_name: str) -> dict:
    if app_name not in registry.list_apps():
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")
    if registry.is_paused():
        return _paused_response("promote")

    repo = registry.get_app_repo(app_name)
    if repo is None:
        raise HTTPException(status_code=409, detail=f"local checkout for {app_name!r} is missing and could not be recovered")

    if _lock_for(app_name).locked():
        _server_log("promote", f"app={app_name!r} already has a cycle running -- skipped")
        return {"triggered": False, "reason": "a cycle for this app is already running"}

    objective = Memory(repo).get_objective() or ""
    run_key = _new_run_key(app_name, "promote", objective)
    _server_log("promote", f"app={app_name!r} run_key={run_key} -- promotion to prod dispatched")
    asyncio.create_task(_run_promote_background(run_key, app_name, repo))
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

"""HTTP entry points that invoke the orchestrator from outside a terminal.

Until now every entry point in this codebase (agentra/cli.py) assumed a
human running a command in a terminal. vision.md's autonomous loop assumes
the system also reacts to (a) a schedule, (b) an error/alarm, (c) new work
landing in a queue -- none of which involve a human typing a command. This
module is those three trigger paths as real HTTP endpoints, meant to sit
behind Cloud Scheduler, a GCP Monitoring alerting webhook, and a Pub/Sub
push subscription respectively.

Deployed as the always-on Cloud Run *service*; the actual agent work still
runs as short-lived subprocesses the Claude Agent SDK spawns inside this
same process (agents/base.py::run_agent) -- "specialized agents run on
demand, not as standing services" was already true architecturally before
this module existed, this just gives that architecture inbound HTTP paths
to be triggered from instead of only a terminal.

Every handler that kicks off real agent work returns fast (submits the
cycle as a background asyncio task, returns 202 with a run_key to poll)
rather than blocking the HTTP response on a full cycle, which can run many
minutes -- past any reasonable request timeout. The one exception is
/trigger/queue: registry.dispatch_once() is cheap, pure file/JSON work (see
registry.py's own docstring), so it runs inline and Pub/Sub gets a prompt
ack.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hmac
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agentra import environments, registry
from agentra.agents.brain import run_autonomous_cycle
from agentra.dashboard import DASHBOARD_HTML
from agentra.memory import Memory
from agentra.orchestrator import run_prod_debug_cycle

logger = logging.getLogger("agentra.server")

app = FastAPI(title="agentra orchestrator")


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    return DASHBOARD_HTML

# In-memory record of background runs this process has kicked off -- lets a
# duplicate trigger for an app already mid-cycle be told so instead of
# double-starting a second, conflicting cycle, and backs GET /runs/{run_key}
# for polling. Deliberately in-process, not persisted: a restart losing this
# bookkeeping is fine, the durable record of what actually happened is each
# app's own <repo>/.agentra/memory/ and .agentra/logs/, same as every other
# entry point.
_active_runs: dict[str, dict[str, Any]] = {}
_app_locks: dict[str, asyncio.Lock] = {}


def _lock_for(app_name: str) -> asyncio.Lock:
    if app_name not in _app_locks:
        _app_locks[app_name] = asyncio.Lock()
    return _app_locks[app_name]


def _server_log(source: str, message: str) -> None:
    """Top-level trigger log, independent of any one app's own Memory -- a
    trigger can name an app that isn't registered, or arrive before any repo
    context is resolved at all, so it can't always be filed under an app's
    own .agentra/logs/."""
    registry.AGENTRA_HOME.mkdir(parents=True, exist_ok=True)
    path = registry.AGENTRA_HOME / "server.log"
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
    with path.open("a") as f:
        f.write(f"[{timestamp}] source={source} {message}\n")
    logger.info("source=%s %s", source, message)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "apps_registered": len(registry.list_apps())}


# ── TASK-017: global kill switch. Checked at the top of every trigger path
# below -- scheduled/alarm/queue/on-demand all no-op (not error) while
# paused, same "log why, return 200" shape the not-registered/no-objective
# no-ops already use, so a paused system doesn't look like a broken one to
# whatever's calling it (Cloud Scheduler, Pub/Sub -- retrying a paused
# no-op achieves nothing and would just be noise). ─────────────────────────


class PauseRequest(BaseModel):
    reason: str | None = None


def _paused_response(source: str) -> dict:
    _server_log(source, "system is paused -- no-op")
    return {"triggered": False, "reason": "system is paused"}


@app.get("/system/status")
async def system_status() -> dict:
    pause_record = registry.is_paused()
    return {"paused": pause_record is not None, "pause_record": pause_record}


@app.post("/system/pause")
async def system_pause(payload: PauseRequest | None = None) -> dict:
    reason = payload.reason if payload else None
    registry.pause(reason)
    _server_log("system", f"paused -- reason={reason!r}")
    return {"paused": True, "reason": reason}


@app.post("/system/resume")
async def system_resume() -> dict:
    registry.resume()
    _server_log("system", "resumed")
    return {"paused": False}


@app.get("/runs/{run_key}")
async def get_run(run_key: str) -> dict:
    run = _active_runs.get(run_key)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run_key")
    return run


@app.get("/runs")
async def list_runs(limit: int = 50) -> dict:
    """TASK-015: dashboard feed of active + recently completed runs.
    _active_runs is process-local (see its own docstring on why that's
    fine -- the durable record is each app's own Memory), so this only
    reflects runs since the current instance started, newest first."""
    runs = sorted(
        ({"run_key": key, **info} for key, info in _active_runs.items()),
        key=lambda r: r["started_at"],
        reverse=True,
    )
    return {"runs": runs[:limit]}


_SIGNAL_LINE_RE = re.compile(r"^\[(?P<ts>[^\]]+)\] source=(?P<source>\S+) (?P<message>.*)$")


@app.get("/signals")
async def list_signals(limit: int = 50) -> dict:
    """TASK-015: dashboard feed of every trigger this instance has logged
    (server.py's own _server_log ledger -- schedule/alarm/queue/register/
    system events), newest first."""
    log_path = registry.AGENTRA_HOME / "server.log"
    if not log_path.exists():
        return {"signals": []}
    lines = log_path.read_text().splitlines()[-limit:]
    signals = []
    for line in reversed(lines):
        m = _SIGNAL_LINE_RE.match(line)
        if m:
            signals.append(m.groupdict())
        else:
            signals.append({"ts": None, "source": None, "message": line})
    return {"signals": signals}


# ── App registration: TASK-016, register any GitHub repo from the dashboard
# without a pre-existing local checkout. Clones under registry.REPOS_ROOT
# (TASK-018: durable storage in the deployed environment, so the checkout
# survives a restart the same way the registry itself now does) and
# registers it exactly as `agentra apps add` would. ───────────────────────


class RegisterAppRequest(BaseModel):
    name: str
    repo_url: str
    branch: str = "main"
    objective: str | None = None


@app.get("/apps")
async def list_apps() -> dict:
    apps = registry.list_apps()
    result = {}
    for name, info in apps.items():
        repo = Path(info["repo_path"])
        mem = Memory(repo) if repo.exists() else None
        result[name] = {
            "repo_path": info["repo_path"],
            "objective": mem.get_objective() if mem else None,
            "shipped_count": len(mem.shipped_features()) if mem else 0,
            "known_bugs": len(mem.known_bugs()) if mem else 0,
        }
    return {"apps": result}


@app.post("/apps")
async def register_app(payload: RegisterAppRequest) -> dict:
    if payload.name in registry.list_apps():
        raise HTTPException(status_code=409, detail=f"app {payload.name!r} already registered")

    dest = registry.REPOS_ROOT / payload.name
    try:
        from agentra.agents.git_ops import GitOpError, clone_repo

        clone_repo(payload.repo_url, dest, branch=payload.branch)
    except GitOpError as exc:
        _server_log("register", f"app={payload.name!r} clone failed: {exc}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    registry.register_app(payload.name, str(dest))
    if payload.objective:
        Memory(dest).set_objective(payload.objective)

    _server_log("register", f"app={payload.name!r} repo_url={payload.repo_url!r} branch={payload.branch!r} -- registered at {dest}")
    return {"registered": True, "name": payload.name, "repo_path": str(dest)}


async def _run_autonomous_background(
    run_key: str, app_name: str, repo: Path, objective: str, feature: str | None, skip_deploy: bool
) -> None:
    lock = _lock_for(app_name)
    async with lock:
        _active_runs[run_key]["status"] = "running"
        try:
            env = environments.load(repo) or environments.EnvironmentConfig()
            report = await run_autonomous_cycle(
                repo, objective, env, feature=feature, skip_deploy=skip_deploy
            )
            _active_runs[run_key]["status"] = "completed"
            _active_runs[run_key]["result"] = {
                "run_id": report.run_id,
                "actions": report.actions,
                "final_message": report.final_message,
                "cost_usd": report.cost_usd,
            }
            _server_log(
                _active_runs[run_key]["source"],
                f"app={app_name!r} run_key={run_key} agentra_run_id={report.run_id} completed | cost=${report.cost_usd:.4f}",
            )
        except Exception as exc:
            _active_runs[run_key]["status"] = "failed"
            _active_runs[run_key]["error"] = str(exc)
            _server_log(_active_runs[run_key]["source"], f"app={app_name!r} run_key={run_key} raised: {exc!r}")


async def _run_prod_debug_background(
    run_key: str, app_name: str, repo: Path, objective: str, symptom: str | None
) -> None:
    lock = _lock_for(app_name)
    async with lock:
        _active_runs[run_key]["status"] = "running"
        try:
            report = await run_prod_debug_cycle(repo, objective, symptom=symptom)
            _active_runs[run_key]["status"] = "completed"
            _active_runs[run_key]["result"] = {
                "run_id": report.run_id,
                "root_cause_found": report.root_cause_found,
                "severity": report.severity,
                "fix_attempted": report.fix_attempted,
                "promoted_to_prod": report.promoted_to_prod,
            }
            _server_log(
                "alarm",
                f"app={app_name!r} run_key={run_key} agentra_run_id={report.run_id} "
                f"root_cause_found={report.root_cause_found} promoted_to_prod={report.promoted_to_prod}",
            )
        except Exception as exc:
            _active_runs[run_key]["status"] = "failed"
            _active_runs[run_key]["error"] = str(exc)
            _server_log("alarm", f"app={app_name!r} run_key={run_key} raised: {exc!r}")


def _new_run_key(app_name: str, source: str) -> str:
    run_key = uuid.uuid4().hex[:8]
    _active_runs[run_key] = {"app": app_name, "source": source, "status": "queued", "started_at": time.time()}
    return run_key


# ── (a) Scheduled: Cloud Scheduler -> here on a cron/interval ─────────────────


class ScheduledTrigger(BaseModel):
    app: str
    objective: str | None = None
    feature: str | None = None
    skip_deploy: bool = False


@app.post("/trigger/scheduled")
async def trigger_scheduled(payload: ScheduledTrigger) -> dict:
    if registry.is_paused():
        return _paused_response("scheduled")

    repo = registry.get_app_repo(payload.app)
    if repo is None:
        _server_log("scheduled", f"app={payload.app!r} not registered -- no-op")
        return {"triggered": False, "reason": f"app {payload.app!r} not registered"}

    if _lock_for(payload.app).locked():
        _server_log("scheduled", f"app={payload.app!r} already has a cycle running -- skipped")
        return {"triggered": False, "reason": "a cycle for this app is already running"}

    objective = payload.objective or Memory(repo).get_objective()
    if not objective:
        _server_log("scheduled", f"app={payload.app!r} has no objective set -- no-op")
        return {"triggered": False, "reason": "no objective set for this app"}

    run_key = _new_run_key(payload.app, "scheduled")
    _server_log("scheduled", f"app={payload.app!r} run_key={run_key} objective={objective!r} -- dispatched")
    asyncio.create_task(
        _run_autonomous_background(run_key, payload.app, repo, objective, payload.feature, payload.skip_deploy)
    )
    return {"triggered": True, "run_key": run_key}


@app.post("/apps/{app_name}/run")
async def run_app_now(app_name: str, payload: ScheduledTrigger | None = None) -> dict:
    """On-demand equivalent of /trigger/scheduled, for the dashboard's "run
    now" button -- same dispatch path, just not waiting for a cron tick."""
    body = payload or ScheduledTrigger(app=app_name)
    body.app = app_name
    return await trigger_scheduled(body)


# ── (b) Error/alarm: a GCP Monitoring alerting policy's Webhook notification
# channel POSTs {"incident": {...}}; also accepts a plain {app, symptom} body
# directly so this can be triggered/tested without a real alerting policy. ──


class AlarmTrigger(BaseModel):
    app: str
    symptom: str | None = None
    objective: str | None = None


def _verify_alarm_webhook_auth(authorization: str | None = Header(default=None)) -> None:
    """Cloud Scheduler and Pub/Sub push both authenticate to this service via
    OIDC tokens, verified by Cloud Run's own IAM invoker check before the
    request ever reaches this process (see cloudrun.tf's roles/run.invoker
    grants) -- but GCP Monitoring's Webhook notification channel has no OIDC
    support at all; it authenticates with plain HTTP Basic Auth configured
    on the channel. That means this is the one trigger path IAM-invoker
    can't protect on its own, so it gets its own check here.

    A no-op (open) when ALARM_WEBHOOK_PASSWORD isn't set, so local/manual
    testing (this repo's own live verification, tests/) needs no
    credentials. Deployed with it set (deploy/gcp/terraform/secrets.tf),
    every request must present it, since making this endpoint reachable at
    all from Monitoring's webhook mechanism requires the Cloud Run service
    to allow unauthenticated (allUsers) invocations -- see cloudrun.tf's
    comment on why that grant isn't made by default today.
    """
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


@app.post("/trigger/alarm", dependencies=[Depends(_verify_alarm_webhook_auth)])
async def trigger_alarm(payload: dict) -> dict:
    if registry.is_paused():
        return _paused_response("alarm")

    incident = payload.get("incident")
    if incident is not None:
        # GCP Monitoring's webhook schema: https://cloud.google.com/monitoring/support/notification-options#webhooks
        # No app name in that schema by design (an alerting policy isn't
        # app-aware) -- the policy's webhook URL should carry it, e.g.
        # /trigger/alarm?app=my-app, or the policy's documentation field can
        # be configured to include {"app": "..."} and this falls back to
        # parsing that as JSON.
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

    if _lock_for(app_name).locked():
        _server_log("alarm", f"app={app_name!r} already has a cycle running -- skipped")
        return {"triggered": False, "reason": "a cycle for this app is already running"}

    objective = (payload.get("objective") if incident is None else None) or Memory(repo).get_objective()
    if not objective:
        _server_log("alarm", f"app={app_name!r} has no objective set -- no-op")
        return {"triggered": False, "reason": "no objective set for this app"}

    run_key = _new_run_key(app_name, "alarm")
    _server_log("alarm", f"app={app_name!r} run_key={run_key} symptom={symptom!r} -- dispatched to prod-debug")
    asyncio.create_task(_run_prod_debug_background(run_key, app_name, repo, objective, symptom))
    return {"triggered": True, "run_key": run_key}


# ── (c) Queue: a Pub/Sub push subscription -> here whenever a message is
# published. Cheap, synchronous -- just files the request into the same
# durable inbox `agentra dispatch` already drains (registry.py). ─────────────


@app.post("/trigger/queue")
async def trigger_queue(envelope: dict) -> dict:
    if registry.is_paused():
        # Still ack (200) -- Pub/Sub retries a non-2xx, and a paused system
        # staying paused doesn't make this message any more processable on
        # redelivery. The request itself isn't durably lost: whoever
        # published it can resubmit once resumed.
        _server_log("queue", "system is paused -- acking without processing")
        return {"processed": False, "reason": "system is paused"}

    message = envelope.get("message")
    if not message or "data" not in message:
        # Pub/Sub retries on non-2xx, and a malformed envelope will never become
        # well-formed on retry -- ack it (200) so Pub/Sub stops resending it,
        # but log loudly since this is a real integration problem, not a no-op.
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

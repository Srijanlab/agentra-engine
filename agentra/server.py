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
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agentra import environments, registry
from agentra.agents.brain import run_autonomous_cycle
from agentra.connectors import github_app
from agentra.memory import Memory
from agentra.orchestrator import run_prod_debug_cycle
from agentra.standup import run_daily_standup, run_standup

logger = logging.getLogger("agentra.server")

app = FastAPI(title="agentra orchestrator")

# The React dashboard (agentra/web/) is built separately (`npm run build`)
# and served as static files, not embedded in Python source -- a real
# frontend toolchain, not a hand-maintained HTML string. AGENTRA_WEB_DIST
# lets the Dockerfile point this at wherever it copied `dist/` in the
# image; local dev defaults to the dist/ this same repo's web/ produces.
WEB_DIST = Path(os.environ.get("AGENTRA_WEB_DIST") or (Path(__file__).resolve().parent / "web" / "dist"))

if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="web-assets")


@app.get("/", response_model=None)
async def dashboard() -> FileResponse | dict:
    index = WEB_DIST / "index.html"
    if not index.exists():
        return {
            "error": "dashboard not built",
            "hint": "run `npm install && npm run build` in agentra/web/, or set AGENTRA_WEB_DIST",
        }
    return FileResponse(index)


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


# ── Connectors: TASK-016 follow-up. GitHub App status, so "is agentra
# actually able to reach this org's repos" is answerable from the
# dashboard instead of discovered by a 403 mid-registration. ──────────────


@app.get("/connectors/github")
async def github_connector_status() -> dict:
    if not github_app.is_configured():
        return {"configured": False, "installations": [], "error": None, "install_url": None}
    try:
        installations = github_app.list_installations()
        return {
            "configured": True,
            "installations": installations,
            "error": None,
            "install_url": github_app.install_url(),
        }
    except github_app.GitHubAppError as exc:
        return {"configured": True, "installations": [], "error": str(exc), "install_url": github_app.install_url()}


@app.get("/connectors/github/callback")
async def github_connector_callback() -> RedirectResponse:
    """GitHub redirects here after an install/update if the App's 'Setup
    URL' (Advanced settings) is pointed at this path -- purely a UX nicety
    (lands back on the dashboard instead of GitHub's blank confirmation
    page); list_installations() already reflects new installs immediately
    without this, since nothing here needs to persist an installation_id."""
    return RedirectResponse(url="/")


# ── App registration: TASK-016, register any GitHub repo from the dashboard
# without a pre-existing local checkout. Clones under registry.REPOS_ROOT
# (local disk, not durable storage -- gcsfuse can't hold a real git
# checkout, see cloudrun.tf's comment; registry.get_app_repo() re-clones
# from repo_url automatically if a checkout goes missing, TASK-018) and
# registers it exactly as `agentra apps add` would, plus repo_url/branch
# so that auto-reclone has something to work with. ────────────────────────


class RegisterAppRequest(BaseModel):
    name: str
    repo_url: str
    branch: str = "main"
    objective: str | None = None
    # Deployment info -- overrides on top of environments.detect()'s
    # best-effort read of the freshly-cloned repo (see environments.py).
    # None means "leave whatever detect() found"; only an explicit True/
    # False/string overrides it.
    vercel: bool | None = None
    firebase: bool | None = None
    ci_cd_on_push: bool | None = None
    pre_prod_branch: str | None = None
    prod_branch: str | None = None
    # Free-form context agents should have on hand -- saved as Memory
    # "architecture" entries (the category meant for exactly this: standing
    # context an agent reads at the start of a cycle, not per-run data).
    documentation_notes: str | None = None
    testing_notes: str | None = None


@app.get("/connectors/github/repos")
async def github_connector_repos() -> dict:
    """Backs the dashboard's repo picker -- every repo the App can
    currently reach, so registering one is a selection, not a URL to find
    and paste by hand."""
    if not github_app.is_configured():
        return {"repos": []}
    try:
        return {"repos": github_app.list_repos()}
    except github_app.GitHubAppError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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

    registry.register_app(payload.name, str(dest), repo_url=payload.repo_url, branch=payload.branch)

    mem = Memory(dest)
    if payload.objective:
        mem.set_objective(payload.objective)

    # Best-effort auto-detect from the repo itself, then layer any explicit
    # answers from the form on top -- detect() alone is a good default,
    # but shouldn't silently override something the user just told us.
    env_config = environments.detect(dest)
    for field in ("vercel", "firebase", "ci_cd_on_push", "pre_prod_branch", "prod_branch"):
        value = getattr(payload, field)
        if value is not None:
            setattr(env_config, field, value)
    environments.save(dest, env_config)

    if payload.documentation_notes:
        mem.write("architecture", "documentation", payload.documentation_notes)
    if payload.testing_notes:
        mem.write("architecture", "testing-notes", payload.testing_notes)

    # Without this, everything just written above (objective.yaml,
    # environments.yaml, the notes) only ever lands in this container's
    # local checkout -- which is NOT durable (TASK-018: only the registry
    # entry survives a restart; the checkout itself re-clones from
    # repo_url on demand, restoring whatever's on the remote, not whatever
    # was written here). Confirmed: without this push, a redeploy before
    # any cycle happened to run persist_audit_trail would silently lose
    # all of it.
    from agentra.agents.git_ops import GitOpError, commit_and_push

    push_warning = None
    try:
        commit_and_push(dest, payload.branch, "agentra: register app (objective/environment/notes)", [".agentra/"])
    except GitOpError as exc:
        push_warning = str(exc)
        _server_log("register", f"app={payload.name!r} registered, but persisting .agentra/ failed: {exc}")

    _server_log("register", f"app={payload.name!r} repo_url={payload.repo_url!r} branch={payload.branch!r} -- registered at {dest}")
    result = {"registered": True, "name": payload.name, "repo_path": str(dest)}
    if push_warning:
        result["warning"] = f"registered, but could not push .agentra/ to the remote: {push_warning}"
    return result


# ── Standups: TASK-019. Cheap enough (no tools, one short LLM call) to run
# synchronously from an HTTP handler, unlike a full autonomous cycle. ──────


@app.get("/apps/{app_name}/standup/latest")
async def get_latest_standup(app_name: str) -> dict:
    repo = registry.get_app_repo(app_name)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")
    latest = Memory(repo).latest_standup()
    if latest is None:
        return {"app": app_name, "standup": None}
    return {"app": app_name, "standup": latest}


@app.post("/apps/{app_name}/standup")
async def trigger_app_standup(app_name: str) -> dict:
    if registry.is_paused():
        _server_log("standup", f"app={app_name!r} system is paused -- no-op")
        return {"triggered": False, "reason": "system is paused"}

    repo = registry.get_app_repo(app_name)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")

    report = await run_standup(repo, app_name)
    _server_log("standup", f"app={app_name!r} generated")
    return {"app": app_name, "report": report}


@app.post("/standup/daily")
async def trigger_daily_standup() -> dict:
    """The orchestrator's side of the daily standup: run every registered
    app's standup and return the batch. Meant to sit behind Cloud
    Scheduler (scheduler.tf's agentra-daily-standup job), same pattern as
    /trigger/scheduled."""
    if registry.is_paused():
        _server_log("standup", "system is paused -- no-op")
        return {"triggered": False, "reason": "system is paused"}

    apps = registry.list_apps()
    if not apps:
        _server_log("standup", "no apps registered -- no-op")
        return {"triggered": False, "reason": "no apps registered"}

    reports = await run_daily_standup(apps)
    _server_log("standup", f"daily standup generated for {list(reports.keys())}")
    return {"triggered": True, "reports": reports}


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

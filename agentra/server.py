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
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agentra import environments, registry
from agentra.agents import deployment
from agentra.agents.brain import run_autonomous_cycle
from agentra.connectors import github_app
from agentra.memory import Memory
from agentra.orchestrator import run_prod_debug_cycle, run_promote
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
# double-starting a second, conflicting cycle. Every mutation is also
# written through to registry.record_run (Firestore-backed when configured,
# same as agent_steps/signals) via _set_run below, so GET /runs and
# /runs/{run_key} survive a redeploy instead of a rolled Cloud Run revision
# making every just-finished dogfooding cycle look like it never happened --
# the dashboard's whole "Runs" tab was silently instance-local until this.
_active_runs: dict[str, dict[str, Any]] = {}
_app_locks: dict[str, asyncio.Lock] = {}


def _set_run(run_key: str, **fields: Any) -> None:
    _active_runs[run_key].update(fields)
    registry.record_run(run_key, **fields)


def _lock_for(app_name: str) -> asyncio.Lock:
    if app_name not in _app_locks:
        _app_locks[app_name] = asyncio.Lock()
    return _app_locks[app_name]


def _server_log(source: str, message: str) -> None:
    """Top-level trigger log, independent of any one app's own Memory -- a
    trigger can name an app that isn't registered, or arrive before any repo
    context is resolved at all, so it can't always be filed under an app's
    own .agentra/logs/. Firestore-backed when configured (agentra's own
    durable operational state, same as registry.py's apps/pause/inbox);
    falls back to a local append-only file otherwise."""
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
    db = registry.firestore_client()
    if db is not None:
        db.collection("signals").add({"ts": timestamp, "source": source, "message": message})
    else:
        registry.AGENTRA_HOME.mkdir(parents=True, exist_ok=True)
        path = registry.AGENTRA_HOME / "server.log"
        with path.open("a") as f:
            f.write(f"[{timestamp}] source={source} {message}\n")
    logger.info("source=%s %s", source, message)


def _branch_head_sha(repo: Path, branch: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", branch],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _record_production_release(repo: Path, run_id: str) -> list[str]:
    env = environments.load(repo) or environments.EnvironmentConfig()
    mem = Memory(repo)
    released = {feature["feature"] for feature in mem.released_features()}
    prod_sha = _branch_head_sha(repo, env.prod_branch)
    newly_released: list[str] = []

    for feature in mem.shipped_features():
        name = feature["feature"]
        if name in released:
            continue
        mem.record_released(name, release_run_id=run_id, commit_sha=prod_sha or feature.get("commit_sha"))
        newly_released.append(name)

    if newly_released:
        persist_error = deployment.persist_audit_trail(repo, env.prod_branch)
        if persist_error:
            _server_log("promote", f"release ledger persisted locally but push failed: {persist_error}")
    return newly_released


def _run_log_path(run_key: str) -> Path | None:
    run = _active_runs.get(run_key) or registry.get_run(run_key)
    if run is None:
        return None
    repo = registry.get_app_repo(run["app"])
    if repo is None:
        return None
    return Memory(repo).log_root / f"{run_key}.log"


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
    run = _active_runs.get(run_key) or registry.get_run(run_key)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run_key")
    return run


@app.get("/runs/{run_key}/logs")
async def stream_run_logs(run_key: str) -> StreamingResponse:
    run = _active_runs.get(run_key) or registry.get_run(run_key)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run_key")

    log_path = _run_log_path(run_key)
    if log_path is None:
        raise HTTPException(status_code=409, detail="local checkout for this run is missing and could not be recovered")

    async def event_stream():
        offset = 0
        terminal_cycles = 0
        while True:
            if log_path.exists():
                lines = log_path.read_text().splitlines()
                for line in lines[offset:]:
                    yield f"data: {json.dumps({'line': line})}\n\n"
                offset = len(lines)
            current = _active_runs.get(run_key) or registry.get_run(run_key)
            terminal = current is not None and current.get("status") in {"completed", "failed"}
            if terminal:
                terminal_cycles += 1
                if terminal_cycles >= 2:
                    yield "event: done\ndata: {}\n\n"
                    break
            else:
                terminal_cycles = 0
            await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/runs")
async def list_runs(limit: int = 50) -> dict:
    """TASK-015: dashboard feed of active + recently completed runs.
    Sourced from registry.list_runs() (Firestore-backed when configured,
    same durable pattern as agent_steps/signals) rather than the in-process
    _active_runs dict, so this survives a redeploy instead of only
    reflecting runs since the current instance started.

    reconcile_stale_runs() runs first so a run orphaned by a dead process
    (see its own docstring) gets marked failed the moment any dashboard
    polls this -- cheap (no LLM calls, just comparing timestamps already
    in hand), so doing it on every poll is fine."""
    registry.reconcile_stale_runs()
    return {"runs": registry.list_runs(limit)}


_SIGNAL_LINE_RE = re.compile(r"^\[(?P<ts>[^\]]+)\] source=(?P<source>\S+) (?P<message>.*)$")


@app.get("/signals")
async def list_signals(limit: int = 50) -> dict:
    """TASK-015: dashboard feed of every trigger this instance has logged
    (server.py's own _server_log ledger -- schedule/alarm/queue/register/
    system events), newest first."""
    db = registry.firestore_client()
    if db is not None:
        from google.cloud import firestore

        docs = (
            db.collection("signals")
            .order_by("ts", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        return {"signals": [{"ts": d.get("ts"), "source": d.get("source"), "message": d.get("message")} for d in docs]}

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


@app.get("/agent-steps")
async def list_agent_steps(app_name: str | None = None, limit: int = 100) -> dict:
    """Per-agent trace for the dashboard's Agent Activity view -- which of
    the nine tools/agents ran, for which app/run, ok or not, cost, turns,
    and a short summary of what it worked on (agents/brain.py's
    OrchestratorSession.note(), made structured and durable -- see
    registry.record_agent_step)."""
    return {"steps": registry.list_agent_steps(app=app_name, limit=limit)}


# ── Connectors: TASK-016 follow-up. GitHub App status, so "is agentra
# actually able to reach this org's repos" is answerable from the
# dashboard instead of discovered by a 403 mid-registration. ──────────────

# Local testing capability: without this, App.tsx's whole dashboard is
# gated behind a real installed GitHub App, so seeing anything beyond the
# ConnectGate screen locally meant either real credentials or a one-off
# Playwright route mock outside a real browser session. AGENTRA_DEV_MODE=1
# (set by scripts/dev.sh, never in any deployed environment) fakes a
# already-installed App so `agentra dev` renders the full dashboard against
# dev_seed.py's fixture data -- only takes effect when a real App genuinely
# isn't configured, so it can never mask a real misconfiguration.
DEV_MODE = os.environ.get("AGENTRA_DEV_MODE") == "1"
_DEV_INSTALLATION = {"account": "dev-local", "type": "User", "repository_selection": "all"}
_DEV_REPOS = [
    {
        "full_name": "dev-local/example-app",
        "clone_url": "https://github.com/dev-local/example-app.git",
        "default_branch": "main",
        "private": False,
        "account": "dev-local",
    }
]


@app.get("/connectors/github")
async def github_connector_status() -> dict:
    if DEV_MODE and not github_app.is_configured():
        return {"configured": True, "installations": [_DEV_INSTALLATION], "error": None, "install_url": None}
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
    schedule_hours: float | None = None
    alarm_enabled: bool | None = None
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
    if DEV_MODE and not github_app.is_configured():
        return {"repos": _DEV_REPOS}
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
        env_config = environments.load(repo) if repo.exists() else None
        result[name] = {
            "repo_path": info["repo_path"],
            "objective": mem.get_objective() if mem else None,
            "shipped_count": len(mem.shipped_features()) if mem else 0,
            "released_count": len(mem.released_features()) if mem else 0,
            "known_bugs": len(mem.known_bugs()) if mem else 0,
            "pre_prod_branch": env_config.pre_prod_branch if env_config else environments.EnvironmentConfig().pre_prod_branch,
            "prod_branch": env_config.prod_branch if env_config else environments.EnvironmentConfig().prod_branch,
            "schedule_hours": env_config.schedule_hours if env_config else environments.EnvironmentConfig().schedule_hours,
            "alarm_enabled": env_config.alarm_enabled if env_config else environments.EnvironmentConfig().alarm_enabled,
        }
    return {"apps": result}


def _apply_app_config(
    dest: Path,
    branch: str,
    *,
    objective: str | None,
    vercel: bool | None,
    firebase: bool | None,
    ci_cd_on_push: bool | None,
    pre_prod_branch: str | None,
    prod_branch: str | None,
    schedule_hours: float | None,
    alarm_enabled: bool | None,
    documentation_notes: str | None,
    testing_notes: str | None,
    detect_defaults: bool,
    commit_message: str,
) -> str | None:
    """Shared by register (fresh clone, detect_defaults=True so an
    auto-detected baseline is layered under any explicit answers) and
    update (existing checkout, detect_defaults=False so an untouched field
    keeps its current saved value instead of being silently reset to
    whatever detect() would guess today). Returns a push-failure warning
    string, or None on success -- same shape either caller surfaces to the
    dashboard."""
    mem = Memory(dest)
    if objective:
        mem.set_objective(objective)

    env_config = environments.detect(dest) if detect_defaults else (environments.load(dest) or environments.EnvironmentConfig())
    for field, value in (
        ("vercel", vercel),
        ("firebase", firebase),
        ("ci_cd_on_push", ci_cd_on_push),
        ("pre_prod_branch", pre_prod_branch),
        ("prod_branch", prod_branch),
        ("schedule_hours", schedule_hours),
        ("alarm_enabled", alarm_enabled),
    ):
        if value is not None:
            setattr(env_config, field, value)
    environments.save(dest, env_config)

    if documentation_notes:
        mem.write("architecture", "documentation", documentation_notes)
    if testing_notes:
        mem.write("architecture", "testing-notes", testing_notes)

    # Without this, everything just written above (objective.yaml,
    # environments.yaml, the notes) only ever lands in this container's
    # local checkout -- which is NOT durable (TASK-018: only the registry
    # entry survives a restart; the checkout itself re-clones from
    # repo_url on demand, restoring whatever's on the remote, not whatever
    # was written here). Confirmed: without this push, a redeploy before
    # any cycle happened to run persist_audit_trail would silently lose
    # all of it.
    from agentra.agents.git_ops import GitOpError, commit_and_push

    try:
        commit_and_push(dest, branch, commit_message, [".agentra/"])
        return None
    except GitOpError as exc:
        return str(exc)


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

    push_warning = _apply_app_config(
        dest,
        payload.branch,
        objective=payload.objective,
        vercel=payload.vercel,
        firebase=payload.firebase,
        ci_cd_on_push=payload.ci_cd_on_push,
        pre_prod_branch=payload.pre_prod_branch,
        prod_branch=payload.prod_branch,
        schedule_hours=payload.schedule_hours,
        alarm_enabled=payload.alarm_enabled,
        documentation_notes=payload.documentation_notes,
        testing_notes=payload.testing_notes,
        detect_defaults=True,
        commit_message="agentra: register app (objective/environment/notes)",
    )
    if push_warning:
        _server_log("register", f"app={payload.name!r} registered, but persisting .agentra/ failed: {push_warning}")

    _server_log("register", f"app={payload.name!r} repo_url={payload.repo_url!r} branch={payload.branch!r} -- registered at {dest}")
    result = {"registered": True, "name": payload.name, "repo_path": str(dest)}
    if push_warning:
        result["warning"] = f"registered, but could not push .agentra/ to the remote: {push_warning}"
    return result


@app.get("/apps/{name}")
async def get_app(name: str) -> dict:
    """Full config for the dashboard's edit modal -- the list view (GET
    /apps) deliberately stays a cheap digest since it's polled every 5s;
    this one is fetched on demand when a user opens Edit."""
    apps = registry.list_apps()
    if name not in apps:
        raise HTTPException(status_code=404, detail=f"app {name!r} not registered")
    info = apps[name]

    repo = registry.get_app_repo(name)
    if repo is None:
        raise HTTPException(status_code=409, detail=f"local checkout for {name!r} is missing and could not be recovered")

    mem = Memory(repo)
    env_config = environments.load(repo) or environments.EnvironmentConfig()
    return {
        "name": name,
        "repo_path": str(repo),
        "repo_url": info.get("repo_url"),
        "branch": info.get("branch"),
        "objective": mem.get_objective(),
        "shipped_count": len(mem.shipped_features()),
        "released_count": len(mem.released_features()),
        "known_bugs": len(mem.known_bugs()),
        "shipped": mem.shipped_features(),
        "released": mem.released_features(),
        "bugs": mem.known_bugs(),
        "feature_queue": mem.feature_queue(),
        "vercel": env_config.vercel,
        "firebase": env_config.firebase,
        "ci_cd_on_push": env_config.ci_cd_on_push,
        "pre_prod_branch": env_config.pre_prod_branch,
        "prod_branch": env_config.prod_branch,
        "schedule_hours": env_config.schedule_hours,
        "alarm_enabled": env_config.alarm_enabled,
        "documentation_notes": mem.read("architecture", "documentation"),
        "testing_notes": mem.read("architecture", "testing-notes"),
    }


class UpdateAppRequest(BaseModel):
    objective: str | None = None
    vercel: bool | None = None
    firebase: bool | None = None
    ci_cd_on_push: bool | None = None
    pre_prod_branch: str | None = None
    prod_branch: str | None = None
    schedule_hours: float | None = None
    alarm_enabled: bool | None = None
    documentation_notes: str | None = None
    testing_notes: str | None = None


@app.patch("/apps/{name}")
async def update_app(name: str, payload: UpdateAppRequest) -> dict:
    apps = registry.list_apps()
    if name not in apps:
        raise HTTPException(status_code=404, detail=f"app {name!r} not registered")
    info = apps[name]

    repo = registry.get_app_repo(name)
    if repo is None:
        raise HTTPException(status_code=409, detail=f"local checkout for {name!r} is missing and could not be recovered")

    push_warning = _apply_app_config(
        repo,
        info.get("branch", "main"),
        objective=payload.objective,
        vercel=payload.vercel,
        firebase=payload.firebase,
        ci_cd_on_push=payload.ci_cd_on_push,
        pre_prod_branch=payload.pre_prod_branch,
        prod_branch=payload.prod_branch,
        schedule_hours=payload.schedule_hours,
        alarm_enabled=payload.alarm_enabled,
        documentation_notes=payload.documentation_notes,
        testing_notes=payload.testing_notes,
        detect_defaults=False,
        commit_message="agentra: update app configuration",
    )
    _server_log("update", f"app={name!r} configuration updated" + (f" -- push failed: {push_warning}" if push_warning else ""))
    result = {"updated": True, "name": name}
    if push_warning:
        result["warning"] = f"updated, but could not push .agentra/ to the remote: {push_warning}"
    return result


@app.get("/apps/{name}/loops")
async def get_app_loops(name: str) -> dict:
    """The end-to-end view: every run made toward one objective for this
    app, grouped together with aggregate cost and a per-agent breakdown --
    see registry.list_loops's own docstring for why a loop is exactly
    this and not a separately tracked start/stop batch."""
    if name not in registry.list_apps():
        raise HTTPException(status_code=404, detail=f"app {name!r} not registered")
    return {"loops": registry.list_loops(name)}


class BacklogRequestPayload(BaseModel):
    type: str = "feature_request"
    description: str
    severity: str | None = None


@app.post("/apps/{name}/backlog")
async def submit_backlog_request(name: str, payload: BacklogRequestPayload) -> dict:
    """Dashboard's "add to backlog" action -- same durable inbox path
    /trigger/queue uses (registry.submit_request then dispatch_once,
    cheap/synchronous, no LLM calls), just submitted directly instead of
    via a Pub/Sub envelope. The request type determines whether it appears
    in known_bugs or feature_queue immediately, not after the next queue tick."""
    if payload.type not in {"bug", "feature_request"}:
        raise HTTPException(status_code=400, detail="type must be 'bug' or 'feature_request'")
    if payload.type == "bug" and payload.severity not in {"critical", "high", "medium", "low"}:
        raise HTTPException(status_code=400, detail="bug severity must be critical, high, medium, or low")
    try:
        request_id = registry.submit_request(
            app=name,
            request_type=payload.type,
            description=payload.description,
            severity=payload.severity if payload.type == "bug" else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    registry.dispatch_once()
    _server_log("queue", f"request_id={request_id} app={name!r} type={payload.type!r} -- submitted from dashboard")
    return {"submitted": True, "request_id": request_id}


@app.post("/apps/{name}/feature-requests")
async def submit_feature_request(name: str, payload: BacklogRequestPayload) -> dict:
    """Compatibility endpoint for older dashboard clients."""
    payload.type = "feature_request"
    payload.severity = None
    return await submit_backlog_request(name, payload)


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


# ── (a) Scheduled: Cloud Scheduler -> here on a cron/interval ─────────────────


class ScheduledTrigger(BaseModel):
    app: str
    objective: str | None = None
    feature: str | None = None
    skip_deploy: bool = False


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


@app.post("/trigger/scheduled")
async def trigger_scheduled(payload: ScheduledTrigger) -> dict:
    """Meant to sit behind one Cloud Scheduler cron tick shared by every
    app (e.g. hourly) -- enforce_schedule=True means each app only
    actually dispatches once its own configured schedule_hours has
    elapsed since its last scheduled run, so one tick serves every app on
    its own cadence without a GCP Scheduler job per app."""
    return await _dispatch_cycle(
        payload.app, "scheduled", payload.objective, payload.feature, payload.skip_deploy, enforce_schedule=True
    )


@app.post("/apps/{app_name}/run")
async def run_app_now(app_name: str, payload: ScheduledTrigger | None = None) -> dict:
    """On-demand equivalent of /trigger/scheduled, for the dashboard's "run
    now" button -- same dispatch path, but never schedule-gated (a human
    explicitly asking for a run now should never be told "not due yet")."""
    body = payload or ScheduledTrigger(app=app_name)
    return await _dispatch_cycle(
        app_name, "on-demand", body.objective, body.feature, body.skip_deploy, enforce_schedule=False
    )


@app.post("/apps/{app_name}/promote")
async def promote_app(app_name: str) -> dict:
    """Dashboard's Promote-to-prod action: merges pre_prod_branch into
    prod_branch and pushes, via the exact same human-approved
    orchestrator.run_promote flow `agentra promote` uses -- just
    triggered from the UI instead of a terminal. There is no dry-run or
    review step here: clicking Promote IS the approval, and it pushes to
    prod_branch immediately."""
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

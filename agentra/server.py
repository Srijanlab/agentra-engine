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

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agentra import chat_store, environments, registry
from agentra.agents import catalog as agents_catalog
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


def _app_branch(app_name: str) -> str:
    return registry.list_apps().get(app_name, {}).get("branch") or "main"


def _run_log_path(run_key: str) -> Path | None:
    run = _active_runs.get(run_key) or registry.get_run(run_key)
    if run is None:
        return None
    repo = registry.get_app_repo(run["app"])
    if repo is None:
        return None
    return Memory(repo).log_root / f"{run_key}.log"


def _run_screenshot_path(run_key: str) -> Path | None:
    run = _active_runs.get(run_key) or registry.get_run(run_key)
    if run is None:
        return None
    repo = registry.get_app_repo(run["app"])
    if repo is None:
        return None
    from agentra.agents.testing import screenshot_path

    return screenshot_path(repo, run_key)


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
        # A redeploy re-clones every registered app's checkout fresh
        # (REPOS_ROOT is documented VM-local-only), which wipes
        # .agentra/logs/ immediately since it's deliberately gitignored --
        # so any run whose log isn't sitting on THIS instance's disk is
        # either genuinely still in flight elsewhere (impossible in this
        # single-VM deployment) or its only copy now lives in Firestore's
        # run_logs mirror (memory.py's log()). One-shot dump from there,
        # not live-tailed, since a log missing locally can only mean the
        # run already finished before the most recent redeploy.
        if not log_path.exists():
            db = registry.firestore_client()
            if db is not None:
                doc = db.collection("run_logs").document(run_key).get()
                if doc.exists:
                    for line in doc.to_dict().get("lines", []):
                        yield f"data: {json.dumps({'line': line})}\n\n"
                    yield "event: done\ndata: {}\n\n"
                    return

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


@app.get("/runs/{run_key}/screenshot")
async def get_run_screenshot(run_key: str) -> FileResponse:
    """The Testing Agent's pre-prod screenshot (agents/testing.py's
    run_pre_prod, agents/screenshot.py) for a human reviewing this run to
    see the actual live page, not just its written report. VM-local only
    (test_artifacts/ is gitignored, same tier per-run logs used to be
    before their Firestore mirror) -- lost on the next redeploy, unlike
    logs; a real follow-up would give this the same durability treatment.
    404 if this run never captured one (screenshot failed, or predates
    this feature)."""
    run = _active_runs.get(run_key) or registry.get_run(run_key)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run_key")
    path = _run_screenshot_path(run_key)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="no screenshot captured for this run")
    return FileResponse(path, media_type="image/png")


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


@app.get("/agents/metadata")
async def list_agent_metadata() -> dict:
    """Static per-agent profile for the dashboard's Agent card: skills,
    tools, and each tool's permission tier (agents/catalog.py) -- not run
    history, so it's populated even for an agent that's never executed."""
    return {"agents": agents_catalog.AGENT_METADATA, "permission_model_note": agents_catalog.PERMISSION_MODEL_NOTE}


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
if DEV_MODE:
    # known_bugs/feature_queue/objective/environments are GitHub-only now
    # (memory.py/environments.py), and dev mode never has real GitHub App
    # credentials -- fake the backend here too, not just in dev_seed.py's
    # seed script, since this server process is what actually serves every
    # dashboard read/write after seeding. Persisted under AGENTRA_HOME so
    # it survives across dev_seed.py's separate seed-then-serve processes
    # (dev.sh's documented workflow) -- never under a target repo's own
    # .agentra/, which stays genuinely GitHub-only.
    from agentra.connectors import github_fake

    github_fake.install(persist_path=registry.AGENTRA_HOME / "dev_github_fake.json")
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


@app.get("/loops")
async def get_all_loops() -> dict:
    """Same grouped-by-loop view as /apps/{name}/loops, across every
    registered app -- lets the dashboard's "All apps" filter show the same
    structure (objective header, agent totals, collapsible runs) the
    per-app view already does, instead of a flatter, differently-shaped
    timeline."""
    return {"loops": registry.list_loops(None)}


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
    if app_name not in registry.list_apps():
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")
    latest = chat_store.latest_standup(app_name)
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
    error = registry.persist_agentra_dir(repo, _app_branch(app_name), f"agentra: daily standup for {app_name!r}")
    if error:
        _server_log("standup", f"app={app_name!r} standup saved locally but failed to push: {error}")
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


_STANDUP_MENTION_PATTERN = re.compile(r"@(\w+)")


def _route_standup_message(text: str) -> str:
    """Which agent a human's standup-channel message is addressed to --
    @mention by id or label's first word, case-insensitive (e.g. "@testing"
    or "@Testing" both match the testing agent). Defaults to the
    Orchestrator when nothing matches, since deciding who should actually
    field an unaddressed question is exactly its job."""
    from agentra.standup import AGENT_LABELS

    match = _STANDUP_MENTION_PATTERN.search(text)
    if not match:
        return "orchestrator"
    mentioned = match.group(1).lower()
    for agent_id, label in AGENT_LABELS.items():
        if mentioned == agent_id or mentioned == label.split()[0].lower():
            return agent_id
    return "orchestrator"


@app.websocket("/apps/{app_name}/standup/live")
async def standup_live_channel(websocket: WebSocket, app_name: str) -> None:
    """The live, multi-agent standup channel -- the interactive replacement
    for the old static written report as the dashboard's primary standup
    experience. The POST/GET report endpoints above are untouched (the
    daily-cron path and historical record still want a durable markdown
    snapshot); this is a different, live view of the same underlying data.

    On connect: replays today's channel if it already has messages,
    otherwise generates the opening burst -- generate_standup_updates() is
    the exact same single cheap LLM call the written report is built from,
    just posted as one message per agent instead of one combined blob, so
    this costs nothing extra over what standups already cost. Then listens
    for human messages, routes each to whichever agent it's @mentioned (or
    the Orchestrator if unaddressed), and streams that agent's reply back
    token-by-token via stream_chat_turn -- the same session-resume/
    grounding/safety-hook machinery 1:1 chat uses, just under a separate
    per-day session namespace (standup:{date}:{agent_id}) so this
    conversation doesn't bleed into unrelated 1:1 Q&A with the same agent,
    and starts fresh each new standup rather than accumulating forever."""
    await websocket.accept()
    repo = registry.get_app_repo(app_name)
    if repo is None:
        await websocket.close(code=1008, reason=f"app {app_name!r} not registered")
        return

    mem = Memory(repo)
    date_str = dt.datetime.now(dt.timezone.utc).date().isoformat()

    existing = chat_store.get_standup_channel_messages(app_name, date_str)
    if existing:
        # Replayed history on reconnect -- no "fresh" flag, so the
        # frontend shows it instantly instead of re-running the
        # one-agent-at-a-time reveal pacing a human already sat through
        # once already today.
        for msg in existing:
            await websocket.send_json({"type": "message", **msg})
    else:
        from agentra.standup import generate_standup_updates

        updates = await generate_standup_updates(repo, app_name, mem)
        for agent_id, text in updates.items():
            msg = chat_store.record_standup_channel_message(app_name, date_str, agent_id, text)
            await websocket.send_json({"type": "message", "fresh": True, **msg})

    # Marks the end of the opening burst specifically (not history replay,
    # which the frontend already knows is complete once the socket opens
    # with no further sends expected). The dashboard's "All apps" view
    # needs this to know when it's safe to merge every app's per-agent
    # updates into one combined bubble per agent role -- without an
    # explicit signal it would have to guess "how many messages is enough"
    # per app, which breaks the moment an app's actual agent roster ever
    # differs from another's.
    await websocket.send_json({"type": "burst_complete"})

    try:
        while True:
            data = await websocket.receive_json()
            human_text = (data.get("message") or "").strip()
            if not human_text:
                continue

            human_msg = chat_store.record_standup_channel_message(app_name, date_str, "human", human_text)
            await websocket.send_json({"type": "message", **human_msg})

            agent_id = _route_standup_message(human_text)
            resume_session_id = chat_store.get_standup_agent_session_id(app_name, date_str, agent_id)
            system_prompt = AGENT_CHAT_SYSTEM_PROMPTS.get(agent_id, AGENT_CHAT_SYSTEM_PROMPTS["custom"])

            from agentra.agents.base import extract_json_block, stream_chat_turn

            await websocket.send_json({"type": "start", "sender": agent_id})
            full_text = ""
            session_id: str | None = None
            async for event in stream_chat_turn(
                prompt=human_text,
                system_prompt=system_prompt,
                cwd=repo,
                allowed_tools=["Read", "Grep", "Glob"],
                max_turns=10,
                agent_label=_chat_agent_label(agent_id),
                resume=resume_session_id,
            ):
                if event["type"] == "delta":
                    full_text += event["text"]
                    await websocket.send_json({"type": "delta", "sender": agent_id, "text": event["text"]})
                else:
                    full_text = event["text"] or full_text
                    session_id = event["session_id"]

            json_data = extract_json_block(full_text)
            if json_data and "work_update" in json_data:
                mem.record_work_update(agent_id, json_data["work_update"])
                full_text = re.sub(r"```json\s*.*?\s*```", "", full_text, flags=re.DOTALL).strip()

            if session_id:
                chat_store.set_standup_agent_session_id(app_name, date_str, agent_id, session_id)
            agent_msg = chat_store.record_standup_channel_message(app_name, date_str, agent_id, full_text)
            # "replacing_stream" tells the frontend to collapse the
            # delta-built bubble into this authoritative final version
            # (full_text can differ from the raw concatenated deltas -- the
            # work_update JSON block strip above, same as 1:1 chat).
            await websocket.send_json({"type": "message", "replacing_stream": True, **agent_msg})

            def _persist_channel_in_background() -> None:
                error = registry.persist_agentra_dir(
                    repo, _app_branch(app_name), f"agentra: standup channel for {app_name!r}"
                )
                if error:
                    _server_log("standup", f"app={app_name!r} channel message saved locally but failed to push: {error}")

            asyncio.get_running_loop().run_in_executor(None, _persist_channel_in_background)
    except WebSocketDisconnect:
        pass


# Neural2 (1M free chars/month, permanent -- not the 90-day new-customer
# trial) reads noticeably more naturally than the browser's own
# speechSynthesis, which is what this replaces. One distinct voice per
# agent, same "who's speaking" cue the text-side avatar/label already
# gives -- arbitrary assignment (there's no canonical "testing agent
# voice"), just needs to be consistent and distinct per agent, not
# meaningfully matched to personality.
AGENT_VOICES = {
    "orchestrator": "en-US-Neural2-D",
    "codebase": "en-US-Neural2-A",
    "discovery": "en-US-Neural2-C",
    "implementation": "en-US-Neural2-I",
    "testing": "en-US-Neural2-E",
    "deployment": "en-US-Neural2-J",
    "feedback": "en-US-Neural2-F",
    "prod_debug": "en-US-Neural2-G",
    "custom": "en-US-Neural2-H",
}


class TtsPayload(BaseModel):
    text: str
    agent_id: str = "custom"


@app.post("/tts")
async def text_to_speech(payload: TtsPayload) -> Response:
    """Agent voice output, real synthesized speech instead of the browser's
    own speechSynthesis -- returns raw MP3 bytes the dashboard plays
    directly via an <audio> element; nothing this ephemeral needs a
    separate upload/storage round-trip. Runs in a thread (the client
    library's synthesize_speech is synchronous, blocking gRPC) so it
    doesn't stall the event loop other requests are sharing.

    503 (not 500) on any failure -- most likely cause in practice is ADC
    not being configured in whatever environment this runs in (e.g. local
    dev without `gcloud auth application-default login`), which is a
    "voice isn't available right now" condition for the human, not a
    server bug to alarm over."""
    from google.cloud import texttospeech

    voice_name = AGENT_VOICES.get(payload.agent_id, AGENT_VOICES["custom"])

    def _synthesize() -> bytes:
        client = texttospeech.TextToSpeechClient()
        response = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=payload.text),
            voice=texttospeech.VoiceSelectionParams(language_code="en-US", name=voice_name),
            audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3),
        )
        return response.audio_content

    try:
        audio = await asyncio.get_running_loop().run_in_executor(None, _synthesize)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"text-to-speech unavailable: {exc}") from exc

    return Response(content=audio, media_type="audio/mpeg")


class ChatPayload(BaseModel):
    message: str

class WorkUpdatePayload(BaseModel):
    description: str

AGENT_CHAT_SYSTEM_PROMPTS = {
    "orchestrator": """You are the Orchestrator Agent. You decide which agent to call next and sequence tasks. You are now communicating directly with a human user. Respond to their queries in a helpful and concise manner.
If you have performed any work recently, you can update the work you performed by including a JSON block at the end of your response:
```json
{
  "work_update": "Description of the work you performed"
}
```
""",
    "codebase": """You are the Codebase Agent. Your role is read-only scanning of the repository: mapping framework, architecture, and existing patterns. Respond to user queries about the code.
If you have performed any work recently, you can update the work you performed by including a JSON block at the end of your response:
```json
{
  "work_update": "Description of the work you performed"
}
```
""",
    "discovery": """You are the Discovery Agent. Your role is deciding what to build next based on codebase, analytics, and backlog signals.
If you have performed any work recently, you can update the work you performed by including a JSON block at the end of your response:
```json
{
  "work_update": "Description of the work you performed"
}
```
""",
    "implementation": """You are the Implementation Agent. Your role is implementing features and fixing bugs on dedicated branches.
If you have performed any work recently, you can update the work you performed by including a JSON block at the end of your response:
```json
{
  "work_update": "Description of the work you performed"
}
```
""",
    "testing": """You are the Testing Agent. Your role is verifying the code locally and independently verifying live deployments.
If you have performed any work recently, you can update the work you performed by including a JSON block at the end of your response:
```json
{
  "work_update": "Description of the work you performed"
}
```
""",
    "deployment": """You are the Deployment Agent. Your role is deploying verified feature branches to pre-prod or promoting to production.
If you have performed any work recently, you can update the work you performed by including a JSON block at the end of your response:
```json
{
  "work_update": "Description of the work you performed"
}
```
""",
    "feedback": """You are the Analytics Feedback Agent. Your role is checking if a shipped feature is measurable and naming success metrics.
If you have performed any work recently, you can update the work you performed by including a JSON block at the end of your response:
```json
{
  "work_update": "Description of the work you performed"
}
```
""",
    "prod_debug": """You are the Production Debugging Agent. Your role is diagnosing production alarms and auto-remediating issues.
If you have performed any work recently, you can update the work you performed by including a JSON block at the end of your response:
```json
{
  "work_update": "Description of the work you performed"
}
```
""",
    "custom": """You are a Custom Agent. Your role is executing one-off sub-tasks that do not fit specialized agents.
If you have performed any work recently, you can update the work you performed by including a JSON block at the end of your response:
```json
{
  "work_update": "Description of the work you performed"
}
```
"""
}

@app.get("/apps/{app_name}/agents/{agent_id}/chat")
async def get_agent_chat(app_name: str, agent_id: str) -> dict:
    if app_name not in registry.list_apps():
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")
    return {"messages": chat_store.get_agent_chat_messages(app_name, agent_id)}

def _chat_agent_label(agent_id: str) -> str:
    if agent_id == "orchestrator":
        return "Orchestrator"
    if agent_id == "prod_debug":
        return "Production Debugging Agent"
    if agent_id == "feedback":
        return "Analytics Feedback Agent"
    if agent_id == "custom":
        return "Custom Agent"
    return agent_id.capitalize() + " Agent"


# Shared by both the buffered and streaming chat endpoints -- the raw text
# a turn produced needs the exact same treatment either way (strip the
# work_update JSON block, persist the work update, record the chat message,
# push it durable in the background), so this is the one place that logic
# lives rather than two copies that could quietly drift.
def _finalize_chat_turn(
    mem: "Memory", repo: Path, app_name: str, agent_id: str, raw_text: str, session_id: str | None,
) -> tuple[str, str | None]:
    from agentra.agents.base import extract_json_block

    response_text = raw_text or "(no response)"
    json_data = extract_json_block(response_text)
    work_update = None
    if json_data and "work_update" in json_data:
        work_update = json_data["work_update"]
        mem.record_work_update(agent_id, work_update)
        response_text = re.sub(r"```json\s*.*?\s*```", "", response_text, flags=re.DOTALL).strip()

    if session_id:
        chat_store.set_agent_session_id(app_name, agent_id, session_id)
    chat_store.record_agent_chat_message(app_name, agent_id, "agent", response_text)

    # Chat itself lives server-side now (chat_store.py, AGENTRA_HOME) --
    # nothing about the conversation lands in the target repo's git
    # history anymore. A work_update is the one thing a chat turn can
    # still write into the target repo's own .agentra/ (Memory, still
    # git-committed), so only push when one was actually recorded --
    # otherwise there's nothing new under .agentra/ for this app and a
    # background git round-trip would be pure overhead.
    if work_update:
        def _persist_chat_in_background() -> None:
            error = registry.persist_agentra_dir(
                repo, _app_branch(app_name), f"agentra: work update from {agent_id!r} for {app_name!r}"
            )
            if error:
                _server_log("chat", f"app={app_name!r} agent={agent_id!r} work update saved locally but failed to push: {error}")

        # Was a blocking git push (commit + push to GitHub) on the critical
        # path of every single chat message -- the human waited on a full
        # network round-trip to GitHub for even a one-word reply. Local
        # writes already happened synchronously above; this only pushes
        # the work_update durable, which doesn't need to block the
        # response the human is waiting on.
        asyncio.get_running_loop().run_in_executor(None, _persist_chat_in_background)

    return response_text, work_update


@app.post("/apps/{app_name}/agents/{agent_id}/chat")
async def post_agent_chat(app_name: str, agent_id: str, payload: ChatPayload) -> dict:
    repo = registry.get_app_repo(app_name)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")

    mem = Memory(repo)
    chat_store.record_agent_chat_message(app_name, agent_id, "human", payload.message)

    from agentra.agents.base import run_agent
    system_prompt = AGENT_CHAT_SYSTEM_PROMPTS.get(agent_id, AGENT_CHAT_SYSTEM_PROMPTS["custom"])

    # Was reconstructing the last 5 messages as a "Human: ...\nAgent: ..."
    # text blob and sending it fresh every turn -- a crude, lossy substitute
    # for what run_agent's resume= now does properly: continue the actual
    # prior CLI session (full context, tool-use history, everything),
    # keyed per (app, agent_id) same as the chat history itself. None on the
    # first message of a conversation -- run_agent starts a fresh session
    # same as any other caller, and stores the new one below either way.
    resume_session_id = chat_store.get_agent_session_id(app_name, agent_id)

    result = await run_agent(
        prompt=payload.message,
        system_prompt=system_prompt,
        cwd=repo,
        # Read-only grounding -- was allowed_tools=[], which made every chat
        # answer a context-blind guess from a 5-message text blob alone, no
        # way to actually check current codebase/state before answering.
        # Not Bash/Write/Edit: this is casual Q&A, not a work session, and
        # the safety hooks (agents/safety.py, applied automatically by
        # run_agent to every caller) only gate those tools anyway --
        # Read/Grep/Glob were never the risk surface.
        allowed_tools=["Read", "Grep", "Glob"],
        # max_turns=1 couldn't fit a tool call *and* a response in the same
        # turn (tool-use -> tool-result -> answer is normally 2+ turns) --
        # was effectively forcing every answer to skip grounding regardless
        # of the tools available. Confirmed live: even 5 wasn't always
        # enough for a genuinely investigative question ("is everything
        # green?" needing several file/log lookups) -- 10 without turning a
        # chat reply into a full agent cycle; _friendly_error_text covers
        # the rare case that's still not enough.
        max_turns=10,
        agent_label=_chat_agent_label(agent_id),
        resume=resume_session_id,
    )

    response_text, work_update = _finalize_chat_turn(mem, repo, app_name, agent_id, result.text, result.session_id)
    return {"response": response_text, "work_update": work_update}


@app.post("/apps/{app_name}/agents/{agent_id}/chat/stream")
async def post_agent_chat_stream(app_name: str, agent_id: str, payload: ChatPayload) -> StreamingResponse:
    """Streaming sibling of POST .../chat -- same grounding/resume/persist
    behavior, but the response arrives as SSE text deltas instead of one
    buffered JSON blob, so the dashboard can render tokens as they're
    generated instead of a multi-second blank wait. POST (not GET, so not a
    plain EventSource on the frontend -- fetch()'s streaming body reader
    works the same over POST) since the human's message is the request
    body, same as the non-streaming endpoint."""
    repo = registry.get_app_repo(app_name)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")

    mem = Memory(repo)
    chat_store.record_agent_chat_message(app_name, agent_id, "human", payload.message)

    from agentra.agents.base import stream_chat_turn
    system_prompt = AGENT_CHAT_SYSTEM_PROMPTS.get(agent_id, AGENT_CHAT_SYSTEM_PROMPTS["custom"])
    resume_session_id = chat_store.get_agent_session_id(app_name, agent_id)

    async def event_stream():
        async for event in stream_chat_turn(
            prompt=payload.message,
            system_prompt=system_prompt,
            cwd=repo,
            allowed_tools=["Read", "Grep", "Glob"],
            max_turns=10,
            agent_label=_chat_agent_label(agent_id),
            resume=resume_session_id,
        ):
            if event["type"] == "delta":
                yield f"data: {json.dumps({'delta': event['text']})}\n\n"
            else:
                response_text, work_update = _finalize_chat_turn(
                    mem, repo, app_name, agent_id, event["text"], event["session_id"]
                )
                yield f"data: {json.dumps({'done': True, 'response': response_text, 'work_update': work_update})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/apps/{app_name}/work-updates")
async def get_work_updates(app_name: str) -> dict:
    repo = registry.get_app_repo(app_name)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")
    mem = Memory(repo)
    return {"updates": mem.get_work_updates()}

@app.post("/apps/{app_name}/agents/{agent_id}/work")
async def post_work_update(app_name: str, agent_id: str, payload: WorkUpdatePayload) -> dict:
    repo = registry.get_app_repo(app_name)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")
    mem = Memory(repo)
    mem.record_work_update(agent_id, payload.description)
    error = registry.persist_agentra_dir(
        repo, _app_branch(app_name), f"agentra: log work update for {agent_id!r} on {app_name!r}"
    )
    if error:
        _server_log("chat", f"app={app_name!r} agent={agent_id!r} work update saved locally but failed to push: {error}")
    return {"ok": True}


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
    # Optional: the VM's local trigger loop (compute.tf) posts one shared
    # tick with no app at all, meant to cover every registered app off a
    # single cron -- see trigger_scheduled below for the fan-out.
    app: str | None = None
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
    its own cadence without a GCP Scheduler job per app.

    payload.app is optional specifically so the VM's local trigger loop
    (compute.tf) can post one bare tick and have it fan out to every
    registered app here, instead of the loop needing to know the app
    roster itself -- previously the loop hardcoded a single app name, so
    any other registered app silently never got scheduled."""
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

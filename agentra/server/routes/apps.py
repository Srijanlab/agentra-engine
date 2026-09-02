"""server/routes/apps.py — app registration, list, configuration, backlog, and loop views."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentra import environments, registry
from agentra.memory import Memory
from agentra.server.utils import _server_log

logger = logging.getLogger(__name__)

router = APIRouter()


class RegisterAppPayload(BaseModel):
    name: str
    repo_url: str
    branch: str = "main"
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
    slack_channel_id: str | None = None


class UpdateAppPayload(BaseModel):
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
    slack_channel_id: str | None = None


class BacklogRequestPayload(BaseModel):
    type: str = "feature_request"
    title: str | None = None
    description: str
    severity: str | None = None


def _mask_stale_machine_testing_notes(content: str | None) -> str | None:
    """GitHub #112: the old run_local wrote its 'Lint:/Typecheck:/Notes:' summary into the
    human-owned architecture/testing-notes key. Read-time mask only -- if `content` matches
    that machine shape, return None (as if unset); otherwise return it verbatim. Pure, no writes.
    Conservative: anything that isn't clearly the machine shape is treated as human text."""
    if content is None:
        return None
    stripped = content.strip()
    if not stripped:
        return content
    non_empty = [ln for ln in stripped.splitlines() if ln.strip()]
    if len(non_empty) < 2:
        return content
    if not re.match(r"^Lint: .+", non_empty[0]) or not re.match(r"^Typecheck: .+", non_empty[1]):
        return content
    # Any further content must start with the machine summary's 'Notes:' line.
    if len(non_empty) > 2 and not non_empty[2].startswith("Notes:"):
        return content
    return None


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

    from agentra.agents.git_ops import GitOpError, commit_and_push

    try:
        commit_and_push(dest, branch, commit_message, [".agentra/"])
        return None
    except GitOpError as exc:
        return str(exc)


async def _app_digest(name: str, info: dict, github_data: dict | None = None) -> tuple[str, dict]:
    repo = Path(info["repo_path"])
    if not repo.exists() and not info.get("repo_url"):
        defaults = environments.EnvironmentConfig()
        return name, {
            "repo_path": info["repo_path"],
            "objective": None,
            "shipped_count": 0,
            "released_count": 0,
            "known_bugs": 0,
            "pre_prod_branch": defaults.pre_prod_branch,
            "prod_branch": defaults.prod_branch,
            "schedule_hours": defaults.schedule_hours,
            "alarm_enabled": defaults.alarm_enabled,
        }
    mem = Memory(repo)
    env_config = environments.load(repo) or environments.EnvironmentConfig()

    if github_data is not None:
        # Use pre-fetched batch data — no GitHub API calls here.
        from agentra.memory.core import _STATUS_SHIPPED_LABEL, _label_names
        open_bugs = [i for i in github_data.get("open_bugs", [])
                     if _STATUS_SHIPPED_LABEL not in i.get("labels", [])]
        # shipped = open features with status:shipped + closed features
        open_shipped = [i for i in github_data.get("open_features", [])
                        if _STATUS_SHIPPED_LABEL in i.get("labels", [])]
        closed_features = github_data.get("closed_features", [])
        shipped_count = len(open_shipped) + len(closed_features)
        # released = closed features with status:done label
        from agentra.memory.core import _STATUS_DONE_LABEL
        released_count = len([i for i in closed_features
                               if _STATUS_DONE_LABEL in i.get("labels", [])])
        known_bugs = len(open_bugs)
    else:
        shipped, bugs = await asyncio.gather(
            asyncio.to_thread(mem.shipped_features), asyncio.to_thread(mem.known_bugs)
        )
        shipped_count = len(shipped)
        released_count = len(mem.released_features())
        known_bugs = len(bugs)

    return name, {
        "repo_path": info["repo_path"],
        "objective": mem.get_objective(),
        "shipped_count": shipped_count,
        "released_count": released_count,
        "known_bugs": known_bugs,
        "pre_prod_branch": env_config.pre_prod_branch,
        "prod_branch": env_config.prod_branch,
        "schedule_hours": env_config.schedule_hours,
        "alarm_enabled": env_config.alarm_enabled,
    }


@router.get("/apps")
async def list_apps() -> dict:
    import hashlib

    from agentra.connectors import github_issues
    from agentra.connectors.github_app import owner_repo_from_url
    from agentra.server.gh_cache import cached

    apps = registry.list_apps()

    repo_url_map: dict[str, str] = {}  # name -> repo_url
    for name, info in apps.items():
        url = info.get("repo_url")
        if url and owner_repo_from_url(url):
            repo_url_map[name] = url

    # One GraphQL call for all apps, cached in Firestore -- the dashboard polls
    # this often and the backlog counts don't change second-to-second.
    batch: dict[str, dict] = {}
    if repo_url_map:
        urls = sorted(repo_url_map.values())
        key = "digest_batch:" + hashlib.sha1(",".join(urls).encode()).hexdigest()[:16]
        try:
            raw = await cached(
                key, lambda: asyncio.to_thread(github_issues.fetch_app_digest_batch, urls), ttl=90
            )
            url_to_name = {v: k for k, v in repo_url_map.items()}
            batch = {url_to_name[u]: d for u, d in (raw or {}).items() if u in url_to_name}
        except Exception:
            batch = {}

    results = await asyncio.gather(
        *(_app_digest(name, info, github_data=batch.get(name)) for name, info in apps.items())
    )
    return {"apps": dict(results)}



@router.post("/apps")
async def register_app(payload: RegisterAppPayload) -> dict:
    if payload.name in registry.list_apps():
        raise HTTPException(status_code=409, detail=f"app {payload.name!r} already registered")

    dest = registry.REPOS_ROOT / payload.name
    if registry._db is None:
        # Local/CLI: clone up front. Cloud mode has no git -- the repo_url +
        # ensure_labels() below is the validation, and the loop clones on demand.
        try:
            from agentra.agents.git_ops import GitOpError, clone_repo

            clone_repo(payload.repo_url, dest, branch=payload.branch)
        except GitOpError as exc:
            _server_log("register", f"app={payload.name!r} clone failed: {exc}")
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    registry.register_app(payload.name, str(dest), repo_url=payload.repo_url, branch=payload.branch)
    if payload.slack_channel_id is not None:
        registry.set_slack_channel(payload.name, payload.slack_channel_id)

    try:
        from agentra.connectors import github_issues

        github_issues.ensure_labels(payload.repo_url)
    except Exception as exc:
        _server_log("register", f"app={payload.name!r} ensure_labels failed: {exc}")

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


@router.get("/apps/{name}")
async def get_app(name: str) -> dict:
    apps = registry.list_apps()
    if name not in apps:
        raise HTTPException(status_code=404, detail=f"app {name!r} not registered")

    from agentra.server.gh_cache import cached

    return await cached(f"app_detail:{name}", lambda: _build_app_detail(name, apps[name]), ttl=60)


async def _build_app_detail(name: str, info: dict) -> dict:
    repo = registry.get_app_repo(name)
    if repo is None:
        raise HTTPException(status_code=409, detail=f"local checkout for {name!r} is missing and could not be recovered")

    mem = Memory(repo)
    env_config = environments.load(repo) or environments.EnvironmentConfig()
    repo_url = info.get("repo_url")

    shipped, released, bugs, closed_bugs, queue, in_progress = await asyncio.gather(
        asyncio.to_thread(mem.shipped_features),
        asyncio.to_thread(mem.released_features),
        asyncio.to_thread(mem.known_bugs),
        asyncio.to_thread(mem.closed_bugs),
        asyncio.to_thread(mem.feature_queue),
        asyncio.to_thread(mem.in_progress_features),
    )

    return {
        "name": name,
        "repo_path": str(repo),
        "repo_url": repo_url,
        "branch": info.get("branch"),
        "objective": mem.get_objective(),
        "shipped_count": len(shipped),
        "released_count": len(released),
        "known_bugs": len(bugs),
        "shipped": shipped,
        "released": released,
        "bugs": bugs,
        "closed_bugs": closed_bugs,
        "feature_queue": queue,
        "in_progress_features": in_progress,
        "vercel": env_config.vercel,
        "firebase": env_config.firebase,
        "ci_cd_on_push": env_config.ci_cd_on_push,
        "pre_prod_branch": env_config.pre_prod_branch,
        "prod_branch": env_config.prod_branch,
        "schedule_hours": env_config.schedule_hours,
        "alarm_enabled": env_config.alarm_enabled,
        "documentation_notes": mem.read("architecture", "documentation"),
        "testing_notes": _mask_stale_machine_testing_notes(mem.read("architecture", "testing-notes")),
        # GitHub issue #84: the Testing Agent's auto-generated local-test summary now
        # lives under its own key (agents/testing.py::run_local), separate from the
        # human-authored testing_notes above -- read-only here, agent-written only,
        # same as codebase/design steering entries.
        "local_test_summary": mem.read("architecture", "local-test-summary"),
        "slack_channel_id": info.get("slack_channel_id"),
    }


@router.patch("/apps/{name}")
async def update_app(name: str, payload: UpdateAppPayload) -> dict:
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
    if payload.slack_channel_id is not None:
        registry.set_slack_channel(name, payload.slack_channel_id)
    _server_log("update", f"app={name!r} configuration updated" + (f" -- push failed: {push_warning}" if push_warning else ""))
    result = {"updated": True, "name": name}
    if push_warning:
        result["warning"] = f"updated, but could not push .agentra/ to the remote: {push_warning}"
    return result


@router.get("/apps/{name}/loops")
async def get_app_loops(name: str) -> dict:
    if name not in registry.list_apps():
        raise HTTPException(status_code=404, detail=f"app {name!r} not registered")
    return {"loops": registry.list_loops(name)}


@router.get("/loops")
async def get_all_loops() -> dict:
    return {"loops": registry.list_loops(None)}


@router.post("/apps/{name}/backlog")
async def submit_backlog_request(name: str, payload: BacklogRequestPayload) -> dict:
    if payload.type not in {"bug", "feature_request"}:
        raise HTTPException(status_code=400, detail="type must be 'bug' or 'feature_request'")
    if not payload.title or not payload.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    if payload.type == "bug" and payload.severity not in {"critical", "high", "medium", "low"}:
        raise HTTPException(status_code=400, detail="bug severity must be critical, high, medium, or low")
    try:
        request_id = registry.submit_request(
            app=name,
            request_type=payload.type,
            title=payload.title.strip(),
            description=payload.description,
            severity=payload.severity if payload.type == "bug" else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    registry.dispatch_once()
    _server_log("queue", f"request_id={request_id} app={name!r} type={payload.type!r} -- submitted from dashboard")
    return {"submitted": True, "request_id": request_id}


@router.post("/apps/{name}/feature-requests")
async def submit_feature_request(name: str, payload: BacklogRequestPayload) -> dict:
    payload.type = "feature_request"
    payload.severity = None
    payload.title = payload.title.strip() if payload.title and payload.title.strip() else payload.description
    return await submit_backlog_request(name, payload)

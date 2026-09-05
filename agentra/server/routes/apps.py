"""server/routes/apps.py — app registration, list, configuration, backlog, and loop views."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentra import environments, registry
from agentra.memory import Memory
from agentra.server.utils import _server_log

logger = logging.getLogger(__name__)

router = APIRouter()


class RepoEntryPayload(BaseModel):
    name: str
    repo_url: str
    branch: str = "main"
    role: str = "code"
    deploy_strategy: str | None = None


class RegisterAppPayload(BaseModel):
    name: str
    repo_url: str | None = None
    branch: str = "main"
    objective: str | None = None
    vercel: bool | None = None
    firebase: bool | None = None
    ci_cd_on_push: bool | None = None
    pre_prod_branch: str | None = None
    prod_branch: str | None = None
    schedule_hours: float | None = None
    alarm_enabled: bool | None = None
    slack_channel_id: str | None = None
    repos: list[RepoEntryPayload] | None = None


class UpdateAppPayload(BaseModel):
    objective: str | None = None
    vercel: bool | None = None
    firebase: bool | None = None
    ci_cd_on_push: bool | None = None
    pre_prod_branch: str | None = None
    prod_branch: str | None = None
    schedule_hours: float | None = None
    alarm_enabled: bool | None = None
    slack_channel_id: str | None = None


class BacklogRequestPayload(BaseModel):
    type: str = "feature_request"
    title: str | None = None
    description: str
    severity: str | None = None


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

    if registry.cloud_mode():
        return None  # cloud: no local checkout to commit .agentra/ from

    from agentra.agents.git_ops import GitOpError, commit_and_push

    try:
        commit_and_push(dest, branch, commit_message, [".agentra/"])
        return None
    except GitOpError as exc:
        return str(exc)


def _coord_view(name: str, info: dict) -> dict:
    """{"repo_path", "repo_url", "branch"} sourced from the coordination repo --
    a legacy single-repo entry has these at the top level already; a multi-repo
    entry ({"repos": [...]}) has none, so resolve its coordination repo instead."""
    if "repos" not in info:
        return {"repo_path": info.get("repo_path"), "repo_url": info.get("repo_url"), "branch": info.get("branch")}
    spec = registry.get_coordination_repo(name)
    if spec is None:
        return {"repo_path": None, "repo_url": None, "branch": None}
    return {"repo_path": str(spec.path) if spec.path else None, "repo_url": spec.repo_url, "branch": spec.branch}


async def _app_digest(name: str, info: dict, github_data: dict | None = None) -> tuple[str, dict]:
    try:
        return await _app_digest_inner(name, info, github_data)
    except Exception:
        # One app failing (missing GitHub creds, API blip) must not 500 the whole list.
        logger.warning("app digest failed for %s", name, exc_info=True)
        d = environments.EnvironmentConfig()
        return name, {
            "repo_path": info.get("repo_path"), "objective": None,
            "shipped_count": 0, "released_count": 0, "known_bugs": 0,
            "pre_prod_branch": d.pre_prod_branch, "prod_branch": d.prod_branch,
            "schedule_hours": d.schedule_hours, "alarm_enabled": d.alarm_enabled,
            "digest_error": True,
        }


async def _app_digest_inner(name: str, info: dict, github_data: dict | None = None) -> tuple[str, dict]:
    view = _coord_view(name, info)
    repo = Path(view["repo_path"]) if view["repo_path"] else None
    if (repo is None or not repo.exists()) and not view["repo_url"]:
        defaults = environments.EnvironmentConfig()
        return name, {
            "repo_path": view["repo_path"],
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
        "repo_path": view["repo_path"],
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

    repo_url_map: dict[str, str] = {}  # name -> coordination repo_url
    for name, info in apps.items():
        url = _coord_view(name, info)["repo_url"]
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



async def _register_multi_repo_app(payload: RegisterAppPayload) -> dict:
    """A multi-repo app: N code repos plus exactly one coordination repo (issues,
    .agentra/memory, objective -- no deployable code). Each code repo's own deploy
    config (vercel/firebase/branches/ci_cd_on_push) lives in that repo's own GitHub
    Actions Variables, read later by the cycle -- this call only sets app-level
    config (objective, schedule) on the coordination repo."""
    coord = next((r for r in payload.repos if r.role == "coordination"), None)
    if coord is None:
        raise HTTPException(status_code=400, detail="repos must include exactly one entry with role='coordination'")

    coord_dest = registry.REPOS_ROOT / payload.name / coord.name
    if not registry.cloud_mode():
        from agentra.agents.git_ops import GitOpError, clone_repo

        for r in payload.repos:
            repo_dest = registry.REPOS_ROOT / payload.name / r.name
            try:
                clone_repo(r.repo_url, repo_dest, branch=r.branch)
            except GitOpError as exc:
                _server_log("register", f"app={payload.name!r} repo={r.name!r} clone failed: {exc}")
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    registry.register_app(payload.name, repos=[r.model_dump() for r in payload.repos])
    if payload.slack_channel_id is not None:
        registry.set_slack_channel(payload.name, payload.slack_channel_id)

    try:
        from agentra.connectors import github_issues

        github_issues.ensure_labels(coord.repo_url)
    except Exception as exc:
        _server_log("register", f"app={payload.name!r} ensure_labels failed: {exc}")

    push_warning = _apply_app_config(
        coord_dest,
        coord.branch,
        objective=payload.objective,
        vercel=None,
        firebase=None,
        ci_cd_on_push=None,
        pre_prod_branch=None,
        prod_branch=None,
        schedule_hours=payload.schedule_hours,
        alarm_enabled=payload.alarm_enabled,
        detect_defaults=False,
        commit_message="agentra: register multi-repo app (objective/schedule)",
    )
    if push_warning:
        _server_log("register", f"app={payload.name!r} registered, but persisting .agentra/ failed: {push_warning}")

    _server_log(
        "register",
        f"app={payload.name!r} repos={[r.name for r in payload.repos]!r} coordination={coord.name!r} -- registered",
    )
    result = {"registered": True, "name": payload.name, "repos": [r.model_dump() for r in payload.repos]}
    if push_warning:
        result["warning"] = f"registered, but could not push .agentra/ to the remote: {push_warning}"
    return result


@router.post("/apps")
async def register_app(payload: RegisterAppPayload) -> dict:
    if payload.name in registry.list_apps():
        raise HTTPException(status_code=409, detail=f"app {payload.name!r} already registered")

    if payload.repos:
        return await _register_multi_repo_app(payload)

    if not payload.repo_url:
        raise HTTPException(status_code=400, detail="repo_url is required (or pass repos= for a multi-repo app)")

    dest = registry.REPOS_ROOT / payload.name
    if not registry.cloud_mode():
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


@router.delete("/apps/{name}")
async def delete_app(name: str) -> dict:
    if not registry.remove_app(name):
        raise HTTPException(status_code=404, detail=f"app {name!r} not registered")
    _server_log("register", f"app={name!r} -- removed")
    return {"removed": True, "name": name}


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
    view = _coord_view(name, info)

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
        "repo_url": view["repo_url"],
        "branch": view["branch"],
        "repos": info.get("repos"),
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
        # GitHub issue #84: the Testing Agent's auto-generated local-test summary,
        # read-only here, agent-written only (same as codebase/design steering entries).
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

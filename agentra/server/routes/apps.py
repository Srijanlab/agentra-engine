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


async def _app_digest(name: str, info: dict) -> tuple[str, dict]:
    repo = Path(info["repo_path"])
    if not repo.exists():
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
    shipped, bugs = await asyncio.gather(
        asyncio.to_thread(mem.shipped_features), asyncio.to_thread(mem.known_bugs)
    )
    return name, {
        "repo_path": info["repo_path"],
        "objective": mem.get_objective(),
        "shipped_count": len(shipped),
        "released_count": len(mem.released_features()),
        "known_bugs": len(bugs),
        "pre_prod_branch": env_config.pre_prod_branch,
        "prod_branch": env_config.prod_branch,
        "schedule_hours": env_config.schedule_hours,
        "alarm_enabled": env_config.alarm_enabled,
    }


@router.get("/apps")
async def list_apps() -> dict:
    apps = registry.list_apps()
    results = await asyncio.gather(*(_app_digest(name, info) for name, info in apps.items()))
    return {"apps": dict(results)}


@router.post("/apps")
async def register_app(payload: RegisterAppPayload) -> dict:
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
    info = apps[name]

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
        "testing_notes": mem.read("architecture", "testing-notes"),
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

"""server/__init__.py — FastAPI application initialization and routing."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

from agentra.server.auth import CORS_ORIGIN_REGEX, auth_middleware, auth_status, log_startup_warnings
from agentra.server.queue_auth import QueueAuthError, queue_auth_error_handler

from agentra import registry
from agentra.agents import catalog as agents_catalog
from agentra.memory import Memory
from agentra.server.state import _active_runs, _app_locks
from agentra.server.utils import _strip_log_timestamp

logger = logging.getLogger("agentra.server")

from agentra import observability  # noqa: E402

observability.init_observability()

app = FastAPI(title="agentra orchestrator")
app.add_exception_handler(QueueAuthError, queue_auth_error_handler)
log_startup_warnings()


# Order matters: CORS added last == outermost, so it answers preflight and
# attaches headers even to the auth gate's 401/403.
app.middleware("http")(auth_middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """Return a CORS-able JSON 500 instead of a bare ASGI crash (which reaches the
    browser as an opaque 'NetworkError' with no CORS headers)."""
    logger.error("unhandled error on %s: %s", request.url.path, exc, exc_info=True)
    from starlette.responses import JSONResponse

    detail = "quota exceeded -- try again shortly" if "429" in str(exc) else f"{type(exc).__name__}"
    return JSONResponse({"detail": f"internal error: {detail}"}, status_code=500)


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
    from agentra.artifacts import screenshot_path

    return screenshot_path(repo, run_key)


def _run_report_path(run_key: str) -> Path | None:
    run = _active_runs.get(run_key) or registry.get_run(run_key)
    if run is None:
        return None
    repo = registry.get_app_repo(run["app"])
    if repo is None:
        return None
    from agentra.artifacts import report_path

    return report_path(repo, run_key)


def _build_commit() -> str:
    """Deployed git SHA (Vercel-injected, else AGENTRA_BUILD_SHA), or "" when unknown."""
    return os.environ.get("VERCEL_GIT_COMMIT_SHA") or os.environ.get("AGENTRA_BUILD_SHA") or ""


def _dashboard_url() -> str:
    """The configured dashboard URL when it is an http(s) URL, else ""."""
    url = (os.environ.get("AGENTRA_DASHBOARD_URL") or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else ""


@app.get("/", response_model=None)
async def root() -> Response | dict:
    """Redirect to the configured dashboard, else return the API-service descriptor."""
    dashboard_url = _dashboard_url()
    if dashboard_url:
        return RedirectResponse(dashboard_url, status_code=307)
    return {"service": "agentra-engine", "status": "ok", "commit": _build_commit(), "health": "/health"}


@app.get("/favicon.ico", response_model=None)
@app.get("/favicon.svg", response_model=None)
async def favicon() -> Response:
    """The engine ships no favicon, so answer browsers' probe with 204."""
    return Response(status_code=204)


@app.get("/health")
@app.get("/healthz")
async def health() -> dict:
    """GitHub #113: /healthz is a pure alias so probes using either convention succeed.
    `commit` is the deployed build's git SHA (Vercel injects VERCEL_GIT_COMMIT_SHA) --
    the loop's verify_pre_prod uses it to confirm a pre-prod deploy has caught up
    before the Testing Agent runs."""
    commit = _build_commit()
    auth = auth_status().as_dict()
    try:
        return {"status": "ok", "apps_registered": len(registry.list_apps()), "commit": commit, "auth": auth}
    except Exception as exc:  # never let a backend blip fail the liveness probe
        return {"status": "degraded", "error": f"{type(exc).__name__}", "commit": commit, "auth": auth}


@app.get("/debug/dynamodb")
async def debug_dynamodb() -> dict:
    """Diagnose the DynamoDB path (static IAM keys). No secrets returned."""
    from agentra import registry

    out = {
        "table_prefix": os.environ.get("AGENTRA_DYNAMODB_TABLE_PREFIX"),
        "region": os.environ.get("AGENTRA_AWS_REGION"),
        "access_key_id_set": bool(os.environ.get("AGENTRA_AWS_ACCESS_KEY_ID")),
        "secret_access_key_set": bool(os.environ.get("AGENTRA_AWS_SECRET_ACCESS_KEY")),
        "db_connected": registry.dynamodb_resource() is not None,
    }
    ddb = registry.dynamodb_resource()
    if ddb is not None:
        try:
            from agentra.registry import _dynamo

            item = _dynamo.get_item(_dynamo.table("system"), {"key": "pause"})
            out["system_pause_item_present"] = item is not None
        except Exception as exc:
            out["read_error"] = f"{type(exc).__name__}: {exc}"[:400]
    return out


@app.get("/runs/{run_key}/logs")
async def stream_run_logs(run_key: str) -> StreamingResponse:
    run = _active_runs.get(run_key) or registry.get_run(run_key)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run_key")

    log_path = _run_log_path(run_key)
    if log_path is None:
        raise HTTPException(status_code=409, detail="local checkout for this run is missing and could not be recovered")

    async def event_stream():
        if not log_path.exists():
            ddb = registry.dynamodb_resource()
            if ddb is not None:
                from agentra.registry import _dynamo

                item = _dynamo.get_item(_dynamo.table("run-logs"), {"run_id": run_key})
                if item is not None:
                    for line in item.get("lines", []):
                        yield f"data: {json.dumps({'line': _strip_log_timestamp(line)})}\n\n"
                    yield "event: done\ndata: {}\n\n"
                    return

        offset = 0
        terminal_cycles = 0
        while True:
            if log_path.exists():
                lines = log_path.read_text().splitlines()
                for line in lines[offset:]:
                    yield f"data: {json.dumps({'line': _strip_log_timestamp(line)})}\n\n"
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
    run = _active_runs.get(run_key) or registry.get_run(run_key)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run_key")
    path = _run_screenshot_path(run_key)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="no screenshot captured for this run")
    return FileResponse(path, media_type="image/png")


@app.get("/runs/{run_key}/test-report")
async def get_run_test_report(run_key: str) -> dict:
    """Structured, itemized test-case results from the Testing Agent's live pre-prod verification (agents/testing.py's run_pre_prod) -- the data behind the dashboard's 'Review promotion' panel, so a human sees each acceptance criterion's pass/fail (plus the reachability check) before clicking Promote, not just a single aggregate verdict."""
    run = _active_runs.get(run_key) or registry.get_run(run_key)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run_key")
    path = _run_report_path(run_key)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="no test report available for this run")
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        raise HTTPException(status_code=404, detail="no test report available for this run")


@app.get("/agents/metadata")
async def list_agent_metadata() -> dict:
    return {"agents": agents_catalog.AGENT_METADATA, "permission_model_note": agents_catalog.PERMISSION_MODEL_NOTE}


# Include all sub-routers at root prefix
from agentra.server.routes.systems import router as systems_router  # noqa: E402
from agentra.server.routes.connectors import router as connectors_router  # noqa: E402
from agentra.server.routes.apps import router as apps_router  # noqa: E402
from agentra.server.routes.schedule import router as schedule_router  # noqa: E402
from agentra.server.routes.loops import router as loops_router  # noqa: E402
from agentra.server.routes.standup import router as standup_router  # noqa: E402
from agentra.server.routes.chat import router as chat_router  # noqa: E402
from agentra.server.routes.triggers import router as triggers_router  # noqa: E402
from agentra.server.routes.human_input import router as human_input_router  # noqa: E402
from agentra.server.routes.review import router as review_router  # noqa: E402
from agentra.server.routes.internal import router as internal_router  # noqa: E402
from agentra.a2a.routes import router as a2a_router  # noqa: E402

app.include_router(systems_router)
app.include_router(connectors_router)
app.include_router(apps_router)
app.include_router(schedule_router)
app.include_router(loops_router)
app.include_router(standup_router)
app.include_router(chat_router)
app.include_router(triggers_router)
app.include_router(human_input_router)
app.include_router(review_router)
app.include_router(internal_router)
app.include_router(a2a_router)

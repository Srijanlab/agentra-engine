"""server/__init__.py — FastAPI application initialization and routing."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agentra import registry
from agentra.agents import catalog as agents_catalog
from agentra.agents import deployment
from agentra.memory import Memory
from agentra.server.state import _active_runs, _app_locks
from agentra.server.utils import _strip_log_timestamp
from agentra.server.routes.chat import AGENT_VOICES
from agentra.server.routes.triggers import _record_production_release, _branch_head_sha, _run_promote_background

logger = logging.getLogger("agentra.server")

app = FastAPI(title="agentra orchestrator")


@app.middleware("http")
async def _refresh_oidc_token(request, call_next):
    # Fluid Compute delivers the OIDC JWT as a per-request header; sync it to the
    # file identity_pool reads, then lazily build the Firestore client (it can't
    # exist at import -- no token yet).
    from agentra import registry

    registry.sync_oidc_token_file(request.headers.get("x-vercel-oidc-token"))
    registry.ensure_firestore()
    return await call_next(request)


WEB_DIST = Path(os.environ.get("AGENTRA_WEB_DIST") or (Path(__file__).resolve().parent.parent / "web" / "dist"))

if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="web-assets")

FAVICON = WEB_DIST / "favicon.svg"


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


def _run_report_path(run_key: str) -> Path | None:
    run = _active_runs.get(run_key) or registry.get_run(run_key)
    if run is None:
        return None
    repo = registry.get_app_repo(run["app"])
    if repo is None:
        return None
    from agentra.agents.testing import report_path

    return report_path(repo, run_key)


@app.get("/", response_model=None)
async def dashboard() -> FileResponse | dict:
    index = WEB_DIST / "index.html"
    if not index.exists():
        return {
            "error": "dashboard not built",
            "hint": "run `npm install && npm run build` in agentra/web/, or set AGENTRA_WEB_DIST",
        }
    return FileResponse(index)


@app.get("/favicon.ico", response_model=None)
@app.get("/favicon.svg", response_model=None)
async def favicon() -> FileResponse:
    """GitHub #127: serve the built dashboard favicon so browsers' /favicon.ico request does not 404."""
    if not FAVICON.exists():
        raise HTTPException(status_code=404, detail="favicon not built")
    return FileResponse(FAVICON, media_type="image/svg+xml")


@app.get("/health")
@app.get("/healthz")
async def health() -> dict:
    """GitHub #113: /healthz is a pure alias so probes using either convention succeed."""
    return {"status": "ok", "apps_registered": len(registry.list_apps())}


@app.get("/debug/firestore")
async def debug_firestore(request: Request) -> dict:
    """Diagnose the keyless Firestore path (Vercel OIDC -> WIF). No secrets returned."""
    from agentra import registry

    out = {
        "vercel_oidc_token_present": bool(os.environ.get("VERCEL_OIDC_TOKEN")),
        "wif_config_present": bool(os.environ.get("GCP_WORKLOAD_IDENTITY_CONFIG")),
        "firestore_project": os.environ.get("AGENTRA_FIRESTORE_PROJECT"),
        "db_connected": registry.firestore_client() is not None,
        "github_token_loaded": bool(os.environ.get("GITHUB_TOKEN")),
        "vercel_env_keys": sorted(k for k in os.environ if k.startswith(("VERCEL", "AWS", "GOOGLE", "GCP"))),
        "vercel_headers": sorted(h for h in request.headers if h.lower().startswith(("x-vercel", "x-oidc"))),
    }
    from agentra.registry.core import _secret_hydration_status
    out["secret_hydration"] = dict(_secret_hydration_status)
    db = registry.firestore_client()
    if db is not None:
        try:
            out["apps_doc_count"] = sum(1 for _ in db.collection("apps").limit(20).stream())
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
            db = registry.firestore_client()
            if db is not None:
                doc = db.collection("run_logs").document(run_key).get()
                if doc.exists:
                    for line in doc.to_dict().get("lines", []):
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
from agentra.server.routes.standup import router as standup_router  # noqa: E402
from agentra.server.routes.chat import router as chat_router  # noqa: E402
from agentra.server.routes.triggers import router as triggers_router  # noqa: E402
from agentra.server.routes.human_input import router as human_input_router  # noqa: E402
from agentra.server.routes.review import router as review_router  # noqa: E402
from agentra.server.routes.slack import router as slack_router  # noqa: E402
from agentra.a2a.routes import router as a2a_router  # noqa: E402

app.include_router(systems_router)
app.include_router(connectors_router)
app.include_router(apps_router)
app.include_router(standup_router)
app.include_router(chat_router)
app.include_router(triggers_router)
app.include_router(human_input_router)
app.include_router(review_router)
app.include_router(slack_router)
app.include_router(a2a_router)

"""Vercel entrypoint — serves the agentra-engine FastAPI app as one function.
All routes are rewritten here by vercel.json."""

import pathlib
import sys
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from agentra.server import app  # noqa: F401  (ASGI app Vercel detects)
except Exception:  # surface import failures instead of an opaque crash
    _tb = traceback.format_exc()
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.get("/health")
    @app.get("/healthz")
    async def _health() -> JSONResponse:
        return JSONResponse({"status": "degraded", "reason": "import failed"}, status_code=503)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
    async def _import_error(path: str) -> JSONResponse:
        return JSONResponse(
            {"error": "engine import failed", "traceback": _tb.splitlines()[-25:]},
            status_code=500,
        )

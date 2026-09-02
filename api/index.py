"""Vercel entrypoint — serves the agentra-engine FastAPI app as one function.
All routes are rewritten here by vercel.json."""

import pathlib
import sys
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _load_app():
    try:
        from agentra.server import app as real_app

        return real_app
    except Exception:  # surface import failures instead of an opaque crash
        tb = traceback.format_exc()
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse

        fallback = FastAPI()

        @fallback.get("/health")
        @fallback.get("/healthz")
        async def _health() -> JSONResponse:
            return JSONResponse({"status": "degraded", "reason": "import failed"}, status_code=503)

        @fallback.api_route("/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
        async def _import_error(path: str) -> JSONResponse:
            return JSONResponse(
                {"error": "engine import failed", "traceback": tb.splitlines()[-25:]},
                status_code=500,
            )

        return fallback


app = _load_app()  # top-level binding Vercel's ASGI detection needs

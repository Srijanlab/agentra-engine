"""Vercel entrypoint — serves the agentra-engine FastAPI app as one function.
All routes are rewritten here by vercel.json."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agentra.server import app  # noqa: E402  (ASGI app Vercel detects)

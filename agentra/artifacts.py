"""agentra.artifacts — paths for a run's test artifacts under `.agentra/test_artifacts/`.

Just path builders, shared by the Testing Agent (which writes them, agentra-loop)
and the dashboard's Review-Promotion panel (which reads them back).
"""

from __future__ import annotations

from pathlib import Path


def screenshot_path(repo: Path, run_id: str) -> Path:
    return repo / ".agentra" / "test_artifacts" / run_id / "screenshot.png"


def report_path(repo: Path, run_id: str) -> Path:
    """The structured (JSON) test-report artifact for a run's live pre-prod
    verification -- same directory as screenshot_path and the human-readable
    report.md, so a reviewer has the per-criterion breakdown, the prose summary,
    and the screenshot in one place."""
    return repo / ".agentra" / "test_artifacts" / run_id / "report.json"

"""server/routes/review.py — the dashboard's Backlog board and Ready to Review tabs."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException

from agentra import registry
from agentra.memory import Memory

logger = logging.getLogger(__name__)

router = APIRouter()


def _repo_or_404(name: str):
    apps = registry.list_apps()
    if name not in apps:
        raise HTTPException(status_code=404, detail=f"app {name!r} not registered")
    repo = registry.get_app_repo(name)
    if repo is None:
        raise HTTPException(status_code=409, detail=f"local checkout for {name!r} is missing and could not be recovered")
    return repo


def _attach_run_ids(mem: Memory, item: dict) -> dict:
    """Every run_id that has ever worked this item's issue (1:N -- an issue routinely gets
    resumed across several separate runs), newest-first. Empty for an item nothing has
    touched yet (the not-started buckets)."""
    external_id = item.get("external_id")
    return {**item, "run_ids": mem.run_ids_for(str(external_id)) if external_id else []}


async def _attach_run_ids_to_all(mem: Memory, items: list[dict]) -> list[dict]:
    return list(await asyncio.gather(*(asyncio.to_thread(_attach_run_ids, mem, item) for item in items)))


@router.get("/apps/{name}/backlog-board")
async def get_backlog_board(name: str) -> dict:
    """Kanban-style view of the backlog: not-started bugs/features, everything with real work
    already in flight (single-part status:in-progress items plus standing multi-part sub-issue
    features), and code-complete items awaiting merge -- the same buckets/priority check_backlog
    presents to the orchestrator, surfaced for a human to see directly. Every item carries its
    full run_ids history (1:N), not just the single most-recent run."""
    from agentra.server.gh_cache import cached

    return await cached(f"backlog_board:{name}", lambda: _build_backlog_board(name), ttl=45)


async def _build_backlog_board(name: str) -> dict:
    repo = _repo_or_404(name)
    mem = Memory(repo)

    bugs, features, in_progress_single, in_progress_multi, code_complete = await asyncio.gather(
        asyncio.to_thread(mem.known_bugs),
        asyncio.to_thread(mem.feature_queue),
        asyncio.to_thread(mem.in_progress_items),
        asyncio.to_thread(mem.in_progress_features),
        asyncio.to_thread(mem.code_complete_items),
    )
    in_progress_ids = {item["external_id"] for item in in_progress_single if item.get("external_id")}
    not_started_bugs = [b for b in bugs if not b.get("needs_human") and b.get("external_id") not in in_progress_ids]
    not_started_features = [f for f in features if f.get("external_id") not in in_progress_ids]

    (
        not_started_bugs, not_started_features, in_progress_single, in_progress_multi, code_complete,
    ) = await asyncio.gather(
        _attach_run_ids_to_all(mem, not_started_bugs),
        _attach_run_ids_to_all(mem, not_started_features),
        _attach_run_ids_to_all(mem, in_progress_single),
        _attach_run_ids_to_all(mem, in_progress_multi),
        _attach_run_ids_to_all(mem, code_complete),
    )

    return {
        "not_started": {"bugs": not_started_bugs, "features": not_started_features},
        "in_progress": {"single_part": in_progress_single, "multi_part": in_progress_multi},
        "code_complete": code_complete,
    }


@router.get("/apps/{name}/ready-to-review")
async def get_ready_to_review(name: str) -> dict:
    """Every status:tested bug/feature -- live-verified against pre-prod, one Promote away from
    production -- each with its own itemized test report attached (moved out of the per-run
    detail drawer so a reviewer sees every pending item's own verification in one consolidated
    view, not just whichever run they happen to click into) and its full run_ids history (1:N)."""
    from agentra.server.gh_cache import cached

    return await cached(f"ready_to_review:{name}", lambda: _build_ready_to_review(name), ttl=45)


async def _build_ready_to_review(name: str) -> dict:
    repo = _repo_or_404(name)
    mem = Memory(repo)
    items = await asyncio.to_thread(mem.tested_items)

    from agentra.agents.testing import report_path

    def _attach_report(item: dict) -> dict:
        run_ids = mem.run_ids_for(str(item.get("external_id"))) if item.get("external_id") else []
        report = None
        if run_ids:
            path = report_path(repo, run_ids[0])  # newest run -- the one most likely to still be relevant
            if path.exists():
                try:
                    report = json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    report = None
        return {**item, "run_ids": run_ids, "test_report": report}

    reports = await asyncio.gather(*(asyncio.to_thread(_attach_report, item) for item in items))
    return {"items": list(reports)}

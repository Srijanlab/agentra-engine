"""server/routes/human_input.py — human-in-the-loop escalation answer
channel (GitHub issue #34).

Two answer channels resolve a HUMAN_INPUT_REQUIRED escalation, and both
funnel through dispatch_human_answer() below so a resume always behaves
identically regardless of which one a human used:

1. The dashboard's "Needs your input" panel -- POST /apps/{app}/human-input,
   this module's own route.
2. A plain comment on the needs_human GitHub issue -- polled, not pushed
   (see this module's docstring on why there is no inbound webhook here),
   from server/routes/triggers.py's /trigger/scheduled handler, which
   already ticks every ~15 minutes per compute.tf's agentra-trigger-loop
   with no new infra. See _reconcile_human_input in triggers.py.

Resuming reuses the original branch + session_id via run_autonomous_cycle's
human_answer/human_answer_issue params (agents/brain/__init__.py) -- NOT a
brand new implement_feature call starting from scratch. See
agents/brain/tools.py's implement_feature/_format_spec for how the answer
gets woven back into the resumed Implementation Agent turn.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentra import environments, registry
from agentra.agents.brain import run_autonomous_cycle
from agentra.memory import Memory
from agentra.server.utils import _lock_for, _paused_response, _server_log, _set_run

logger = logging.getLogger(__name__)

router = APIRouter()


class HumanInputAnswerPayload(BaseModel):
    issue_number: int
    answer: str


def _feature_hint_for_resume(context: dict) -> str:
    """A strong, explicit instruction (same "feature hint" mechanism
    run_autonomous_cycle already exposes for cli.py's --feature) telling the
    brain exactly which in-progress item to continue and how -- on top of,
    not instead of, check_backlog already listing in-progress work first and
    session.human_answer/human_answer_issue getting woven into the matching
    implement_feature call's spec (see tools.py). Without a tracking_issue
    (e.g. the original escalation had no resolves_id/sub_feature_of --
    should not happen for implement_feature's own escalations, but
    defensive since context is read back from a GitHub issue comment, not a
    typed record) this falls back to a generic nudge."""
    tracking_issue = context.get("tracking_issue")
    branch = context.get("branch")
    if tracking_issue and branch:
        return (
            f"A human has just answered a blocking question for issue #{tracking_issue} on "
            f"branch {branch!r}. Call implement_feature now with resume_branch={branch!r} and "
            f"resolves_id={str(tracking_issue)!r} (or sub_feature_of, whichever matches how that "
            "issue was originally being worked) to continue that exact interrupted call using "
            "the human's answer -- do not start a new branch or new work first."
        )
    return (
        "A human has just answered a blocking question raised earlier this run. Call "
        "check_backlog to see what was in progress and continue it."
    )


async def _run_human_resume_background(run_key: str, app_name: str, repo: Path, context: dict, answer: str) -> None:
    lock = _lock_for(app_name)
    async with lock:
        _set_run(run_key, status="running")
        try:
            env = environments.load(repo) or environments.EnvironmentConfig()
            objective = Memory(repo).get_objective() or ""
            report = await run_autonomous_cycle(
                repo,
                objective,
                env,
                feature=_feature_hint_for_resume(context),
                run_id=run_key,
                human_answer=answer,
                human_answer_issue=context.get("tracking_issue"),
            )
            _set_run(
                run_key,
                status="waiting_for_human" if report.waiting_for_human else "completed",
                result={
                    "run_id": report.run_id,
                    "actions": report.actions,
                    "final_message": report.final_message,
                    "cost_usd": report.cost_usd,
                    "feature": report.feature,
                },
            )
            _server_log(
                "human-input",
                f"app={app_name!r} run_key={run_key} agentra_run_id={report.run_id} resumed | "
                f"waiting_for_human={report.waiting_for_human} cost=${report.cost_usd:.4f}",
            )
        except Exception as exc:
            _set_run(run_key, status="failed", error=str(exc))
            _server_log("human-input", f"app={app_name!r} run_key={run_key} raised: {exc!r}")


def dispatch_human_answer(app_name: str, repo: Path, issue_number: int, answer: str, *, source: str) -> dict:
    """Records `answer` on the needs_human issue (removing the needs_human
    label -- Memory.record_human_answer) and dispatches a resume in the
    background that reuses the original branch/session_id. Shared by the
    dashboard POST route below and triggers.py's GitHub-issue-comment
    poller so both answer channels behave identically.

    Also flips any run record still sitting in waiting_for_human for this
    exact issue to 'answered' synchronously, before returning -- so a
    dashboard poll of /needs-human immediately after this call already
    shows it gone, per GitHub issue #34's acceptance criterion that the
    status change is observable "within the same request/short poll
    window", not just eventually once the resumed cycle finishes.

    Raises ValueError if this issue has no recorded human-input context
    (never escalated through _escalate_to_human, or GitHub was unreachable
    when it originally was) -- callers translate that into whatever's right
    for their own transport (404 for the HTTP route, a skipped/logged
    no-op for the poller)."""
    mem = Memory(repo)
    context = mem.get_human_input_context(issue_number)
    if context is None:
        raise ValueError(f"no human-input context recorded for issue #{issue_number}")

    for run in registry.list_waiting_for_human():
        human_input = run.get("human_input") or {}
        if run.get("app") == app_name and human_input.get("issue_number") == issue_number:
            registry.record_run(run["run_key"], status="answered")

    objective = mem.get_objective() or ""
    run_key = uuid.uuid4().hex[:8]
    registry.record_run(
        run_key,
        app=app_name,
        source=source,
        status="queued",
        started_at=time.time(),
        objective=objective,
        loop_id=registry.loop_id_for(objective),
    )
    mem.record_human_answer(issue_number, answer, resumed_run_key=run_key)
    _server_log(source, f"app={app_name!r} issue=#{issue_number} run_key={run_key} -- human answer accepted, resuming")
    asyncio.create_task(_run_human_resume_background(run_key, app_name, repo, context, answer))
    return {"run_key": run_key, "branch": context.get("branch"), "session_id": context.get("session_id")}


@router.post("/apps/{app_name}/human-input")
async def submit_human_input(app_name: str, payload: HumanInputAnswerPayload) -> dict:
    if registry.is_paused():
        return _paused_response("human-input")
    repo = registry.get_app_repo(app_name)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")
    try:
        dispatched = dispatch_human_answer(app_name, repo, payload.issue_number, payload.answer, source="human-input")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"accepted": True, **dispatched}


@router.get("/needs-human")
async def list_needs_human() -> dict:
    """Backs the dashboard's 'Needs your input' panel -- every run
    currently waiting_for_human or escalated (past its max-wait timeout,
    see registry.reconcile_waiting_for_human), across every registered
    app."""
    return {"runs": registry.list_waiting_for_human()}

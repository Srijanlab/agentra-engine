"""server/routes/human_input.py — human-in-the-loop escalation answer channel (GitHub issue #34)."""

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
    """A strong, explicit instruction (same "feature hint" mechanism run_autonomous_cycle already exposes for cli.py's --feature) telling the brain exactly which in-progress item to continue and how -- on top of, not instead of, check_backlog already listing in-progress work first and session.human_answer/human_answer_issue getting woven into the matching implement_feature call's spec (see tools.py)."""
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
                ended_at=time.time(),
                cost_usd=report.cost_usd,
                summary=report.final_message,
                feature=report.feature,
            )
            _server_log(
                "human-input",
                f"app={app_name!r} run_key={run_key} agentra_run_id={report.run_id} resumed | "
                f"waiting_for_human={report.waiting_for_human} cost=${report.cost_usd:.4f}",
            )
        except Exception as exc:
            _set_run(run_key, status="failed", ended_at=time.time(), error=str(exc), summary=str(exc)[:2000])
            _server_log("human-input", f"app={app_name!r} run_key={run_key} raised: {exc!r}")


_SOURCE_LABEL = {"human-input": "the dashboard", "slack": "Slack", "github-comment": "a GitHub comment"}


def _ack_slack_thread(app_name: str, issue_number: int, answer: str, source: str) -> None:
    """Reflect the resolution back into the Slack thread so a watcher there sees it was
    handled -- whichever channel the answer actually came from (GitHub issue #68). A reply
    that came in via Slack itself is already visible in the thread, so that just gets a
    short "resuming"; a dashboard/GitHub answer echoes the answer text. Best-effort."""
    thread_ts = registry.slack_thread_for(app_name, issue_number)
    if not thread_ts:
        return
    if source == "slack":
        text = ":white_check_mark: Got it — resuming."
    else:
        text = f':white_check_mark: Answered via {_SOURCE_LABEL.get(source, source)}: "{answer.strip()}" — resuming.'
    try:
        from agentra.connectors import slack

        slack._post_message(text, channel=registry.get_slack_channel(app_name), thread_ts=thread_ts)
    except Exception:
        logger.warning("_ack_slack_thread failed for app=%s issue=#%s", app_name, issue_number, exc_info=True)


def dispatch_human_answer(app_name: str, repo: Path, issue_number: int, answer: str, *, source: str) -> dict:
    """Records `answer` on the needs_human issue (removing the needs_human label -- Memory.record_human_answer) and dispatches a resume in the background that reuses the original branch/session_id."""
    mem = Memory(repo)
    context = mem.get_human_input_context(issue_number)
    if context is None:
        raise ValueError(f"no human-input context recorded for issue #{issue_number}")

    # A second answer landing after the first (e.g. a Slack reply just after a
    # dashboard answer) must not spawn a spurious fresh cycle -- once any channel
    # has answered, the need_human label is gone and this is a no-op ack.
    if not mem.human_input_pending(issue_number):
        _ack_slack_thread(app_name, issue_number, answer, source)
        _server_log(source, f"app={app_name!r} issue=#{issue_number} -- answer ignored, already resolved")
        return {"run_key": None, "already_answered": True}

    for run in registry.list_waiting_for_human():
        human_input = run.get("human_input") or {}
        if run.get("app") == app_name and human_input.get("issue_number") == issue_number:
            registry.record_run(run["run_key"], status="answered")

    objective = mem.get_objective() or ""
    tracking_issue = context.get("tracking_issue")
    loop_id = (
        registry.loop_id_for_issue(repo.name, tracking_issue)
        if tracking_issue is not None
        else registry.loop_id_for(objective)
    )
    run_key = uuid.uuid4().hex[:8]
    registry.record_run(
        run_key,
        app=app_name,
        source=source,
        status="queued",
        started_at=time.time(),
        objective=objective,
        loop_id=loop_id,
    )
    mem.record_human_answer(issue_number, answer, resumed_run_key=run_key)
    _ack_slack_thread(app_name, issue_number, answer, source)
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
    """Backs the dashboard's 'Needs your input' panel -- every run..."""
    return {"runs": registry.list_waiting_for_human()}

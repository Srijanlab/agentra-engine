"""server/routes/human_input.py — human-in-the-loop escalation answer channel (GitHub issue #34)."""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentra import registry
from agentra.memory import Memory
from agentra.server.utils import _paused_response, _server_log

logger = logging.getLogger(__name__)

router = APIRouter()


class HumanInputAnswerPayload(BaseModel):
    issue_number: int
    answer: str


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

    objective = mem.get_objective() or ""
    tracking_issue = context.get("tracking_issue")
    loop_id = (
        registry.loop_id_for_issue(app_name, tracking_issue)
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
    # The loop is being actively worked again -- drop it out of the "needs input"
    # listing now; the resume run's own roll-up sets the next loop status.
    try:
        if tracking_issue is not None:
            registry.set_loop_status(loop_id, "active")
    except Exception:
        logger.warning("dispatch_human_answer: could not set loop %s active", loop_id, exc_info=True)
    mem.record_human_answer(issue_number, answer, resumed_run_key=run_key)
    _ack_slack_thread(app_name, issue_number, answer, source)
    job_id = registry.enqueue_job("human_resume", {
        "run_key": run_key, "app": app_name, "issue_number": issue_number,
        "answer": answer, "context": context,
    }, dedup_key=f"human_resume:{app_name}:{issue_number}")
    _server_log(source, f"app={app_name!r} issue=#{issue_number} run_key={run_key} job={job_id} -- human answer accepted, resume queued")
    return {"run_key": run_key, "job_id": job_id, "branch": context.get("branch"), "session_id": context.get("session_id")}


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


def _needs_human_shape(loop: dict) -> dict:
    """A waiting loop, shaped like the dashboard's NeedsHumanRun (run_key + app +
    status + human_input) -- the panel is issue-oriented, so run_key is just a
    display/React key and maps to the loop's most recent run."""
    return {
        "run_key": loop.get("last_run_key") or loop.get("loop_id") or "",
        "app": loop.get("app") or "",
        "status": loop.get("status"),
        "human_input": loop.get("human_input") or {},
    }


@router.get("/needs-human")
async def list_needs_human() -> dict:
    """Backs the dashboard's 'Needs your input' panel -- one entry per loop parked
    on a blocking human question (the run that hit the block has already terminated)."""
    return {"runs": [_needs_human_shape(l) for l in registry.list_waiting_for_human()]}

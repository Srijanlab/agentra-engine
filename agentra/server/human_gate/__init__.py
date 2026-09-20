"""server/human_gate/ — closes the silent-run gap (GitHub issue #55).

Confirmed live 2026-09-20: three loops (#49, #50, #51) each ended a run with a
HUMAN_INPUT_REQUIRED note in its free-text summary, asking a human to decide
something -- but nothing raised the need_human label or posted to Slack, since
the run's own terminal status was "completed", not "waiting_for_human", and
the label-based watcher only sees issues that already carry need_human. A run
asking for a human must never go out silently.

`maybe_raise(run_key)` is the single entry point: given a just-recorded run,
raise its human gate if it qualifies (terminal status + an error containing
the HUMAN_INPUT_REQUIRED token), idempotently. Called from two places: (a) the
/internal/rpc record_run handler, right after a successful write -- the fast
path; (b) the /trigger/cron tick, sweeping recent runs -- the backstop for a
missed or failed prompt-path attempt (Slack down, GitHub blip).
"""

from __future__ import annotations

import logging
import re
import time

from agentra import registry, urls
from agentra.connectors import slack

logger = logging.getLogger("agentra.server.human_gate")

TOKEN = "HUMAN_INPUT_REQUIRED"
_TOKEN_RE = re.compile(re.escape(TOKEN) + r"[:\-\s]*(.*)", re.DOTALL)
_TERMINAL_STATUSES = frozenset({"completed", "failed", "blocked", "waiting_for_human"})
_MAX_QUESTION_CHARS = 1500


def _extract_question(error: str) -> str:
    match = _TOKEN_RE.search(error)
    question = (match.group(1).strip() if match else "").strip()
    return question[:_MAX_QUESTION_CHARS] if question else "(question unavailable)"


def _memory_for(app_name: str):
    from agentra.memory import Memory

    repo = registry.get_app_repo(app_name)
    return Memory(repo) if repo is not None else None


def maybe_raise(run_key: str) -> dict | None:
    """Raise `run_key`'s human gate if it qualifies. Returns a summary dict
    ({app, run_key, issue_number, label_set, slack_posted}) when a gate was
    raised or retried, or None when there was nothing to do (doesn't qualify,
    or an identical gate for this exact run already fully succeeded). Never
    raises -- a gate failure must not turn a successful record_run/cron tick
    into an error."""
    try:
        run = registry.get_run(run_key)
        if not run or run.get("status") not in _TERMINAL_STATUSES:
            return None
        # Confirmed live 2026-09-20 (#49/#50/#51): the loop's own orchestrator
        # narrates a HUMAN_INPUT_REQUIRED note in the run's free-text `summary`,
        # not `error` -- a crashed tool call ends the run as "completed" with no
        # `error` set at all. Check both; prefer `error` when both are present.
        text = run.get("error") or run.get("summary") or ""
        if TOKEN not in text:
            return None
        app_name = run.get("app")
        if not app_name:
            return None
        return _raise(app_name, run_key, run, text)
    except Exception:
        logger.warning("maybe_raise: failed for run_key=%r", run_key, exc_info=True)
        return None


def _raise(app_name: str, run_key: str, run: dict, error: str) -> dict | None:
    loop_id = run.get("loop_id")
    loop = registry.get_loop(loop_id) if loop_id else None
    human_input = dict((loop or {}).get("human_input") or {})

    same_run_retry = human_input.get("gate_run_key") == run_key
    if same_run_retry and human_input.get("slack_thread_ts"):
        return None  # this exact run's gate already fully succeeded -- nothing to retry

    mem = _memory_for(app_name)
    if mem is None:
        return None

    raw_issue_number = (loop or {}).get("issue_number") or human_input.get("issue_number")
    issue_number = int(raw_issue_number) if raw_issue_number is not None else None
    question = _extract_question(error)
    label_set = False

    if issue_number is None:
        issue_number = mem.record_known_bug(
            run_key, "high", question, "(needs a human decision)",
            source="human-input-required", needs_human=True,
        )
        if issue_number is None:
            return None
        label_set = True
        human_input = {}  # a brand-new gate -- nothing stale to carry over
    elif not same_run_retry:
        if mem.human_input_pending(issue_number):
            return None  # a different, still-unanswered gate is already open on this issue -- skip entirely
        # A fresh gate on an issue with no still-open ask (never gated, or a
        # prior gate was already answered and cleared): comment + label, and
        # start human_input clean -- reusing the old dict here would leak a
        # stale slack_thread_ts from an already-answered prior gate and wrongly
        # suppress this new gate's own Slack post.
        mem.escalate_existing_issue(issue_number, run_key, f"{TOKEN}: {question}")
        label_set = True
        human_input = {}
    # else: same_run_retry with no Slack post yet -- keep the existing
    # human_input as-is and only retry the Slack step below.

    if not loop_id:
        loop_id = registry.loop_id_for_issue(app_name, issue_number)
    mem.record_human_input_context(issue_number, app=app_name, run_id=run_key, question=question)

    human_input.update(
        issue_number=issue_number, question=question,
        issue_url=mem.issue_html_url(issue_number),
        waiting_since=human_input.get("waiting_since") or time.time(),
        app=app_name, category="silent_run", gate_run_key=run_key,
    )
    registry.set_loop_human_input(loop_id, human_input)

    slack_posted = False
    if not human_input.get("slack_thread_ts"):
        try:
            ts = slack.notify_human_input_required(
                app=app_name, run_id=run_key,
                question=f"Loop {loop_id} · issue #{issue_number} -- {question}",
                issue_url=human_input.get("issue_url"),
                dashboard_url=urls.dashboard_run_url(run_key, app_name),
                branch=human_input.get("branch"), session_id=human_input.get("session_id"),
                channel=registry.get_slack_channel(app_name),
            )
        except Exception:
            logger.warning("_raise: slack post failed for app=%r issue=#%s", app_name, issue_number, exc_info=True)
            ts = None
        if ts:
            registry.record_slack_thread(ts, app=app_name, issue_number=issue_number)
            human_input["slack_thread_ts"] = ts
            registry.set_loop_human_input(loop_id, human_input)
            slack_posted = True

    return {
        "app": app_name, "run_key": run_key, "issue_number": issue_number,
        "label_set": label_set, "slack_posted": slack_posted,
    }


def sweep_recent_runs(limit: int = 50) -> list[dict]:
    """Backstop for the /trigger/cron tick: re-check recent runs whose error or
    summary carries the token, so a missed or failed prompt-path attempt gets
    retried."""
    gates: list[dict] = []
    for run in registry.list_runs(limit=limit):
        run_key = run.get("run_key")
        text = run.get("error") or run.get("summary") or ""
        if not run_key or run.get("status") not in _TERMINAL_STATUSES or TOKEN not in text:
            continue
        gate = maybe_raise(run_key)
        if gate:
            gates.append(gate)
    return gates

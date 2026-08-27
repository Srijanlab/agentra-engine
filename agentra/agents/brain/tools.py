"""agents/brain/tools.py — Specialized sub-agent tools orchestrating the codebase."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import tool

from agentra import change_risk
from agentra.agents import architecture_review, codebase, deployment, discovery, feedback, human_answer_judge, implementation, requirements, testing
from agentra.agents import catalog as agents_catalog
from agentra.agents.base import AgentResult
from agentra.agents.brain import infra_cost_gate
from agentra.agents.generic import TaskSpec, spawn as spawn_generic
from agentra.environments import feature_branch_name
from agentra.memory.core import _DISCOVERY_LABEL
from agentra.ranking import rank

if TYPE_CHECKING:
    from agentra.agents.brain import OrchestratorSession
    from agentra.memory import Memory

logger = logging.getLogger(__name__)

MAX_SELF_HEAL_ATTEMPTS = 1


def _actionable_bugs(bugs: list[dict]) -> list[dict]:
    """Filters out bugs labeled need_human."""
    return [b for b in bugs if not b.get("needs_human")]


def _format_spec(spec: dict, human_answer: str | None = None) -> str:
    """Requirements Agent's JSON spec formatted as readable text."""
    lines = [f"Spec: {spec.get('spec', '')}"]
    criteria = spec.get("acceptance_criteria") or []
    if criteria:
        lines.append("\nAcceptance criteria:")
        lines.extend(f"- {c}" for c in criteria)
    if human_answer:
        lines.append(
            "\nA human has answered your previous blocking question -- use this to proceed, "
            "do not ask it again:\n" + human_answer.strip()
        )
    return "\n".join(lines)


def _has_active_feature_branches(repo: Path) -> bool:
    """Check if there are any active dev/ feature branches, indicating work-in-progress."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "branch", "-a", "--format=%(refname:short)"],
            capture_output=True,
            text=True,
            check=True,
        )
        branches = result.stdout.splitlines()
        # Check for remotes/origin/dev/* branches (active feature work)
        return any("dev/" in b for b in branches)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _file_top_opportunity_as_feature_request(session: OrchestratorSession, opportunities: list[dict]) -> dict | None:
    """When the backlog is genuinely empty (no in-progress feature, no actionable known bug, no queued feature request -- the same emptiness that already makes discover_opportunities the documented last resort in check_backlog's own tool description), durably files the single top-ranked opportunity as a real GitHub feature-request issue (issue #23) so it shows up in check_backlog's feature queue on a future cycle like any other GitHub-sourced feature request."""
    if not opportunities:
        return None
    if session.mem.in_progress_features() or _actionable_bugs(session.mem.known_bugs()) or session.mem.feature_queue():
        return None
    # Also check for active feature branches (in-progress work that may not have sub-issues yet)
    if _has_active_feature_branches(session.repo):
        return None
    top = opportunities[0]
    feature = (top.get("feature") or "").strip() or "New opportunity"
    description = (top.get("description") or "").strip() or "No further description was provided by Discovery Agent."
    reason = (top.get("reason") or "").strip()
    body = description
    if reason:
        body += f"\n\nWhy: {reason}"
    impact, effort = top.get("impact"), top.get("effort")
    if impact or effort:
        body += f"\n\nImpact: {impact or 'unspecified'}, Effort: {effort or 'unspecified'}"
    return session.mem.record_feature_request(body, source="github", title=feature, extra_labels=[_DISCOVERY_LABEL])


_ISSUE_REF_RE = re.compile(r"#(\d+)")


def _infer_resolves_from_brief(mem, feature_brief: str) -> tuple[str, str] | None:
    """Best-effort fallback for when the caller didn't pass resolves_id/resolves_origin
    explicitly: scans feature_brief for a #<number> reference and, if it matches an open known
    bug or feature-queue item's external_id, returns (id, origin) to use as if the caller had
    passed it -- not a replacement for the caller passing resolves_id itself."""
    for match in _ISSUE_REF_RE.finditer(feature_brief):
        number = match.group(1)
        for bug in mem.known_bugs():
            if str(bug.get("external_id")) == number:
                return number, "known_bug"
        for feature in mem.feature_queue():
            if str(feature.get("external_id")) == number:
                return number, "feature_queue"
    return None


def _check_auth_failure(session: OrchestratorSession, tool_name: str, result: AgentResult) -> dict | None:
    """GitHub issue #42: a Claude Code CLI auth/login failure (result.auth_failure, set distinctly by agents/base.py's run_agent) is an infra-level problem -- no retry, no different tool call, no different brief will fix it, only a human running `claude /login`."""
    if not result.auth_failure:
        return None
    session.auth_failure_this_cycle = True
    session.mem.record_failure(session.run_id, tool_name, result.text)
    session.hard_stop_reason = (
        f"Claude Code session is not authenticated on this runner (detected in {tool_name}) -- "
        "run /login and re-trigger. Filed as a blocking bug and notified via Slack (if "
        "configured); stop calling tools and end your turn."
    )
    session.note(f"{tool_name}: Claude Code auth failure -- escalated, ending this cycle", ok=False)
    return {"content": [{"type": "text", "text": result.text}], "is_error": True}


def _escalate_to_human(
    session: OrchestratorSession,
    *,
    category: str,
    diagnosis: str,
    question: str,
    source: str,
    title: str,
    branch: str | None = None,
    tracking_issue: int | None = None,
    session_id: str | None = None,
) -> int | None:
    """Human-in-the-loop escalation (GitHub issue #34), single shared helper for every escalation site in this module (implement_feature's own HUMAN_INPUT_REQUIRED branch, discover_opportunities, and the deterministic infra-cost gate) -- deploy_pre_prod's own HUMAN_INPUT_REQUIRED path is explicitly out of scope for this, left untouched. category is a short, machine-checkable tag (e.g. "product_direction", "implementation", "infra_cost") stamped into the filed issue body AND threaded into session.mark_waiting_for_human's structured human_input dict, so an escalation's reason is queryable/chartable from the Firestore/local-JSON run record itself, not just discoverable by grepping issue text. session_id defaults to session.session_id (the cross-run resume seed) but a caller with a fresher just-returned session_id (e.g. implement_feature's own AgentResult) should pass it explicitly, since session.session_id is no longer kept in sync with every sub-agent call. When tracking_issue is set, the blocking question is posted directly on that issue (never a separate needs_human issue) -- confirmed live as issues #79, #80, #81: the same interrupted item spawned three separate escalation issues instead of the question just landing on the tracking issue itself. Only a genuinely homeless escalation (no tracking_issue at all) files a new bug issue."""
    session_id = session_id if session_id is not None else session.session_id
    full_diagnosis = f"Category: {category}\n\n{diagnosis}"
    if tracking_issue is not None:
        session.mem.escalate_existing_issue(tracking_issue, session.run_id, full_diagnosis)
        issue_number = tracking_issue
    else:
        issue_number = session.mem.record_known_bug(
            session.run_id, "medium", full_diagnosis,
            "Requires an explicit human decision -- not an implementation/discovery failure, "
            "not something a different brief/approach fixes.",
            source=source,
            needs_human=True,
            title=title,
        )
    issue_url = session.mem.issue_html_url(issue_number) if issue_number is not None else None
    if issue_number is not None:
        session.mem.record_human_input_context(
            issue_number, app=session.app_name, run_id=session.run_id, question=question,
            branch=branch, session_id=session_id, tracking_issue=tracking_issue,
        )
    from agentra import urls
    from agentra.connectors import slack

    slack.notify_human_input_required(
        app=session.app_name,
        run_id=session.run_id,
        question=question,
        issue_url=issue_url,
        dashboard_url=urls.dashboard_run_url(session.run_id, session.app_name),
        branch=branch,
        session_id=session_id,
    )
    session.mark_waiting_for_human(
        issue_number=issue_number, issue_url=issue_url, question=question, branch=branch, category=category,
    )
    return issue_number


def _notify_shipped_pending(session: OrchestratorSession, verification_result: str) -> None:
    """Drains session.pending_shipped_notifications, posting exactly one Slack 'shipped'
    message per entry via connectors/slack.py's notify_shipped -- the single choke point for
    this notification. Called only from deploy_pre_prod's trivial-merge success branch and
    verify_pre_prod's pass branch (never from implement_feature/record_code_complete's earlier
    status:shipped label stamp), so it fires strictly on confirmed pre-prod delivery. Fail-open:
    a Slack failure (unconfigured, network, API rejection) never raises or blocks the run."""
    if not session.pending_shipped_notifications:
        return
    from agentra.connectors import slack

    pending, session.pending_shipped_notifications = session.pending_shipped_notifications, []
    for item in pending:
        board_issue_number = item.get("board_issue_number") or item.get("issue_number")
        issue_url = session.mem.issue_html_url(board_issue_number) if board_issue_number is not None else None
        try:
            slack.notify_shipped(
                app=session.app_name,
                feature_title=item.get("title") or "Untitled feature",
                issue_url=issue_url,
                verification_result=verification_result,
            )
        except Exception:
            logger.warning("notify_shipped failed for issue #%s", board_issue_number, exc_info=True)


# Deterministic infra-cost gate (Part 2/2): keyword heuristic + gate decision logic live in
# their own module (agents/brain/infra_cost_gate.py) -- these two thin wrappers just supply the
# session/escalation plumbing that module doesn't own, to avoid a circular import.
async def _run_design_review(session: OrchestratorSession, feature_brief: str) -> tuple[dict | None, AgentResult | None]:
    return await infra_cost_gate.run_design_review(session, feature_brief, check_auth_failure=_check_auth_failure)


async def _judge_human_answer(
    session: OrchestratorSession, review: dict, human_answer: str, feature_brief: str
) -> tuple[dict | None, str, str]:
    """Runs the Human-Answer Judge Agent and returns (stop, decision, reason). stop is a
    tool-result dict the caller should return immediately on a Claude Code auth failure;
    decision/reason default to a fail-safe "still_needs_escalation" if the judge call itself
    didn't succeed or didn't return a parseable decision."""
    result = await human_answer_judge.run(session.repo, review, human_answer, feature_brief)
    if stop := _check_auth_failure(session, "implement_feature", result):
        return stop, "still_needs_escalation", "judge call hit an auth failure"
    session.cost_usd += result.cost_usd
    data = result.json_data or {}
    decision = data.get("decision") if result.ok else None
    reason = data.get("reason") or (result.text[:200] if not result.ok else "no reason given")
    return None, decision or "still_needs_escalation", reason


async def _infra_cost_gate(session: OrchestratorSession, feature_brief: str, tracking_issue: int | None) -> dict | None:
    return await infra_cost_gate.gate(
        session, feature_brief, tracking_issue,
        escalate_to_human=_escalate_to_human, judge_human_answer=_judge_human_answer,
    )


def _attach_resume_branches(mem: Memory, entries: list[dict]) -> list[dict]:
    """Adds resume_branch and resume_run_id fields concurrently via a thread pool."""
    if not entries:
        return entries
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        branches = list(pool.map(lambda e: mem.resume_branch_for(e["external_id"]), entries))
        run_ids = list(pool.map(lambda e: mem.resume_run_id_for(e["external_id"]), entries))
    for entry, branch, run_id in zip(entries, branches, run_ids):
        entry["resume_branch"] = branch
        entry["resume_run_id"] = run_id
    return entries


def _stagnation_tracked(session: OrchestratorSession, tool_name: str, handler):
    """Wraps a tool handler so the stagnation breaker tracks its state changes."""
    async def wrapped(args):
        before = session.progress_snapshot()
        result = await handler(args)
        after = session.progress_snapshot()
        session.record_tool_call(tool_name, args, state_changed=before != after)
        return result
    return wrapped


def _file_incidental_findings(mem: Memory, run_id: str, data: dict, source: str) -> int:
    """Files incidental bug findings noticed by Testing Agent during verification."""
    findings = data.get("incidental_findings") or []
    filed = 0
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        diagnosis = (finding.get("diagnosis") or finding.get("description") or "").strip()
        if not diagnosis:
            continue
        mem.record_known_bug(
            run_id,
            finding.get("severity") or "medium",
            diagnosis,
            finding.get("proposed_fix")
            or "Not investigated further -- noticed incidentally by the Testing Agent.",
            source=source,
        )
        filed += 1
    return filed


def _tools_for(session: OrchestratorSession) -> list:
    @tool(
        "understand_codebase",
        "Scan the repo and produce the codebase understanding summary. Usually "
        "NOT needed: if one was already generated in a prior cycle, it's already "
        "loaded into context below and check_backlog/discover_opportunities/"
        "implement_feature all work without calling this again. Call it only if "
        "no summary is shown below (first-ever run for this repo) or you have a "
        "specific reason to believe it's now significantly out of date.",
        {},
    )
    async def understand_codebase(_args):
        if stop := session.check_hard_stop():
            return stop
        cb = await codebase.run_cached(session.repo, session.mem)
        if stop := _check_auth_failure(session, "understand_codebase", cb):
            return stop
        session.cost_usd += cb.cost_usd
        if cb.ok:
            session.cb_summary = cb.text
            session.record_success("understand_codebase")
        else:
            session.record_failure("understand_codebase")
        session.note(f"understand_codebase: ok={cb.ok}", ok=cb.ok, cost_usd=cb.cost_usd, turns=cb.turns)
        return {
            "content": [{"type": "text", "text": f"[{'ok' if cb.ok else 'failed'}] {cb.text[:4000]}"}],
            "is_error": not cb.ok,
        }

    @tool(
        "check_backlog",
        "Cheap, direct data read -- no sub-agent call, always call this before "
        "discover_opportunities. Priority order: (1) shipped items pending live testing, "
        "(2) code-complete items pending merge to pre-prod, (3) in-progress multi-part "
        "features, (4) in-progress single-part bugs/features (status:in-progress, real work "
        "already started), (5) known bugs not yet started, (6) feature request queue, "
        "(7) discover_opportunities. Bugs labeled need_human are filtered out. A non-null "
        "resume_branch means work was previously started there; pass it to implement_feature "
        "to continue.",
        {},
    )
    async def check_backlog(_args):
        if stop := session.check_hard_stop():
            return stop
        shipped = [f["feature"] for f in session.mem.shipped_features()]
        pending_test = _attach_resume_branches(session.mem, session.mem.shipped_pending_test_items())
        pending_merge = _attach_resume_branches(session.mem, session.mem.code_complete_items())
        in_progress = session.mem.in_progress_features()
        # GitHub issue #87: a single-part bug/feature already stamped status:in-progress (a
        # branch, prior implement_feature attempts) used to rank no higher than a never-touched
        # backlog item, since neither known_bugs() nor feature_queue() special-cases this label
        # -- filtered back out of those two below so nothing is listed twice.
        flagged_in_progress = _attach_resume_branches(session.mem, session.mem.in_progress_items())
        flagged_ids = {item["external_id"] for item in flagged_in_progress if item.get("external_id")}
        bugs = _attach_resume_branches(session.mem, _actionable_bugs(session.mem.known_bugs()))
        bugs = [b for b in bugs if b.get("external_id") not in flagged_ids]
        queue = _attach_resume_branches(session.mem, session.mem.feature_queue())
        queue = [f for f in queue if f.get("external_id") not in flagged_ids]
        session.backlog_ids_shown.update(
            str(item["external_id"])
            for item in (*pending_test, *pending_merge, *in_progress, *flagged_in_progress, *bugs, *queue)
            if item.get("external_id")
        )
        remaining_after_one = (
            len(pending_test) + len(pending_merge) + len(in_progress) + len(flagged_in_progress)
            + len(bugs) + len(queue) - 1
        )
        batching_hint = (
            f"\n\n{remaining_after_one} more item(s) will still be waiting after you address one of "
            "these -- if what you're about to build is not a trivial fix, consider implementing "
            "several of them before your first deploy_pre_prod call this run (it deploys/verifies "
            "everything implemented so far together, see its own description), rather than paying "
            "for a full pre-prod deploy + live verification once per item. Do not bundle a large, "
            "standing, multi-session effort (e.g. one already spanning several prior commits) in "
            "with an unrelated small fix just because both are waiting -- pick items that are "
            "actually safe to test/deploy/review together."
            if remaining_after_one > 0 else ""
        )
        text = (
            "Work through what's already in flight before starting anything new -- in this order:\n"
            f"1. Shipped, pending live testing (call implement_feature with resolves_id/resume_branch set, "
            f"then run_local_tests -> deploy_pre_prod -> verify_pre_prod): "
            f"{json.dumps(pending_test, indent=2) if pending_test else '(none)'}\n\n"
            f"2. Code complete, pending merge to pre-prod (same resume flow, through deploy_pre_prod): "
            f"{json.dumps(pending_merge, indent=2) if pending_merge else '(none)'}\n\n"
            f"3. In-progress multi-part features (resume and finish coding): "
            f"{json.dumps(in_progress, indent=2) if in_progress else '(none)'}\n\n"
            f"4. In-progress single-part bugs/features (real work already started -- resume this "
            f"branch, do not start fresh): "
            f"{json.dumps(flagged_in_progress, indent=2) if flagged_in_progress else '(none)'}\n\n"
            f"5. Known bugs not yet started (need_human bugs already excluded): "
            f"{json.dumps(bugs, indent=2) if bugs else '(none)'}\n\n"
            f"6. Feature request queue, not yet started: "
            f"{json.dumps(queue, indent=2) if queue else '(none)'}\n\n"
            f"Already shipped: {shipped or '(none)'}"
            f"{batching_hint}"
        )
        session.note("check_backlog", ok=True)
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "discover_opportunities",
        "Last resort: ideate new features once the backlog is empty. Asks "
        "Product Discovery Agent for ranked feature opportunities. When the "
        "backlog was genuinely empty, automatically files the single "
        "top-ranked opportunity as a new GitHub feature-request issue "
        "(discovery label) so it isn't lost -- you don't need to file it "
        "yourself, but may still call implement_feature on it this turn.",
        {},
    )
    async def discover_opportunities(_args):
        if stop := session.check_hard_stop():
            return stop
        if session.cb_summary is None:
            return {"content": [{"type": "text", "text": "Call understand_codebase first."}], "is_error": True}
        disc = await discovery.run(
            session.repo,
            session.objective,
            session.cb_summary,
            session.analytics_summary,
            [f["feature"] for f in session.mem.shipped_features()],
            _actionable_bugs(session.mem.known_bugs()),
            session.mem.feature_queue(),
        )
        if stop := _check_auth_failure(session, "discover_opportunities", disc):
            return stop
        session.cost_usd += disc.cost_usd
        data = disc.json_data or {}

        if data.get("status") == "HUMAN_INPUT_REQUIRED":
            reason = data.get("reason") or "Discovery Agent flagged this objective as needing a strategic decision outside its authority."
            question = data.get("question") or ""
            options = data.get("options") or []
            diagnosis = reason
            if question:
                diagnosis += f"\n\nQuestion for a human: {question}"
            if options:
                diagnosis += f"\n\nOptions considered: {options}"
            # No resume contract for discover_opportunities (out of scope for this
            _escalate_to_human(
                session,
                category="product_direction",
                diagnosis=diagnosis,
                question=question or reason,
                source="discovery-agent-human-input-required",
                title="Human input required: product direction",
            )
            session.note(f"discover_opportunities: human input required -- {reason[:200]}", ok=None)
            return {
                "content": [{
                    "type": "text",
                    "text": f"Escalated to a human: {reason} Filed as a GitHub issue (needs_human) and notified via "
                    "Slack (if configured); this run is now waiting_for_human.",
                }],
                "is_error": True,
            }

        opportunities = rank(data.get("opportunities", []))
        session.note(
            f"discover_opportunities: {len(opportunities)} candidates",
            ok=disc.ok and bool(opportunities),
            cost_usd=disc.cost_usd,
            turns=disc.turns,
        )
        if not disc.ok or not opportunities:
            session.record_failure("discover_opportunities")
            return {"content": [{"type": "text", "text": "Discovery failed to produce opportunities."}], "is_error": True}
        session.record_success("discover_opportunities")

        # Backlog-empty auto-filing (issue #23): discover_opportunities is
        filed = _file_top_opportunity_as_feature_request(session, opportunities)
        text = json.dumps(opportunities, indent=2)
        if filed:
            issue_note = f" (issue #{filed['number']})" if filed.get("number") else ""
            session.note(f"discover_opportunities: backlog was empty -- filed top opportunity as a new feature request{issue_note}", ok=True)
            text += (
                f"\n\nBacklog was empty, so the top-ranked opportunity above was filed as a new "
                f"GitHub feature-request issue{issue_note} (label: discovery) -- it will surface via "
                "check_backlog on a future cycle like any other feature request; no need to "
                "call implement_feature on it this same turn unless you want to."
            )
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "assess_design_impact",
        "Optional, conditional step before implement_feature -- call this only when the "
        "feature brief looks architecturally significant (a schema change, a new API "
        "surface, a cross-cutting refactor spanning multiple layers), not for routine "
        "features. Asks the Architecture Review Agent to flag blast radius, risk, and infra "
        "cost impact before Implementation Agent starts. A material infra_cost_impact or a "
        "high risk_level touching the infra layer deterministically blocks implement_feature "
        "and escalates to a human -- this is not skippable by not calling this tool: briefs "
        "that plausibly touch infra get an automatic review inside implement_feature anyway.",
        {"feature_brief": str},
    )
    async def assess_design_impact(args):
        if stop := session.check_hard_stop():
            return stop
        if session.cb_summary is None:
            return {"content": [{"type": "text", "text": "Call understand_codebase first."}], "is_error": True}
        stop, review = await _run_design_review(session, args["feature_brief"])
        if stop:
            return stop
        if not review.ok:
            return {"content": [{"type": "text", "text": f"Design review failed: {review.text[:2000]}"}], "is_error": True}
        return {"content": [{"type": "text", "text": review.text[:3000]}]}

    @tool(
        "implement_feature",
        "Build a specific feature or fix a bug on a branch. Required once check_backlog has "
        "shown any bugs/feature-queue items this cycle: either set resolves_id (its external_id) "
        "+ resolves_origin ('known_bug' or 'feature_queue') to point at the exact item this brief "
        "resolves, or set resolves_origin='new' to declare this brief is deliberately not one of "
        "them. For multi-part features, set more_parts_expected=true on all calls except the "
        "last, and pass sub_feature_of on subsequent parts. Set resume_branch if resuming work "
        "from check_backlog.",
        {
            "feature_brief": str,
            "resolves_origin": str,
            "resolves_id": str,
            "sub_feature_of": str,
            "more_parts_expected": bool,
            "resume_branch": str,
        },
    )
    async def implement_feature(args):
        if stop := session.check_hard_stop():
            return stop
        if session.cb_summary is None:
            return {"content": [{"type": "text", "text": "Call understand_codebase first."}], "is_error": True}
        brief = args["feature_brief"]
        resume_branch = args.get("resume_branch") or ""
        resuming = bool(resume_branch) and session.feature_branch is None
        if session.feature_branch is None:
            session.feature_branch = resume_branch if resume_branch else feature_branch_name(session.env, session.run_id, brief)
        resolves_origin = args.get("resolves_origin") or ""
        resolves_id = args.get("resolves_id") or ""
        if not resolves_id and resolves_origin != "new":
            resolves_id, resolves_origin = _infer_resolves_from_brief(session.mem, brief) or ("", resolves_origin)
        sub_feature_of = args.get("sub_feature_of") or ""
        more_parts_expected = bool(args.get("more_parts_expected"))
        tracking_issue = None
        if resolves_id and resolves_id.isdigit():
            tracking_issue = int(resolves_id)
        elif sub_feature_of and sub_feature_of.isdigit():
            tracking_issue = int(sub_feature_of)

        if not resolves_id and session.backlog_ids_shown and resolves_origin != "new":
            return {
                "content": [{"type": "text", "text": (
                    "check_backlog showed open bug/feature-queue items this cycle, but this call "
                    "set neither resolves_id (pointing at one of them) nor resolves_origin='new' "
                    "(declaring this brief isn't one of them). Set one and call implement_feature "
                    "again -- otherwise shipping this risks filing a duplicate issue instead of "
                    "resolving the existing one."
                )}],
                "is_error": True,
            }

        if resuming and tracking_issue is not None and session.session_id is None:
            session.session_id = session.mem.resume_session_id_for(str(tracking_issue))

        # Deterministic infra-cost gate, part a: close the coverage gap where
        # assess_design_impact is fully LLM-discretionary and a brief that plausibly touches
        # infra could skip review entirely -- if no review exists yet this cycle for this exact
        # brief and it matches the keyword heuristic, force one now rather than silently
        # skipping straight to implementation.
        if brief not in session.design_reviews and infra_cost_gate.brief_plausibly_touches_infra(brief):
            session.note(
                "implement_feature: feature_brief matches the infra-cost keyword heuristic -- "
                "automatically running a design-impact review before implementation",
                ok=None,
            )
            stop, _review = await _run_design_review(session, brief)
            if stop:
                return stop

        # Infra-cost gate, part b: should_block() is a real Python check, not a prompt
        # instruction -- cannot be bypassed by the brain skipping assess_design_impact or by
        # prompt wording. Whether a resumed cycle may proceed past a block once a human has
        # answered is judged by the Human-Answer Judge Agent, not a fixed rule (see
        # infra_cost_gate.gate's docstring).
        if stop := await _infra_cost_gate(session, brief, tracking_issue):
            return stop

        spec_dict = session.mem.get_spec(tracking_issue) if tracking_issue is not None else None
        if spec_dict is not None:
            session.note(f"requirements: reusing existing spec for issue #{tracking_issue}", ok=True)
        else:
            req = await requirements.run(session.repo, session.objective, brief, session.cb_summary)
            if stop := _check_auth_failure(session, "implement_feature", req):
                return stop
            session.cost_usd += req.cost_usd
            session.note(f"requirements: ok={req.ok}", ok=req.ok, cost_usd=req.cost_usd, turns=req.turns)
            if req.ok and req.json_data and req.json_data.get("spec"):
                spec_dict = req.json_data
                if tracking_issue is not None:
                    session.mem.record_spec(tracking_issue, spec_dict)
        # Human-in-the-loop escalation (GitHub issue #34): if this session was
        human_answer_for_this_call = None
        if session.human_answer and tracking_issue is not None and tracking_issue == session.human_answer_issue:
            human_answer_for_this_call = session.human_answer
            session.human_answer = None
            session.human_answer_issue = None
        if spec_dict:
            spec_text = _format_spec(spec_dict, human_answer=human_answer_for_this_call)
        elif human_answer_for_this_call:
            spec_text = (
                "A human has answered your previous blocking question -- use this to proceed, "
                "do not ask it again:\n" + human_answer_for_this_call.strip()
            )
        else:
            spec_text = ""
        session.current_spec = spec_text or None

        try:
            impl = await implementation.run(
                session.repo, session.objective, brief, session.cb_summary, session.env, session.feature_branch,
                resume=resuming, spec=spec_text, session_id=session.session_id,
                mem=session.mem, run_id=session.run_id,
            )
        except Exception as exc:
            session.record_failure("implement_feature")
            session.note(f"implement_feature: raised {exc!r}", ok=False)
            return {"content": [{"type": "text", "text": f"implement_feature raised: {exc}"}], "is_error": True}
        if stop := _check_auth_failure(session, "implement_feature", impl):
            return stop
        session.cost_usd += impl.cost_usd
        if impl.push_failed and session.feature_branch:
            # GitHub issue #78: the commit is NOT confirmed durable on GitHub -- record a
            # deterministic, per-branch hard-stop flag for THIS feature branch so deploy_pre_prod
            # and record_code_complete below refuse to proceed for it, regardless of whether the
            # orchestrator LLM notices impl.ok=False / the error text in impl.text.
            session.mark_push_failed(session.feature_branch)
        # Disabled: chaining session.session_id across every sub-agent call in a cycle made
        # later calls resume the whole prior transcript, compounding context (confirmed live:
        # one Implementation Agent turn read 222K cached input tokens). Each call now starts
        # fresh; session.session_id still only comes from the explicit cross-run resume path.
        # session.session_id = impl.session_id or session.session_id
        data = impl.json_data or {}
        feature_name = data.get("feature") or brief.split(":")[0].strip()
        session.tests_passed = False
        session.pre_prod_verified = False
        session.change_risk = None
        session.note(
            f"implement_feature: ok={impl.ok} feature={feature_name!r}",
            ok=impl.ok,
            cost_usd=impl.cost_usd,
            turns=impl.turns,
        )
        if tracking_issue is not None:
            session.mem.record_in_progress_branch(
                tracking_issue, session.feature_branch, session.run_id, impl.session_id or session.session_id
            )
        commit_sha = None
        try:
            head = subprocess.run(
                ["git", "-C", str(session.repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
            )
            if head.returncode == 0:
                commit_sha = head.stdout.strip()
        except Exception:
            pass
        if tracking_issue is not None and commit_sha:
            session.mem.record_commit(tracking_issue, commit_sha)

        if data.get("status") == "HUMAN_INPUT_REQUIRED":
            reason = data.get("reason") or "Implementation Agent flagged this brief as outside its authority to decide unilaterally."
            question = data.get("question") or ""
            options = data.get("options") or []
            diagnosis = reason
            if question:
                diagnosis += f"\n\nQuestion for a human: {question}"
            if options:
                diagnosis += f"\n\nOptions considered: {options}"
            # Human-in-the-loop escalation (GitHub issue #34): implement_feature
            _escalate_to_human(
                session,
                category="implementation",
                diagnosis=diagnosis,
                question=question or reason,
                source="implementation-agent-human-input-required",
                title=f"Human input required: {feature_name}",
                branch=session.feature_branch,
                tracking_issue=tracking_issue,
                session_id=impl.session_id or session.session_id,
            )
            # Also note it on the tracking issue itself (if this call was
            if tracking_issue is not None:
                session.mem.record_failure_on_issue(
                    tracking_issue, session.run_id, "implementation",
                    f"Stalled, needs human input: {diagnosis}",
                )
            session.note(f"implement_feature: human input required -- {reason[:200]}", ok=None)
            return {
                "content": [{
                    "type": "text",
                    "text": f"Escalated to a human: {reason} Filed as a GitHub issue (needs_human) and notified "
                    "via Slack (if configured); this run is now waiting_for_human.",
                }],
                "is_error": True,
            }

        if not impl.ok:
            if tracking_issue is not None:
                session.mem.record_failure_on_issue(tracking_issue, session.run_id, "implementation", impl.text)
            else:
                session.mem.record_failure(session.run_id, "implementation", impl.text)
            # A push failure surviving retries is deliberately counted the same as any other
            # implementation content failure toward MAX_CONSECUTIVE_TOOL_FAILURES (GitHub issue
            # #78): it's just as much a sign of a real, non-prompt-fixable problem (bad
            # credentials, repo access revoked, persistent network outage) as repeated bad code,
            # and retrying identical briefs against it would be equally pointless.
            session.record_failure("implement_feature")
            return {"content": [{"type": "text", "text": f"Implementation failed: {impl.text[:2000]}"}], "is_error": True}
        # Defense in depth: even though impl.ok already gates this (implementation.run sets
        # ok=False when the push fails after retries), check the dedicated per-branch flag
        # directly too, so record_code_complete can never fire for a branch whose push failed
        # regardless of how impl.ok ends up being computed later, or on a resumed branch whose
        # push failure happened in an earlier call this session.
        if stop := session.check_push_failure(session.feature_branch):
            return stop
        session.record_success("implement_feature")
        code_complete = session.mem.record_code_complete(
            feature_name,
            commit_sha=commit_sha,
            run_id=session.run_id,
            resolves_id=resolves_id if resolves_origin == "feature_queue" else None,
            sub_feature_of=sub_feature_of or None,
            more_parts_expected=more_parts_expected,
            session_id=session.session_id,
            known_bug_issue=resolves_id if resolves_origin == "known_bug" else None,
            branch=session.feature_branch,
        )
        session.mem.append_documentation(
            f"Code complete: **{feature_name}**"
            + (f" (commit `{commit_sha[:7]}`)" if commit_sha else "")
            + f": {brief[:300]}"
        )
        if resolves_id and resolves_origin == "known_bug":
            resolution_note = f"Resolved by agentra: code complete as {feature_name!r} (run {session.run_id})" + (
                f" (commit {commit_sha})" if commit_sha else ""
            )
            session.mem.clear_known_bug(resolves_id, resolution_note)
        session.current_feature = feature_name

        issue_number = code_complete["issue_number"] if code_complete else None
        parent_issue_number = code_complete["board_issue_number"] if code_complete else None
        if issue_number is not None:
            session.code_complete_issue_numbers.append(str(issue_number))
        if code_complete and not more_parts_expected:
            # Queued for the notify_shipped Slack message, drained once pre-prod delivery is
            # actually confirmed at deploy_pre_prod's trivial-merge success or verify_pre_prod's
            # pass -- never here. Skipped entirely when more_parts_expected (an intermediate
            # part of a multi-part feature): only the final part, which marks the parent shipped,
            # queues a notification, and it references the parent via board_issue_number.
            session.pending_shipped_notifications.append({
                "issue_number": issue_number,
                "board_issue_number": parent_issue_number,
                "title": feature_name,
            })
        issue_note = f" (issue #{issue_number})" if issue_number else ""
        next_part_hint = (
            f" More parts expected -- call implement_feature again for the next part with "
            f"sub_feature_of={str(parent_issue_number)!r} (and more_parts_expected=true "
            f"unless that call is the last part)."
            if more_parts_expected and parent_issue_number
            else ""
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Code complete: implemented, committed, and pushed {feature_name!r}{issue_note}. "
                        f"Call run_local_tests before deploy_pre_prod.{next_part_hint}"
                    ),
                }
            ]
        }

    @tool(
        "run_local_tests",
        "Verify the code itself locally. Required before deploy_pre_prod. "
        "On failure, automatically requests one Implementation Agent fix-up attempt.",
        {},
    )
    async def run_local_tests(_args):
        if stop := session.check_hard_stop():
            return stop
        if session.cb_summary is None:
            return {"content": [{"type": "text", "text": "Call understand_codebase first."}], "is_error": True}
        test = await testing.run_local(session.repo, session.cb_summary, session.mem, session_id=session.session_id)
        if stop := _check_auth_failure(session, "run_local_tests", test):
            return stop
        session.cost_usd += test.cost_usd
        # session.session_id = test.session_id or session.session_id  # see implement_feature
        data = test.json_data or {}
        passed = test.ok and data.get("status") != "fail"
        # The Testing Agent itself can fail to produce a verdict at all (e.g. it hits
        # its own max_turns budget) -- that's an agent-execution problem, not a failing
        # test, and test.json_data is None in that case (see run_agent's exception path).
        # Feeding the raw exception text to the Implementation Agent as "fix these
        # failing tests" is nonsensical (there is no such code bug); retry the test run
        # itself instead of asking Implementation to "fix" it.
        agent_errored = test.json_data is None

        attempts = 0
        while not passed and attempts < MAX_SELF_HEAL_ATTEMPTS and session.feature_branch is not None:
            attempts += 1
            if agent_errored:
                session.note(
                    f"run_local_tests: attempt {attempts} produced no verdict ({test.text[:200]!r}); "
                    "retrying the test run instead of dispatching a bogus fix",
                    ok=False, cost_usd=test.cost_usd, turns=test.turns,
                )
                test = await testing.run_local(session.repo, session.cb_summary, session.mem, session_id=session.session_id)
                if stop := _check_auth_failure(session, "run_local_tests", test):
                    return stop
                session.cost_usd += test.cost_usd
                data = test.json_data or {}
                passed = test.ok and data.get("status") != "fail"
                agent_errored = test.json_data is None
                continue
            failing = data.get("failed_tests") or [test.text[:1000]]
            fix = await implementation.run(
                session.repo,
                session.objective,
                f"Fix the currently failing local tests: {failing}. "
                "Do not change unrelated code or tests that are already passing.",
                session.cb_summary,
                session.env,
                session.feature_branch,
                resume=True,
                session_id=session.session_id,
                mem=session.mem,
                run_id=session.run_id,
            )
            if stop := _check_auth_failure(session, "run_local_tests", fix):
                return stop
            session.cost_usd += fix.cost_usd
            if fix.push_failed and session.feature_branch:
                session.mark_push_failed(session.feature_branch)
            # session.session_id = fix.session_id or session.session_id  # see implement_feature
            session.note(
                f"run_local_tests: self-heal attempt {attempts} ok={fix.ok}",
                ok=fix.ok, cost_usd=fix.cost_usd, turns=fix.turns,
            )
            if not fix.ok:
                break
            test = await testing.run_local(session.repo, session.cb_summary, session.mem, session_id=session.session_id)
            if stop := _check_auth_failure(session, "run_local_tests", test):
                return stop
            session.cost_usd += test.cost_usd
            # session.session_id = test.session_id or session.session_id  # see implement_feature
            data = test.json_data or {}
            passed = test.ok and data.get("status") != "fail"
            agent_errored = test.json_data is None

        session.tests_passed = passed
        detail = f"lint={data.get('lint_status', '?')} typecheck={data.get('typecheck_status', '?')}"
        if data.get("failed_tests"):
            detail += f" failed={data['failed_tests']}"
        session.note(f"run_local_tests: passed={passed} | {detail}", ok=passed, cost_usd=test.cost_usd, turns=test.turns)
        if not passed:
            session.record_failure("run_local_tests")
        else:
            session.record_success("run_local_tests")
        return {
            "content": [{"type": "text", "text": f"Local tests {'PASSED' if passed else 'FAILED'}. {test.text[:2000]}"}],
            "is_error": not passed,
        }

    @tool(
        "deploy_pre_prod",
        "Deploy everything implemented so far this run (implement_feature may have been called "
        "more than once) to the pre-prod environment. Requires passing local tests first. "
        "Automatically classifies the accumulated change: a trivial one (test fix, docs, config, "
        "rename, or a couple-line bug fix) is merged straight to pre-prod without a full deploy or "
        "live verification -- passing local tests is already enough proof at that size. A "
        "non-trivial change gets the real deploy, and should go through verify_pre_prod next. "
        "Prefer batching: if check_backlog showed more non-trivial items waiting, implement several "
        "before your first call here rather than deploying once per item.",
        {},
    )
    async def deploy_pre_prod(_args):
        if stop := session.check_hard_stop():
            return stop
        if session.skip_deploy:
            session.note("deploy_pre_prod: skipped (skip_deploy set for this run)", ok=None)
            return {"content": [{"type": "text", "text": "Skipped: this run was started with deploy disabled."}]}
        if not session.tests_passed:
            session.note("deploy_pre_prod: refused, tests not passed", ok=False)
            return {
                "content": [{"type": "text", "text": "Refused: run_local_tests must pass before deploying to pre-prod."}],
                "is_error": True,
            }
        if session.feature_branch is None:
            session.note("deploy_pre_prod: refused, no feature branch", ok=False)
            return {
                "content": [{"type": "text", "text": "Refused: nothing to deploy -- call implement_feature first."}],
                "is_error": True,
            }
        if stop := session.check_push_failure(session.feature_branch):
            session.note("deploy_pre_prod: refused, feature branch failed to push to GitHub", ok=False)
            return stop

        from agentra.agents.git_ops import fetch_ref

        try:
            fetch_ref(session.repo, session.env.pre_prod_branch)
        except Exception:
            pass  # best-effort -- classify_change falls back to STANDARD if the diff can't be read
        session.change_risk = change_risk.classify_change(
            session.repo, f"origin/{session.env.pre_prod_branch}", session.feature_branch
        )
        session.note(f"deploy_pre_prod: change_risk={session.change_risk}", ok=True)

        if session.change_risk == change_risk.TRIVIAL:
            deploy = await deployment.merge_to_pre_prod_only(session.repo, session.env, session.feature_branch)
            if stop := _check_auth_failure(session, "deploy_pre_prod", deploy):
                return stop
            session.deploy_attempted = True
            session.cost_usd += deploy.cost_usd
            ok = deploy.ok
            session.pre_prod_url = None
            session.deployed_to_pre_prod = ok
            # A passing local test suite is the whole point of the TRIVIAL
            session.pre_prod_verified = ok
            session.note(f"deploy_pre_prod: trivial change, merged only: ok={ok}", ok=ok, cost_usd=deploy.cost_usd)
            if not ok:
                session.mem.record_failure(session.run_id, "deployment", deploy.text)
                session.record_failure("deploy_pre_prod")
            else:
                session.record_success("deploy_pre_prod")
                if session.code_complete_issue_numbers:
                    moved = session.mem.record_shipped_to_preprod(session.code_complete_issue_numbers, session.run_id)
                    session.code_complete_issue_numbers = [i for i in session.code_complete_issue_numbers if i not in moved]
                    # No verify_pre_prod call follows a trivial merge, so these never reach
                    # status:tested -- deliberately not added to shipped_this_cycle_issue_numbers.
                # notify_shipped choke point (a): trivial changes never go through
                # verify_pre_prod, so this merge succeeding IS confirmed pre-prod delivery.
                _notify_shipped_pending(
                    session,
                    "Merged to pre-prod without a live deploy (trivial change, local tests are "
                    "sufficient proof).",
                )
            return {
                "content": [{"type": "text", "text": f"{deploy.text[:2000]} No verify_pre_prod call needed for this change."}],
                "is_error": not ok,
            }

        strategy = deployment.PRE_PROD_STRATEGIES[session.env.deploy_strategy]
        deploy = await strategy(
            session.repo, session.env, session.feature_branch, session.run_id, session.session_id
        )
        if stop := _check_auth_failure(session, "deploy_pre_prod", deploy):
            return stop
        session.deploy_attempted = True
        session.cost_usd += deploy.cost_usd
        # session.session_id = deploy.session_id or session.session_id  # see implement_feature
        data = deploy.json_data or {}

        if data.get("status") == "HUMAN_INPUT_REQUIRED":
            reason = data.get("reason") or "Deployment Agent flagged this pre-prod deploy as outside its authority."
            question = data.get("question") or ""
            options = data.get("options") or []
            diagnosis = reason
            if question:
                diagnosis += f"\n\nQuestion for a human: {question}"
            if options:
                diagnosis += f"\n\nOptions considered: {options}"
            session.mem.record_known_bug(
                session.run_id, "medium", diagnosis,
                "Requires an explicit human decision before Deployment Agent can deploy to pre-prod.",
                source="deployment-agent-human-input-required",
                needs_human=True,
                title=f"Human input required: deploy {session.feature_branch} to pre-prod",
            )
            session.pre_prod_url = None
            session.pre_prod_verified = False
            session.note(f"deploy_pre_prod: human input required -- {reason[:200]}", ok=None)
            return {
                "content": [{"type": "text", "text": f"Escalated to a human: {reason} Filed as a GitHub issue (needs_human); not deployed this cycle."}],
                "is_error": True,
            }

        ok = deploy.ok and data.get("status") != "failed"
        session.pre_prod_url = data.get("preview_url") if ok else None
        session.pre_prod_verified = False
        if ok:
            session.deployed_to_pre_prod = True
        session.note(
            f"deploy_pre_prod: ok={ok} preview_url={session.pre_prod_url!r}",
            ok=ok,
            cost_usd=deploy.cost_usd,
            turns=deploy.turns,
        )
        if not ok:
            session.mem.record_failure(session.run_id, "deployment", deploy.text)
            session.record_failure("deploy_pre_prod")
        else:
            session.record_success("deploy_pre_prod")
            if session.code_complete_issue_numbers:
                moved = session.mem.record_shipped_to_preprod(session.code_complete_issue_numbers, session.run_id)
                session.code_complete_issue_numbers = [i for i in session.code_complete_issue_numbers if i not in moved]
                session.shipped_this_cycle_issue_numbers.extend(moved)
        return {"content": [{"type": "text", "text": deploy.text[:2000]}], "is_error": not ok}

    @tool(
        "verify_pre_prod",
        "Verify the running pre-prod live instance. Required after deploy_pre_prod.",
        {},
    )
    async def verify_pre_prod(_args):
        if stop := session.check_hard_stop():
            return stop
        if session.change_risk == change_risk.TRIVIAL:
            session.note("verify_pre_prod: skipped, deploy_pre_prod classified this change as trivial", ok=True)
            return {
                "content": [{"type": "text", "text": "Nothing to verify -- deploy_pre_prod classified this as a trivial change and already merged it without a live deploy."}],
            }
        if not session.pre_prod_url:
            return {
                "content": [{"type": "text", "text": "Call deploy_pre_prod first — no live URL to verify yet."}],
                "is_error": True,
            }
        spec_for_verification = session.current_spec or session.cb_summary or "No spec available."
        test = await testing.run_pre_prod(
            session.repo, spec_for_verification, session.pre_prod_url, session.run_id, session_id=session.session_id
        )
        if stop := _check_auth_failure(session, "verify_pre_prod", test):
            return stop
        session.cost_usd += test.cost_usd
        # session.session_id = test.session_id or session.session_id  # see implement_feature
        data = test.json_data or {}
        passed = test.ok and data.get("status") != "fail"
        session.pre_prod_verified = passed
        detail = f"reachable={data.get('reachable', '?')} feature_verified={data.get('feature_verified', '?')}"
        session.note(f"verify_pre_prod: passed={passed} | {detail}", ok=passed, cost_usd=test.cost_usd, turns=test.turns)
        _file_incidental_findings(session.mem, session.run_id, data, source="testing-agent-pre-prod")
        if not passed:
            session.mem.record_failure(session.run_id, "pre-prod-verification", test.text)
            session.record_failure("verify_pre_prod")
        else:
            session.record_success("verify_pre_prod")
            if session.shipped_this_cycle_issue_numbers:
                session.mem.record_tested(session.shipped_this_cycle_issue_numbers, session.run_id)
                session.shipped_this_cycle_issue_numbers = []
            # notify_shipped choke point (b): a non-trivial change's pre-prod delivery is only
            # confirmed once live verification actually passes, not at deploy_pre_prod's own
            # success (that only means the instance is up).
            verification_result = f"verify_pre_prod passed ({detail})."
            if session.pre_prod_url:
                verification_result += f" Preview: {session.pre_prod_url}"
            _notify_shipped_pending(session, verification_result)
        if session.env.deploy_strategy == "self_hosted_vm":
            # Single-shot, ephemeral sibling -- tear it down once its report is
            deployment.teardown_self_hosted_preprod(session.repo, session.run_id)
        return {
            "content": [{"type": "text", "text": f"Live verification {'PASSED' if passed else 'FAILED'}. {test.text[:2000]}"}],
            "is_error": not passed,
        }

    @tool(
        "assess_feedback",
        "Check whether the shipped feature is measurable and name success metrics.",
        {},
    )
    async def assess_feedback(_args):
        if stop := session.check_hard_stop():
            return stop
        feature = session.current_feature or "unknown feature"
        fb = await feedback.run(session.repo, session.objective, feature, session_id=session.session_id)
        if stop := _check_auth_failure(session, "assess_feedback", fb):
            return stop
        session.cost_usd += fb.cost_usd
        # session.session_id = fb.session_id or session.session_id  # see implement_feature
        session.note("assess_feedback", ok=fb.ok, cost_usd=fb.cost_usd, turns=fb.turns)
        return {"content": [{"type": "text", "text": fb.text[:2000]}]}

    _SPAWNABLE_TOOLS = {"Read", "Write", "Edit", "Glob", "Grep", "Bash"}

    @tool(
        "spawn_custom_agent",
        "Spawn a one-off sub-agent for tasks that do not fit standard tools. "
        "Provide task_name, prompt, system_prompt, and allowed_tools from "
        "Read, Write, Edit, Glob, Grep, Bash.",
        {"task_name": str, "prompt": str, "system_prompt": str, "allowed_tools": str},
    )
    async def spawn_custom_agent(args):
        if stop := session.check_hard_stop():
            return stop
        requested = [t.strip() for t in args["allowed_tools"].split(",") if t.strip()]
        invalid = [t for t in requested if t not in _SPAWNABLE_TOOLS]
        if invalid or not requested:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"allowed_tools must be a non-empty comma-separated list from "
                        f"{sorted(_SPAWNABLE_TOOLS)}; got {args['allowed_tools']!r}.",
                    }
                ],
                "is_error": True,
            }
        spec = TaskSpec(
            name=args["task_name"],
            prompt=args["prompt"],
            system_prompt=args["system_prompt"],
            allowed_tools=requested,
        )
        result = await spawn_generic(session.repo, spec, mem=session.mem, run_id=session.run_id)
        if stop := _check_auth_failure(session, "spawn_custom_agent", result):
            return stop
        session.cost_usd += result.cost_usd
        session.note(
            f"spawn_custom_agent[{args['task_name']}]: ok={result.ok}",
            ok=result.ok,
            cost_usd=result.cost_usd,
            turns=result.turns,
        )
        if not result.ok:
            session.record_failure("spawn_custom_agent")
            return {"content": [{"type": "text", "text": f"Sub-agent failed: {result.text[:2000]}"}], "is_error": True}
        session.record_success("spawn_custom_agent")
        return {"content": [{"type": "text", "text": result.text[:2000]}]}

    _ALL_TOOL_FUNCS = (
        understand_codebase,
        check_backlog,
        discover_opportunities,
        assess_design_impact,
        implement_feature,
        run_local_tests,
        deploy_pre_prod,
        verify_pre_prod,
        assess_feedback,
        spawn_custom_agent,
    )
    _by_name = {t.name: t for t in _ALL_TOOL_FUNCS}
    orchestrator_tool_names = [m["name"] for m in agents_catalog.AGENT_METADATA["orchestrator"]["tools"]]
    try:
        tools = [_by_name[name] for name in orchestrator_tool_names]
    except KeyError as exc:
        raise RuntimeError(
            f"agents/catalog.py's orchestrator entry references tool {exc}, which has no "
            "matching @tool in _tools_for() -- catalog.py and tools.py have drifted out of sync."
        ) from exc
    for t in tools:
        t.handler = _stagnation_tracked(session, t.name, t.handler)
    return tools

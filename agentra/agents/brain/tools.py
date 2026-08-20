"""agents/brain/tools.py — Specialized sub-agent tools orchestrating the codebase."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import tool

from agentra import change_risk
from agentra.agents import architecture_review, codebase, deployment, discovery, feedback, implementation, requirements, testing
from agentra.agents import catalog as agents_catalog
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
    """Requirements Agent's JSON spec formatted as readable text.

    human_answer: human-in-the-loop escalation (GitHub issue #34) -- when
    resuming an implement_feature call that previously hit
    HUMAN_INPUT_REQUIRED, the human's answer to the blocking question gets
    woven directly into this spec text (not just the outer cycle prompt),
    so the Implementation Agent's resumed turn sees it as part of what it's
    building, not as a detached side note it might not read."""
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
    """When the backlog is genuinely empty (no in-progress feature, no
    actionable known bug, no queued feature request -- the same emptiness
    that already makes discover_opportunities the documented last resort in
    check_backlog's own tool description), durably files the single
    top-ranked opportunity as a real GitHub feature-request issue (issue
    #23) so it shows up in check_backlog's feature queue on a future cycle
    like any other GitHub-sourced feature request.

    Deliberately goes through record_feature_request (feature/agentra/
    discovery labels) -- never record_known_bug/needs_human. An opportunity
    is not a bug and must never carry the bug label or trigger a human
    escalation. Re-checks emptiness live here rather than trusting the
    caller already confirmed it earlier this turn (state can change
    between calls), and only ever files opportunities[0] -- one issue per
    empty-backlog occurrence, regardless of how many were ranked.

    Returns the created issue's {"number", "html_url"} (per
    Memory.record_feature_request), or None if the backlog wasn't actually
    empty, there was nothing to file, or filing failed."""
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


def _escalate_to_human(
    session: OrchestratorSession,
    *,
    diagnosis: str,
    question: str,
    source: str,
    title: str,
    branch: str | None = None,
    tracking_issue: int | None = None,
) -> int | None:
    """Human-in-the-loop escalation (GitHub issue #34), shared by
    implement_feature and discover_opportunities' HUMAN_INPUT_REQUIRED
    branches (deploy_pre_prod's own HUMAN_INPUT_REQUIRED path is explicitly
    out of scope for this -- see deploy_pre_prod's own handling, left
    untouched). Does everything the architecture review scoped for this
    part beyond the pre-existing needs_human GitHub issue filing:

    1. Stamps resume-correlation data (app, run_id, branch, session_id,
       tracking_issue, question) onto the filed/updated needs_human issue.
    2. Posts an outbound Slack notification if SLACK_BOT_TOKEN/
       SLACK_HUMAN_INPUT_CHANNEL are configured -- silently skipped
       otherwise (see connectors/slack.py), never surfaced as a run
       failure.
    3. Marks the run waiting_for_human immediately (session.mark_waiting_for_human).

    tracking_issue: the ORIGINAL feature/bug issue this escalation is
    blocking (implement_feature's resolves_id/sub_feature_of), distinct
    from the needs_human issue number this function returns -- a resume
    dispatched later (server/routes/human_input.py) reads this back via
    Memory.get_human_input_context to know which implement_feature call to
    continue and which tracking issue's spec to weave the human's answer
    into (see _format_spec's human_answer param). None for
    discover_opportunities, which has no resume contract in this pass.

    Returns the needs_human issue number (or None if GitHub was
    unreachable/unconfigured -- matching every other best-effort write in
    memory.py), so implement_feature can also stamp record_in_progress_branch/
    record_spec on it to make the escalation issue itself resumable when
    there's no separate tracking issue (a self-initiated idea with no
    resolves_id/sub_feature_of)."""
    issue_number = session.mem.record_known_bug(
        session.run_id, "medium", diagnosis,
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
            branch=branch, session_id=session.session_id, tracking_issue=tracking_issue,
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
        session_id=session.session_id,
    )
    session.mark_waiting_for_human(issue_number=issue_number, issue_url=issue_url, question=question, branch=branch)
    return issue_number


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
        "discover_opportunities. Priority order: (1) in-progress multi-part feature, "
        "(2) known bug, (3) feature request queue, (4) discover_opportunities. "
        "Bugs labeled need_human are filtered out. A non-null resume_branch means "
        "work was previously started there; pass it to implement_feature to continue.",
        {},
    )
    async def check_backlog(_args):
        if stop := session.check_hard_stop():
            return stop
        shipped = [f["feature"] for f in session.mem.shipped_features()]
        in_progress = session.mem.in_progress_features()
        bugs = _attach_resume_branches(session.mem, _actionable_bugs(session.mem.known_bugs()))
        queue = _attach_resume_branches(session.mem, session.mem.feature_queue())
        remaining_after_one = len(in_progress) + len(bugs) + len(queue) - 1
        batching_hint = (
            f"\n\n{remaining_after_one} more item(s) will still be waiting after you address one of "
            "these -- if what you're about to build is not a trivial fix, consider implementing "
            "several of them before your first deploy_pre_prod call this run (it deploys/verifies "
            "everything implemented so far together, see its own description), rather than paying "
            "for a full pre-prod deploy + live verification once per item."
            if remaining_after_one > 0 else ""
        )
        text = (
            f"In-progress multi-part features (resume these first): {json.dumps(in_progress, indent=2) if in_progress else '(none)'}\n\n"
            f"Known bugs awaiting a fix: {json.dumps(bugs, indent=2) if bugs else '(none)'}\n\n"
            f"Feature request queue: {json.dumps(queue, indent=2) if queue else '(none)'}\n\n"
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
            # part, per the architecture review -- resuming discovery with an
            # answer is a separate future feature): _escalate_to_human still files
            # the needs_human issue, notifies Slack, and marks this run
            # waiting_for_human, but passes branch=None since there is no
            # branch/session for a human's answer to resume onto here.
            _escalate_to_human(
                session,
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
        # only ever reached as a last resort once check_backlog has nothing
        # left -- when that's genuinely true, durably file the single
        # top-ranked opportunity as a real GitHub feature-request issue
        # (never a bug/needs_human escalation) so it survives past this
        # run and flows into check_backlog's queue like any other
        # GitHub-sourced feature request next cycle.
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
        "features. Asks the Architecture Review Agent to flag blast radius and risk before "
        "Implementation Agent starts.",
        {"feature_brief": str},
    )
    async def assess_design_impact(args):
        if stop := session.check_hard_stop():
            return stop
        if session.cb_summary is None:
            return {"content": [{"type": "text", "text": "Call understand_codebase first."}], "is_error": True}
        review = await architecture_review.run(session.repo, session.objective, args["feature_brief"], session.cb_summary)
        session.cost_usd += review.cost_usd
        session.note(f"assess_design_impact: ok={review.ok}", ok=review.ok, cost_usd=review.cost_usd, turns=review.turns)
        if not review.ok:
            session.record_failure("assess_design_impact")
            return {"content": [{"type": "text", "text": f"Design review failed: {review.text[:2000]}"}], "is_error": True}
        session.record_success("assess_design_impact")
        return {"content": [{"type": "text", "text": review.text[:3000]}]}

    @tool(
        "implement_feature",
        "Build a specific feature or fix a bug on a branch. Set resolves_id/resolves_origin "
        "if resolving an issue. For multi-part features, set more_parts_expected=true on all "
        "calls except the last, and pass sub_feature_of on subsequent parts. Set resume_branch "
        "if resuming work from check_backlog.",
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
        sub_feature_of = args.get("sub_feature_of") or ""
        more_parts_expected = bool(args.get("more_parts_expected"))
        tracking_issue = None
        if resolves_id and resolves_id.isdigit():
            tracking_issue = int(resolves_id)
        elif sub_feature_of and sub_feature_of.isdigit():
            tracking_issue = int(sub_feature_of)

        if resuming and tracking_issue is not None and session.session_id is None:
            session.session_id = session.mem.resume_session_id_for(str(tracking_issue))

        spec_dict = session.mem.get_spec(tracking_issue) if tracking_issue is not None else None
        if spec_dict is not None:
            session.note(f"requirements: reusing existing spec for issue #{tracking_issue}", ok=True)
        else:
            req = await requirements.run(session.repo, session.objective, brief, session.cb_summary)
            session.cost_usd += req.cost_usd
            session.note(f"requirements: ok={req.ok}", ok=req.ok, cost_usd=req.cost_usd, turns=req.turns)
            if req.ok and req.json_data and req.json_data.get("spec"):
                spec_dict = req.json_data
                if tracking_issue is not None:
                    session.mem.record_spec(tracking_issue, spec_dict)
        # Human-in-the-loop escalation (GitHub issue #34): if this session was
        # itself dispatched as a resume from a human's answer AND this
        # particular implement_feature call is for the tracking issue that
        # answer belongs to, weave it into the spec text so the Implementation
        # Agent's resumed turn sees it as part of what it's building (see
        # _format_spec's human_answer param). Consumed (cleared) immediately
        # after use so a later implement_feature call this same cycle -- e.g.
        # a multi-part feature's next part -- doesn't get a stale answer
        # re-injected into an unrelated spec.
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
            )
        except Exception as exc:
            session.record_failure("implement_feature")
            session.note(f"implement_feature: raised {exc!r}", ok=False)
            return {"content": [{"type": "text", "text": f"implement_feature raised: {exc}"}], "is_error": True}
        session.cost_usd += impl.cost_usd
        session.session_id = impl.session_id or session.session_id
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
                tracking_issue, session.feature_branch, session.run_id, session.session_id
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
            # is THE flow with an existing resume contract (feature_branch +
            # session_id), so route through _escalate_to_human rather than a
            # bare record_known_bug -- this additionally stamps resume
            # correlation data (including tracking_issue, so a later resume's
            # answer gets woven back into THIS issue's spec, see above),
            # posts the Slack notification, and marks the run waiting_for_human
            # immediately.
            _escalate_to_human(
                session,
                diagnosis=diagnosis,
                question=question or reason,
                source="implementation-agent-human-input-required",
                title=f"Human input required: {feature_name}",
                branch=session.feature_branch,
                tracking_issue=tracking_issue,
            )
            # Also note it on the tracking issue itself (if this call was
            # resuming/working one) -- the needs_human issue above is a
            # separate, dedicated item (dashboard/labels treat needs_human
            # specially), but a human looking at THIS issue should still see
            # that it's stalled and why, not just silently stop accumulating
            # progress comments with no explanation.
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
            session.record_failure("implement_feature")
            return {"content": [{"type": "text", "text": f"Implementation failed: {impl.text[:2000]}"}], "is_error": True}
        session.record_success("implement_feature")
        shipped = session.mem.record_shipped(
            feature_name,
            commit_sha=commit_sha,
            run_id=session.run_id,
            resolves_id=resolves_id if resolves_origin == "feature_queue" else None,
            sub_feature_of=sub_feature_of or None,
            more_parts_expected=more_parts_expected,
            session_id=session.session_id,
            known_bug_issue=resolves_id if resolves_origin == "known_bug" else None,
        )
        session.mem.append_documentation(
            f"Shipped **{feature_name}**"
            + (f" (commit `{commit_sha[:7]}`)" if commit_sha else "")
            + f": {brief[:300]}"
        )
        if resolves_id and resolves_origin == "known_bug":
            resolution_note = f"Resolved by agentra: shipped as {feature_name!r} (run {session.run_id})" + (
                f" (commit {commit_sha})" if commit_sha else ""
            )
            session.mem.clear_known_bug(resolves_id, resolution_note)
        session.current_feature = feature_name

        issue_number = shipped["issue_number"] if shipped else None
        parent_issue_number = shipped["board_issue_number"] if shipped else None
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
                        f"Implemented and committed {feature_name!r}{issue_note}. "
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
        session.cost_usd += test.cost_usd
        session.session_id = test.session_id or session.session_id
        data = test.json_data or {}
        passed = test.ok and data.get("status") != "fail"

        attempts = 0
        while not passed and attempts < MAX_SELF_HEAL_ATTEMPTS and session.feature_branch is not None:
            attempts += 1
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
            )
            session.cost_usd += fix.cost_usd
            session.session_id = fix.session_id or session.session_id
            session.note(
                f"run_local_tests: self-heal attempt {attempts} ok={fix.ok}",
                ok=fix.ok, cost_usd=fix.cost_usd, turns=fix.turns,
            )
            if not fix.ok:
                break
            test = await testing.run_local(session.repo, session.cb_summary, session.mem, session_id=session.session_id)
            session.cost_usd += test.cost_usd
            session.session_id = test.session_id or session.session_id
            data = test.json_data or {}
            passed = test.ok and data.get("status") != "fail"

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
            session.deploy_attempted = True
            session.cost_usd += deploy.cost_usd
            ok = deploy.ok
            session.pre_prod_url = None
            session.deployed_to_pre_prod = ok
            # A passing local test suite is the whole point of the TRIVIAL
            # classification -- there is no live instance to verify, so treat
            # this as already verified rather than leaving pre_prod_verified
            # false and making the LLM think verify_pre_prod still applies.
            session.pre_prod_verified = ok
            session.note(f"deploy_pre_prod: trivial change, merged only: ok={ok}", ok=ok, cost_usd=deploy.cost_usd)
            if not ok:
                session.mem.record_failure(session.run_id, "deployment", deploy.text)
                session.record_failure("deploy_pre_prod")
            else:
                session.record_success("deploy_pre_prod")
            return {
                "content": [{"type": "text", "text": f"{deploy.text[:2000]} No verify_pre_prod call needed for this change."}],
                "is_error": not ok,
            }

        strategy = deployment.PRE_PROD_STRATEGIES[session.env.deploy_strategy]
        deploy = await strategy(
            session.repo, session.env, session.feature_branch, session.run_id, session.session_id
        )
        session.deploy_attempted = True
        session.cost_usd += deploy.cost_usd
        session.session_id = deploy.session_id or session.session_id
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
        session.cost_usd += test.cost_usd
        session.session_id = test.session_id or session.session_id
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
        if session.env.deploy_strategy == "self_hosted_vm":
            # Single-shot, ephemeral sibling -- tear it down once its report is
            # produced (pass or fail) so it doesn't accumulate across features
            # tested over time.
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
        session.cost_usd += fb.cost_usd
        session.session_id = fb.session_id or session.session_id
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

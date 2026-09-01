"""agents/brain — Orchestrator Agent cycle runner and session logic."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query

from agentra.agents import architecture_review, codebase, codegraph, deployment, discovery, feedback, human_answer_judge, implementation, requirements, testing
from agentra.agents.base import _sdk_env, _sum_model_usage, log_claude_message, run_log_scope, single_prompt_stream
from agentra.agents.brain.tools import _file_incidental_findings, _format_spec, _tools_for, MAX_SELF_HEAL_ATTEMPTS
from agentra.agents.brain.prompts import SYSTEM_PROMPT
from agentra.agents.safety import make_hooks
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory
from agentra.memory.core import is_login_required_failure

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_TOOL_FAILURES = 2
STAGNATION_WINDOW = 5


def _tool_call_hash(tool_name: str, args: dict) -> str:
    payload = json.dumps({"tool": tool_name, "args": args}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class OrchestratorSession:
    repo: Path
    objective: str
    env: EnvironmentConfig
    mem: Memory
    run_id: str
    analytics_summary: str = "not available"
    cb_summary: str | None = None
    tests_passed: bool = False
    pre_prod_url: str | None = None
    deployed_to_pre_prod: bool = False
    deploy_attempted: bool = False
    pre_prod_verified: bool = False
    # Set by deploy_pre_prod (change_risk.classify_change) each time it's called --
    change_risk: str | None = None
    # Architecture Review Agent results, keyed by the exact feature_brief string they were
    # produced for. Lives only on this session instance (one per run/cycle), so a review
    # done for one brief can never leak into gating a different brief -- it's naturally
    # reset every new cycle along with the rest of this dataclass. Populated by
    # assess_design_impact and by implement_feature's automatic keyword-triggered review.
    design_reviews: dict[str, dict] = field(default_factory=dict)
    current_feature: str | None = None
    current_spec: str | None = None
    # Raw Requirements Agent spec dict for this build -- carries structured
    # acceptance_criteria (GitHub #108) that verify_pre_prod uses to screenshot
    # each UI criterion's own page. current_spec is the formatted-text view.
    current_spec_dict: dict | None = None
    feature_branch: str | None = None
    # The Claude CLI session_id for this issue's build -- captured from each
    session_id: str | None = None
    cost_usd: float = 0.0
    skip_deploy: bool = False
    actions: list[str] = field(default_factory=list)
    tool_failure_counts: dict[str, int] = field(default_factory=dict)
    hard_stop_reason: str | None = None
    recent_tool_calls: list[tuple[str, bool]] = field(default_factory=list)
    stagnation_detected: bool = False
    # Human-in-the-loop escalation (GitHub issue #34). Set by implement_feature
    waiting_for_human: bool = False
    human_input: dict | None = None
    # Set (only) when this session is itself a resume dispatched from a
    human_answer: str | None = None
    human_answer_issue: int | None = None
    # GitHub issue #42 hardening: distinct from hard_stop_reason (which is
    auth_failure_this_cycle: bool = False
    # Set True the moment the top-level orchestrator query() call actually
    orchestrator_result_received: bool = False
    # external_ids of every bug/feature-queue item check_backlog has shown this cycle --
    # implement_feature requires resolves_id/resolves_origin="new" once this is non-empty.
    backlog_ids_shown: set[str] = field(default_factory=set)
    # issue_numbers stamped status:code_complete by implement_feature this cycle, not yet
    # merged to pre-prod -- deploy_pre_prod moves these to status:shipped on success and
    # empties this list into shipped_this_cycle_issue_numbers.
    code_complete_issue_numbers: list[str] = field(default_factory=list)
    # issue_numbers stamped status:shipped this cycle (by the deploy_pre_prod call above) --
    # verify_pre_prod moves these to status:tested on a passing live check.
    shipped_this_cycle_issue_numbers: list[str] = field(default_factory=list)
    # GitHub issue #78: feature branches whose post-commit push to GitHub failed even after
    # retries -- the commit is NOT confirmed durable there. Deterministic, per-branch
    # hard-stop checked by deploy_pre_prod/record_code_complete (see check_push_failure) so a
    # feature in this state can never be deployed or stamped "code complete", independent of
    # whether the orchestrator LLM notices impl.ok=False / the error text in result.text.
    push_failed_branches: set[str] = field(default_factory=set)
    # Shipped-but-not-yet-Slack-notified items, populated by implement_feature each time
    # record_code_complete succeeds for a final part (not an intermediate multi-part one) --
    # each entry: {issue_number, board_issue_number, title}. Drained (and cleared) at the two
    # notify_shipped choke points in tools.py: deploy_pre_prod's trivial-merge success and
    # verify_pre_prod's pass, never at the earlier status:shipped label stamp itself.
    pending_shipped_notifications: list[dict] = field(default_factory=list)

    @property
    def app_name(self) -> str:
        return self.repo.name

    def mark_waiting_for_human(
        self,
        *,
        issue_number: int | None,
        issue_url: str | None,
        question: str,
        branch: str | None = None,
        category: str | None = None,
    ) -> None:
        """Called by implement_feature/discover_opportunities right after filing/updating the needs_human GitHub issue for a HUMAN_INPUT_REQUIRED result. category (e.g. "infra_cost", "product_direction", "implementation" -- see _escalate_to_human) is threaded into the structured human_input dict, not just the GitHub issue body, so the Firestore/local-JSON run record itself is queryable/chartable by escalation reason instead of only discoverable by grepping issue text."""
        self.waiting_for_human = True
        self.human_input = {
            "issue_number": issue_number,
            "issue_url": issue_url,
            "question": question,
            "branch": branch,
            "category": category,
            "session_id": self.session_id,
            "app": self.app_name,
            "waiting_since": time.time(),
        }
        self.hard_stop_reason = (
            "Waiting on a human to answer a blocking question "
            f"(GitHub issue #{issue_number}" + (f", {issue_url}" if issue_url else "") + "). "
            "Stop calling tools -- summarize what's blocked and end your turn; this run will "
            "resume automatically once the human answers."
        )
        from agentra import registry

        registry.record_run(self.run_id, status="waiting_for_human", human_input=self.human_input)

    def note(
        self, action: str, *, agent: str | None = None, ok: bool | None = None, cost_usd: float = 0.0, turns: int | None = None,
        input_tokens: int | None = None, output_tokens: int | None = None,
        cache_read_input_tokens: int | None = None, cache_creation_input_tokens: int | None = None,
    ) -> None:
        self.actions.append(action)
        # "[Orchestrator]" prefix, matching base.py's log_claude_message convention
        self.mem.log(self.run_id, f"[Orchestrator] {action}")
        print(f"[agentra] {action} | cost so far: ${self.cost_usd:.4f}", flush=True)
        from agentra import registry

        resolved_agent = agent or action.split(":", 1)[0].split("[", 1)[0].strip()
        registry.record_agent_step(
            self.app_name, self.run_id, resolved_agent, ok, cost_usd, turns, action,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens, cache_creation_input_tokens=cache_creation_input_tokens,
        )

    def check_hard_stop(self) -> dict | None:
        # No per-cycle cost cap here: the previous $3.00 hard stop never actually
        if self.hard_stop_reason:
            return {"content": [{"type": "text", "text": self.hard_stop_reason}], "is_error": True}
        return None

    def mark_push_failed(self, branch: str) -> None:
        """Record that `branch`'s post-commit push to GitHub failed even after retries -- see check_push_failure."""
        self.push_failed_branches.add(branch)

    def check_push_failure(self, branch: str | None) -> dict | None:
        """Same is_error-dict pattern as check_hard_stop, scoped to one feature branch instead of the whole session -- deploy_pre_prod/record_shipped must call this and refuse to proceed for `branch` if it's set (GitHub issue #78)."""
        if branch is not None and branch in self.push_failed_branches:
            return {
                "content": [{
                    "type": "text",
                    "text": (
                        f"Refused: feature branch {branch!r} failed to push to GitHub even after "
                        "retries -- the commit is not confirmed durable there, so it cannot be "
                        "deployed or marked shipped. Re-run implement_feature to retry the push, "
                        "or investigate the underlying git/auth failure."
                    ),
                }],
                "is_error": True,
            }
        return None

    def record_failure(self, tool_name: str) -> None:
        self.tool_failure_counts[tool_name] = self.tool_failure_counts.get(tool_name, 0) + 1
        if self.tool_failure_counts[tool_name] >= MAX_CONSECUTIVE_TOOL_FAILURES:
            self.hard_stop_reason = (
                f"{tool_name} has now failed {self.tool_failure_counts[tool_name]} times in a "
                "row this run. That's almost certainly an infrastructure problem, not something "
                "a different brief/approach will fix -- stop calling it, summarize what happened "
                "and end your turn."
            )

    def record_success(self, tool_name: str) -> None:
        self.tool_failure_counts[tool_name] = 0

    def progress_snapshot(self) -> tuple:
        return (
            self.cb_summary is not None,
            self.tests_passed,
            self.pre_prod_url,
            self.deployed_to_pre_prod,
            self.pre_prod_verified,
            self.current_feature,
            self.feature_branch,
        )

    def record_tool_call(self, tool_name: str, args: dict, state_changed: bool) -> None:
        if self.hard_stop_reason:
            return
        call_hash = _tool_call_hash(tool_name, args)
        self.recent_tool_calls.append((call_hash, state_changed))
        if len(self.recent_tool_calls) > STAGNATION_WINDOW:
            self.recent_tool_calls.pop(0)
        if len(self.recent_tool_calls) < STAGNATION_WINDOW:
            return
        hashes = {h for h, _ in self.recent_tool_calls}
        any_progress = any(changed for _, changed in self.recent_tool_calls)
        if len(hashes) == 1 and not any_progress:
            self.stagnation_detected = True
            self.hard_stop_reason = (
                f"No progress in the last {STAGNATION_WINDOW} tool calls -- {tool_name!r} was "
                "called with the same arguments repeatedly and nothing about the run's state "
                "changed. Stop calling tools -- summarize what happened and end your turn."
            )
            print(f"[agentra] stagnation breaker tripped: {self.hard_stop_reason}", flush=True)


@dataclass
class AutonomousCycleReport:
    run_id: str
    actions: list[str]
    final_message: str
    cost_usd: float
    feature: str | None = None
    # Human-in-the-loop escalation (GitHub issue #34): set when this cycle
    waiting_for_human: bool = False
    human_input: dict | None = None


async def run_autonomous_cycle(
    repo: Path,
    objective: str,
    env: EnvironmentConfig,
    analytics_summary: str = "not available",
    feature: str | None = None,
    skip_deploy: bool = False,
    max_turns: int = 40,
    run_id: str | None = None,
    human_answer: str | None = None,
    human_answer_issue: int | None = None,
) -> AutonomousCycleReport:
    """human_answer/human_answer_issue: set only when this call is itself a resume dispatched from a human's HUMAN_INPUT_REQUIRED answer (dashboard submission or a GitHub issue comment -- see server/routes/human_input.py)."""
    repo = repo.resolve()
    mem = Memory(repo)
    run_id = run_id or uuid.uuid4().hex[:8]
    try:
        return await _run_autonomous_cycle_body(
            repo, mem, run_id, objective, env, analytics_summary, feature, skip_deploy,
            max_turns, human_answer, human_answer_issue,
        )
    finally:
        # Bug #77: single full-document Firestore flush once this run reaches a
        # terminal state (completed/failed/waiting_for_human) rather than a
        # per-log-line read+write.
        mem.finalize_run_log(run_id)


async def _run_autonomous_cycle_body(
    repo: Path,
    mem: Memory,
    run_id: str,
    objective: str,
    env: EnvironmentConfig,
    analytics_summary: str,
    feature: str | None,
    skip_deploy: bool,
    max_turns: int,
    human_answer: str | None,
    human_answer_issue: int | None,
) -> AutonomousCycleReport:
    session = OrchestratorSession(
        repo=repo,
        objective=objective,
        env=env,
        mem=mem,
        run_id=run_id,
        analytics_summary=analytics_summary,
        skip_deploy=skip_deploy,
        human_answer=human_answer,
        human_answer_issue=human_answer_issue,
    )
    # Pre-seed from any existing cached scan so the model doesn't have to spend
    session.cb_summary = mem.read("architecture", "codebase") or None
    # Same reasoning applies to the code graph: codebase.run_cached is the only
    if session.cb_summary:
        graph_summary = codegraph.load_or_build(repo)
        if graph_summary:
            session.cb_summary += graph_summary
    session.note(
        f"autonomous cycle start | objective={objective!r} feature_hint={feature!r} skip_deploy={skip_deploy}",
        agent="cycle",
    )

    # GitHub issue #60 hardening: close out bugs matching an already-handled
    # transient/quota condition (e.g. Claude Code CLI weekly/usage-limit
    # errors) that were filed before that detection existed -- no need to
    # wait for a successful attempt this cycle, unlike clear_resolved_auth_bugs.
    cleared_transient = mem.clear_resolved_transient_bugs(run_id)
    if cleared_transient:
        refs = ", ".join(f"#{c}" for c in cleared_transient)
        session.note(
            f"auto-closed known bug(s) matching an already-handled transient/quota condition ({refs}) -- "
            "no code fix needed, closing so they stop resurfacing in the backlog",
            agent="cycle", ok=True,
        )

    blocking = mem.blocking_bugs()
    # GitHub issue #42 hardening: a Claude Code auth/login-failure blocking
    hard_blocking = [b for b in blocking if not is_login_required_failure(f"{b.get('diagnosis', '')}\n{b.get('proposed_fix', '')}")]
    auth_blocking = [b for b in blocking if b not in hard_blocking]
    if hard_blocking:
        refs = ", ".join(f"#{b['external_id']}" for b in hard_blocking)
        session.hard_stop_reason = (
            f"Blocked: {len(hard_blocking)} open bug(s) labeled blocking_agentra ({refs}) need a human before "
            "agentra can make further progress. See the issue(s) for diagnosis -- not retrying."
        )
        session.note(f"autonomous cycle blocked by open blocking_agentra bug(s): {refs}", agent="cycle", ok=False)
    elif auth_blocking:
        refs = ", ".join(f"#{b['external_id']}" for b in auth_blocking)
        session.note(
            f"open Claude Code auth-failure blocking bug(s) found ({refs}) -- attempting this cycle "
            "anyway to cheaply verify whether a human has re-authenticated since; will auto-clear "
            "the bug(s) if this run gets a real result back, or fail fast again (no extra retries) "
            "if not",
            agent="cycle",
        )

    tools = _tools_for(session)
    server = create_sdk_mcp_server(name="agentra_brain", tools=tools)
    allowed_tools = [f"mcp__agentra_brain__{t.name}" for t in tools]

    prompt = f"Business objective: {objective}\n"
    if feature:
        prompt += f"A feature has been suggested to prioritize: {feature}\n"
    if skip_deploy:
        prompt += "Deployment is disabled for this run — do not call deploy_pre_prod or verify_pre_prod.\n"
    if session.cb_summary:
        prompt += (
            "\nA codebase understanding summary from a prior cycle is already loaded "
            "(see understand_codebase's tool description) — you do not need to call "
            "understand_codebase again unless you have a specific reason to think it's "
            "stale:\n" + session.cb_summary[:4000] + "\n"
        )
    prompt += "Decide what to do and carry it out."

    options = ClaudeAgentOptions(
        cwd=str(repo),
        system_prompt=SYSTEM_PROMPT.format(objective=objective),
        mcp_servers={"agentra_brain": server},
        # tools=[] disables every built-in tool (Bash, Read, Write, WebSearch,
        tools=[],
        allowed_tools=allowed_tools,
        # Defense-in-depth to match every other agent (agents/base.py's
        hooks=make_hooks(allow_prod=False),
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        env=_sdk_env(),
    )

    print(f"[agentra] run {run_id} starting | objective={objective!r}", flush=True)
    final_text = session.hard_stop_reason or ""
    if not session.hard_stop_reason:
        try:
            with run_log_scope(lambda line: mem.log(run_id, line)):
                async for message in query(prompt=single_prompt_stream(prompt), options=options):
                    log_claude_message(message, lambda line: mem.log(run_id, f"[Orchestrator] {line}"))
                    if isinstance(message, ResultMessage):
                        final_text = message.result or ""
                        turn_cost = message.total_cost_usd or 0.0
                        session.cost_usd += turn_cost
                        session.orchestrator_result_received = True
                        # GitHub issue #74: this cost is real spend by the
                        # orchestrator's own reasoning, not a sub-agent's --
                        # record it as its own agent_steps entry (agent="cycle")
                        # instead of only folding it into the invisible
                        # session.cost_usd run aggregate, so the dashboard's
                        # per-agent cost breakdown stops showing $0.00 for
                        # "Orchestrator" despite real spend happening here.
                        session.note(
                            f"orchestrator reasoning: ok={not message.is_error} "
                            f"turns={message.num_turns} cost=${turn_cost:.4f}",
                            agent="cycle", ok=not message.is_error,
                            cost_usd=turn_cost, turns=message.num_turns,
                            **_sum_model_usage(message.model_usage),
                        )
        except Exception as exc:
            # GitHub issue #42: this is the exact spot a prior cycle crashed
            if is_login_required_failure(str(exc)):
                session.auth_failure_this_cycle = True
                final_text = (
                    "Claude Code session is not authenticated -- run /login and re-trigger. "
                    f"(CLI reported: {exc}). Filed as a blocking bug and notified via Slack (if "
                    "configured) so future cycles stop immediately instead of repeating this "
                    "failure pointlessly."
                )
                session.note(f"autonomous cycle blocked: Claude Code authentication failure: {exc}", agent="cycle", ok=False)
            else:
                final_text = f"autonomous cycle raised: {exc}"
                session.note(f"autonomous cycle crashed: {exc}", agent="cycle", ok=False)
            mem.record_failure(run_id, "autonomous-cycle", final_text)

    if session.orchestrator_result_received and not session.auth_failure_this_cycle:
        cleared = mem.clear_resolved_auth_bugs(run_id)
        if cleared:
            refs = ", ".join(f"#{c}" for c in cleared)
            session.note(
                f"auto-cleared previously-blocking Claude Code auth-failure bug(s) ({refs}) -- "
                "this run confirmed re-authentication succeeded",
                agent="cycle", ok=True,
            )

    if session.stagnation_detected:
        stagnation_note = (
            f"Cycle terminated early: no progress detected. {session.hard_stop_reason}"
        )
        final_text = f"{final_text}\n\n{stagnation_note}" if final_text else stagnation_note
        session.note("stagnation breaker: cycle terminated early, no progress", agent="cycle", ok=False)
    elif session.waiting_for_human:
        pause_note = f"Cycle paused, waiting on a human answer. {session.hard_stop_reason}"
        final_text = f"{final_text}\n\n{pause_note}" if final_text else pause_note
        session.note("human-in-the-loop: cycle paused, waiting_for_human", agent="cycle", ok=None)

    persist_error = deployment.persist_audit_trail(repo, env.pre_prod_branch)
    if persist_error:
        session.note(f"persist_audit_trail: failed: {persist_error}")

    session.note("autonomous cycle complete", agent="cycle", ok=True)
    print(f"[agentra] run {run_id} finished | total cost: ${session.cost_usd:.4f}", flush=True)

    return AutonomousCycleReport(
        run_id=run_id,
        actions=session.actions,
        final_message=final_text,
        cost_usd=session.cost_usd,
        feature=session.current_feature,
        waiting_for_human=session.waiting_for_human,
        human_input=session.human_input,
    )

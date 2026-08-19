"""agents/brain — Orchestrator Agent cycle runner and session logic.

Orchestrates specialized sub-agents via Claude Agent SDK by presenting a
menu of nine tools (defined in tools.py) and directing the cycle toward the
stated business objective.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query

from agentra.agents import architecture_review, codebase, codegraph, deployment, discovery, feedback, implementation, requirements, testing
from agentra.agents.base import log_claude_message, run_log_scope, single_prompt_stream
from agentra.agents.brain.tools import _file_incidental_findings, _format_spec, _tools_for, MAX_SELF_HEAL_ATTEMPTS
from agentra.agents.brain.prompts import SYSTEM_PROMPT
from agentra.agents.safety import make_hooks
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory

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
    # TRIVIAL or STANDARD. None until the first deploy_pre_prod call this run.
    change_risk: str | None = None
    current_feature: str | None = None
    current_spec: str | None = None
    feature_branch: str | None = None
    # The Claude CLI session_id for this issue's build -- captured from each
    # sub-agent's AgentResult.session_id and fed back into the next one
    # (implement_feature -> run_local_tests -> deploy_pre_prod ->
    # verify_pre_prod -> assess_feedback) so the whole SIM shares one
    # conversation instead of each step cold-starting its own.
    session_id: str | None = None
    cost_usd: float = 0.0
    skip_deploy: bool = False
    actions: list[str] = field(default_factory=list)
    tool_failure_counts: dict[str, int] = field(default_factory=dict)
    hard_stop_reason: str | None = None
    recent_tool_calls: list[tuple[str, bool]] = field(default_factory=list)
    stagnation_detected: bool = False

    @property
    def app_name(self) -> str:
        return self.repo.name

    def note(
        self, action: str, *, agent: str | None = None, ok: bool | None = None, cost_usd: float = 0.0, turns: int | None = None
    ) -> None:
        self.actions.append(action)
        self.mem.log(self.run_id, action)
        print(f"[agentra] {action} | cost so far: ${self.cost_usd:.4f}", flush=True)
        from agentra import registry

        resolved_agent = agent or action.split(":", 1)[0].split("[", 1)[0].strip()
        registry.record_agent_step(self.app_name, self.run_id, resolved_agent, ok, cost_usd, turns, action)

    def check_hard_stop(self) -> dict | None:
        # No per-cycle cost cap here: the previous $3.00 hard stop never actually
        # bounded spend -- confirmed live via VM run logs, cycles routinely
        # finishing at $4-5+ with the cap's own "Cycle cost cap reached" message
        # never once printing. check_hard_stop only runs at the START of each
        # tool call, so one expensive call (implement_feature routinely costs
        # $1-4 by itself) can jump straight past the cap in a single step; by
        # the time the next call would be gated, the model has often already
        # finished its turn. Net effect was pure downside: cycles occasionally
        # got cut off mid-implementation (see GitHub issue #2, "not able to
        # pass implementation agent") for a limit that wasn't actually
        # capping anything. self.cost_usd is still tracked for the dashboard.
        if self.hard_stop_reason:
            return {"content": [{"type": "text", "text": self.hard_stop_reason}], "is_error": True}
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


async def run_autonomous_cycle(
    repo: Path,
    objective: str,
    env: EnvironmentConfig,
    analytics_summary: str = "not available",
    feature: str | None = None,
    skip_deploy: bool = False,
    max_turns: int = 40,
    run_id: str | None = None,
) -> AutonomousCycleReport:
    repo = repo.resolve()
    mem = Memory(repo)
    run_id = run_id or uuid.uuid4().hex[:8]
    session = OrchestratorSession(
        repo=repo,
        objective=objective,
        env=env,
        mem=mem,
        run_id=run_id,
        analytics_summary=analytics_summary,
        skip_deploy=skip_deploy,
    )
    # Pre-seed from any existing cached scan so the model doesn't have to spend
    # a call on understand_codebase just to satisfy the "cb_summary is not None"
    # gate every other tool checks -- codebase.run_cached (called by the
    # understand_codebase tool itself) already treats an existing cache as
    # authoritative rather than re-scanning, so seeding it here is consistent,
    # not a separate cache. See codebase.run_cached's docstring / GitHub issue
    # #20 ("orchestrator firing codebase agent every time even though
    # codebase.md available") for why this needed fixing in the first place.
    session.cb_summary = mem.read("architecture", "codebase") or None
    # Same reasoning applies to the code graph: codebase.run_cached is the only
    # place that calls codegraph.load_or_build, but on a repo with a cache this
    # pre-seed means understand_codebase (and therefore run_cached) may never
    # get called at all this cycle -- confirmed live, this exact gap left
    # mcp_config(repo) returning {} for assess_design_impact on every cycle of
    # a mature/cached repo, silently never building a graph. Build/reuse it
    # here too so it's available regardless of whether the model calls
    # understand_codebase.
    if session.cb_summary:
        graph_summary = codegraph.load_or_build(repo)
        if graph_summary:
            session.cb_summary += graph_summary
    session.note(
        f"autonomous cycle start | objective={objective!r} feature_hint={feature!r} skip_deploy={skip_deploy}",
        agent="cycle",
    )

    blocking = mem.blocking_bugs()
    if blocking:
        refs = ", ".join(f"#{b['external_id']}" for b in blocking)
        session.hard_stop_reason = (
            f"Blocked: {len(blocking)} open bug(s) labeled blocking_agentra ({refs}) need a human before "
            "agentra can make further progress. See the issue(s) for diagnosis -- not retrying."
        )
        session.note(f"autonomous cycle blocked by open blocking_agentra bug(s): {refs}", agent="cycle", ok=False)

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
        # ToolSearch, Task, ...) -- confirmed live (run 26bf7dee, 2026-08-18)
        # that allowed_tools alone does NOT restrict availability, only
        # auto-approval: per claude_agent_sdk's own docs, "to restrict which
        # tools are available at all, use `tools`", and permission_mode=
        # "bypassPermissions" auto-approves every tool, not just the ones
        # listed in allowed_tools. Without this, the orchestrator's system
        # prompt claim ("you do not have Read/Write/Edit/Bash yourself...
        # production is deliberately not reachable... under any
        # circumstance") was pure prose with no enforcement -- that live run
        # minted its own GitHub App installation token via raw Bash/python3
        # and ran unrestricted git/gh/curl commands, entirely outside the
        # ten sanctioned MCP tools and their audit trail. This does not
        # affect the MCP server's own tools (mcp_servers/allowed_tools is a
        # separate mechanism from the built-in --tools flag this maps to).
        tools=[],
        allowed_tools=allowed_tools,
        # Defense-in-depth to match every other agent (agents/base.py's
        # run_agent already does this) -- belt-and-suspenders in case a
        # future change reintroduces a built-in tool here; also gives
        # denials a durable audit-trail line via safety.py's _record_denial.
        hooks=make_hooks(allow_prod=False),
        permission_mode="bypassPermissions",
        max_turns=max_turns,
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
                        session.cost_usd += message.total_cost_usd or 0.0
        except Exception as exc:
            final_text = f"autonomous cycle raised: {exc}"
            session.note(f"autonomous cycle crashed: {exc}", agent="cycle", ok=False)
            mem.record_failure(run_id, "autonomous-cycle", final_text)

    if session.stagnation_detected:
        stagnation_note = (
            f"Cycle terminated early: no progress detected. {session.hard_stop_reason}"
        )
        final_text = f"{final_text}\n\n{stagnation_note}" if final_text else stagnation_note
        session.note("stagnation breaker: cycle terminated early, no progress", agent="cycle", ok=False)

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
    )

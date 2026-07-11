"""Orchestrator Agent (vision.md 5.1) as a real decision-maker.

orchestrator.run_cycle() hardcodes the sequence (codebase -> discovery ->
implementation -> testing -> deploy -> feedback) in plain Python. This
module is the alternative vision.md actually describes: an agent that
"decides next best action" — it sees a fixed menu of eight tools, one per
specialized agent (or agent mode), and chooses which to call, in what order,
and when this run is done. There is no hardcoded script here; the sequence
you see in a given run is a real decision the model made, not a lookup.

Safety boundary, unchanged from the rest of the system: this "brain" is
never given Read/Write/Edit/Bash directly, only the eight tools below.
Each tool delegates to one of the existing, narrowly-scoped agents
(agents/codebase.py, discovery.py, etc.) — that's where actual filesystem
and shell access lives, gated exactly as it was before this module existed.
Production is deliberately not one of the eight tools; promote_prod() stays
reachable only via `agentos promote` (human) or the debug-prod
auto-remediate path, never from an autonomous sequencing decision. And the
one invariant that must hold no matter what the model decides — never
deploy before local tests pass — is enforced in deploy_pre_prod's handler
itself (a real boolean check), not left to the system prompt.

Testing is deliberately two tools, not one: run_local_tests verifies the
code; verify_pre_prod independently verifies the live deployment afterward
(agents/testing.py has the full reasoning on why these are distinct passes).
"""

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query, tool

from agentos.agents import codebase, deployment, discovery, feedback, implementation, testing
from agentos.agents.base import single_prompt_stream
from agentos.environments import EnvironmentConfig, feature_branch_name, slug
from agentos.memory import Memory
from agentos.ranking import rank

# Circuit breaker thresholds. Retrying a failing tool is only useful when the failure
# is about *what* was tried (a bad brief, a flaky test) -- an infrastructure error (a
# missing directory, a broken subprocess call) looks identical to the orchestrator LLM
# turn over turn, and prose telling it "don't retry forever" isn't reliable (see
# implementation.py's module docstring on why this codebase does control flow in Python,
# not prompts). So: cap consecutive failures of the *same* tool, and cap total spend,
# both enforced deterministically rather than left to the model's judgment.
MAX_CONSECUTIVE_TOOL_FAILURES = 2
MAX_CYCLE_COST_USD = 3.0


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
    pre_prod_verified: bool = False
    current_feature: str | None = None
    # Set on the first implement_feature call and reused for every later call in this
    # same session -- this run's whole line of work (possibly several implement_feature
    # calls before one deploy_pre_prod) belongs on one branch, not a fresh one each call,
    # or earlier commits would get orphaned on branches deploy_pre_prod never merges.
    feature_branch: str | None = None
    cost_usd: float = 0.0
    skip_deploy: bool = False
    actions: list[str] = field(default_factory=list)
    tool_failure_counts: dict[str, int] = field(default_factory=dict)
    hard_stop_reason: str | None = None

    def note(self, action: str) -> None:
        self.actions.append(action)
        self.mem.log(self.run_id, action)
        print(f"[agentos] {action} | cost so far: ${self.cost_usd:.4f}", flush=True)

    def check_hard_stop(self) -> dict | None:
        """Call at the top of every tool. Returns an error tool_result once this run has
        tripped a breaker, so every subsequent call refuses instead of retrying -- forcing
        the orchestrator LLM to wrap up rather than hoping it remembers to stop on its own."""
        if self.cost_usd >= MAX_CYCLE_COST_USD:
            self.hard_stop_reason = (
                f"Cycle cost cap (${MAX_CYCLE_COST_USD:.2f}) reached. Stop calling tools -- "
                "summarize what happened and end your turn."
            )
        if self.hard_stop_reason:
            return {"content": [{"type": "text", "text": self.hard_stop_reason}], "is_error": True}
        return None

    def record_failure(self, tool_name: str) -> None:
        """Track consecutive failures of one tool. On the Nth in a row, trip the breaker --
        this is deliberately per-tool-name, not global, so a run that e.g. fails
        run_local_tests twice then succeeds at everything else isn't punished for a
        problem that already resolved."""
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


def _tools_for(session: OrchestratorSession) -> list:
    @tool(
        "understand_codebase",
        "Scan the repo and produce/refresh the codebase understanding summary. "
        "Call this before discover_opportunities or implement_feature.",
        {},
    )
    async def understand_codebase(_args):
        if stop := session.check_hard_stop():
            return stop
        cb = await codebase.run(session.repo)
        session.cost_usd += cb.cost_usd
        session.mem.write("architecture", "codebase", cb.text)
        if cb.ok:
            session.cb_summary = cb.text
            session.record_success("understand_codebase")
        else:
            session.record_failure("understand_codebase")
        session.note(f"understand_codebase: ok={cb.ok}")
        return {
            "content": [{"type": "text", "text": f"[{'ok' if cb.ok else 'failed'}] {cb.text[:4000]}"}],
            "is_error": not cb.ok,
        }

    @tool(
        "check_backlog",
        "See what's already shipped (don't repeat it), what known bugs are pending "
        "(a confirmed bug always outranks a nice-to-have feature), and what's in the "
        "feature request queue (customer/admin submitted -- outranks your own ideation).",
        {},
    )
    async def check_backlog(_args):
        if stop := session.check_hard_stop():
            return stop
        shipped = session.mem.shipped_features()
        bugs = session.mem.known_bugs()
        queue = session.mem.feature_queue()
        text = (
            f"Already shipped: {shipped or '(none)'}\n\n"
            f"Known bugs awaiting a fix: {json.dumps(bugs, indent=2) if bugs else '(none)'}\n\n"
            f"Feature request queue: {json.dumps(queue, indent=2) if queue else '(none)'}"
        )
        session.note("check_backlog")
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "discover_opportunities",
        "Ask the Product Discovery Agent for ranked feature opportunities from the "
        "codebase, analytics, and backlog. Requires understand_codebase first.",
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
            session.mem.shipped_features(),
            session.mem.known_bugs(),
            session.mem.feature_queue(),
        )
        session.cost_usd += disc.cost_usd
        session.mem.write("decisions", f"{session.run_id}-discovery", disc.text)
        opportunities = rank((disc.json_data or {}).get("opportunities", []))
        session.note(f"discover_opportunities: {len(opportunities)} candidates")
        if not disc.ok or not opportunities:
            session.record_failure("discover_opportunities")
            return {"content": [{"type": "text", "text": "Discovery failed to produce opportunities."}], "is_error": True}
        session.record_success("discover_opportunities")
        return {"content": [{"type": "text", "text": json.dumps(opportunities, indent=2)}]}

    @tool(
        "implement_feature",
        "Build a specific feature. Pass a concrete, self-contained brief, not just a name.",
        {"feature_brief": str},
    )
    async def implement_feature(args):
        if stop := session.check_hard_stop():
            return stop
        if session.cb_summary is None:
            return {"content": [{"type": "text", "text": "Call understand_codebase first."}], "is_error": True}
        brief = args["feature_brief"]
        if session.feature_branch is None:
            session.feature_branch = feature_branch_name(session.env, session.run_id, brief)
        try:
            impl = await implementation.run(
                session.repo, session.objective, brief, session.cb_summary, session.env, session.feature_branch
            )
        except Exception as exc:
            # A raised exception here (as opposed to AgentResult(ok=False, ...)) means
            # something broke below the agent turn itself -- e.g. Memory.write() hitting a
            # directory git checkout deleted out from under it. That's exactly the class of
            # infra failure a different brief can't fix, so count it the same as an ok=False
            # result rather than letting it bubble up and silently end the whole cycle.
            session.record_failure("implement_feature")
            session.note(f"implement_feature: raised {exc!r}")
            return {"content": [{"type": "text", "text": f"implement_feature raised: {exc}"}], "is_error": True}
        session.cost_usd += impl.cost_usd
        feature_name = (impl.json_data or {}).get("feature") or brief.split(":")[0].strip()
        session.mem.write("features", f"{session.run_id}-{slug(feature_name)}", impl.text)
        session.tests_passed = False  # any new change invalidates the last test result
        session.pre_prod_verified = False  # ...and any prior live verification too
        session.note(f"implement_feature: ok={impl.ok} feature={feature_name!r}")
        if not impl.ok:
            session.mem.write("failures", f"{session.run_id}-implementation", impl.text)
            session.record_failure("implement_feature")
            return {"content": [{"type": "text", "text": f"Implementation failed: {impl.text[:2000]}"}], "is_error": True}
        session.record_success("implement_feature")
        session.mem.record_shipped(feature_name)
        session.current_feature = feature_name
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Implemented and committed {feature_name!r}. Call run_local_tests before deploy_pre_prod.",
                }
            ]
        }

    @tool(
        "run_local_tests",
        "Independently verify the code itself (lint/typecheck/unit/integration) in the "
        "working directory. Required before deploy_pre_prod will do anything — it refuses "
        "if this hasn't passed since the last implementation. This does not touch anything "
        "deployed; see verify_pre_prod for that.",
        {},
    )
    async def run_local_tests(_args):
        if stop := session.check_hard_stop():
            return stop
        if session.cb_summary is None:
            return {"content": [{"type": "text", "text": "Call understand_codebase first."}], "is_error": True}
        test = await testing.run_local(session.repo, session.cb_summary)
        session.cost_usd += test.cost_usd
        passed = test.ok and (test.json_data or {}).get("status") != "fail"
        session.tests_passed = passed
        session.note(f"run_local_tests: passed={passed}")
        if not passed:
            session.mem.write("failures", f"{session.run_id}-testing", test.text)
            session.record_failure("run_local_tests")
        else:
            session.record_success("run_local_tests")
        return {
            "content": [{"type": "text", "text": f"Local tests {'PASSED' if passed else 'FAILED'}. {test.text[:2000]}"}],
            "is_error": not passed,
        }

    @tool(
        "deploy_pre_prod",
        "Deploy the current state to the pre-prod environment.",
        {},
    )
    async def deploy_pre_prod(_args):
        if stop := session.check_hard_stop():
            return stop
        if session.skip_deploy:
            session.note("deploy_pre_prod: skipped (skip_deploy set for this run)")
            return {"content": [{"type": "text", "text": "Skipped: this run was started with deploy disabled."}]}
        if not session.tests_passed:
            session.note("deploy_pre_prod: refused, tests not passed")
            return {
                "content": [{"type": "text", "text": "Refused: run_local_tests must pass before deploying to pre-prod."}],
                "is_error": True,
            }
        if session.feature_branch is None:
            session.note("deploy_pre_prod: refused, no feature branch (implement_feature never called)")
            return {
                "content": [{"type": "text", "text": "Refused: nothing to deploy -- call implement_feature first."}],
                "is_error": True,
            }
        deploy = await deployment.deploy_pre_prod(session.repo, session.env, session.feature_branch)
        session.cost_usd += deploy.cost_usd
        ok = deploy.ok and (deploy.json_data or {}).get("status") != "failed"
        session.pre_prod_url = (deploy.json_data or {}).get("preview_url") if ok else None
        session.pre_prod_verified = False
        session.note(f"deploy_pre_prod: ok={ok} preview_url={session.pre_prod_url!r}")
        if not ok:
            session.mem.write("failures", f"{session.run_id}-deployment", deploy.text)
            session.record_failure("deploy_pre_prod")
        else:
            session.record_success("deploy_pre_prod")
        return {"content": [{"type": "text", "text": deploy.text[:2000]}], "is_error": not ok}

    @tool(
        "verify_pre_prod",
        "Independently verify the LIVE pre-prod deployment (not local code — the actual "
        "running instance). Required after deploy_pre_prod before assess_feedback is "
        "meaningful; catches problems local tests structurally cannot see (build failures, "
        "missing production env vars, a route that only breaks once deployed).",
        {},
    )
    async def verify_pre_prod(_args):
        if stop := session.check_hard_stop():
            return stop
        if not session.pre_prod_url:
            return {
                "content": [{"type": "text", "text": "Call deploy_pre_prod first — no live URL to verify yet."}],
                "is_error": True,
            }
        test = await testing.run_pre_prod(session.repo, session.cb_summary or "", session.pre_prod_url)
        session.cost_usd += test.cost_usd
        passed = test.ok and (test.json_data or {}).get("status") != "fail"
        session.pre_prod_verified = passed
        session.note(f"verify_pre_prod: passed={passed}")
        if not passed:
            session.mem.write("failures", f"{session.run_id}-pre-prod-verification", test.text)
            session.record_failure("verify_pre_prod")
        else:
            session.record_success("verify_pre_prod")
        return {
            "content": [{"type": "text", "text": f"Live verification {'PASSED' if passed else 'FAILED'}. {test.text[:2000]}"}],
            "is_error": not passed,
        }

    @tool(
        "assess_feedback",
        "Check whether the shipped feature is actually measurable (instrumentation) and "
        "name concrete success metrics. Call after verify_pre_prod has passed (or, if this "
        "run has deployment disabled, after run_local_tests has passed).",
        {},
    )
    async def assess_feedback(_args):
        if stop := session.check_hard_stop():
            return stop
        feature = session.current_feature or "unknown feature"
        fb = await feedback.run(session.repo, session.objective, feature)
        session.cost_usd += fb.cost_usd
        session.mem.write("metrics", f"{session.run_id}-{slug(feature)}", fb.text)
        session.note("assess_feedback")
        return {"content": [{"type": "text", "text": fb.text[:2000]}]}

    return [
        understand_codebase,
        check_backlog,
        discover_opportunities,
        implement_feature,
        run_local_tests,
        deploy_pre_prod,
        verify_pre_prod,
        assess_feedback,
    ]


SYSTEM_PROMPT = """You are the Orchestrator Agent in an autonomous product \
engineering system (vision.md 5.1). You decide which specialized agent to \
invoke next, in what order, and when this run is complete — there is no \
fixed script to follow. You have exactly eight tools, each delegating to a \
specialized agent: understand_codebase, check_backlog, \
discover_opportunities, implement_feature, run_local_tests, deploy_pre_prod, \
verify_pre_prod, assess_feedback. You do not have Read/Write/Edit/Bash \
yourself. Production is deliberately not reachable from this session under \
any circumstance.

Use judgment, not a rigid script, but this is generally sound:
1. Understand the codebase before deciding anything.
2. Check the backlog — a known production bug always outranks a \
   nice-to-have feature.
3. If no feature was suggested to you, discover opportunities and pick one.
4. Implement it, then run_local_tests. deploy_pre_prod refuses if local \
   tests haven't passed since the last implementation — if that happens, \
   fix the underlying issue and re-test, don't just retry the deploy.
5. Once deployed, call verify_pre_prod — this checks the actual live \
   deployment, which is a different and more important question than "did \
   local tests pass." A deploy that returns 200 on the homepage but whose \
   feature doesn't actually work is a failure verify_pre_prod exists to catch.
6. Once verified live, assess feedback so impact is actually measurable later.
7. Stop once you've completed one meaningful unit of work for this run, or \
   explain plainly why you stopped short (e.g. tests kept failing, or the \
   live deployment didn't check out).

Business objective: {objective}
"""


@dataclass
class AutonomousCycleReport:
    run_id: str
    actions: list[str]
    final_message: str
    cost_usd: float


async def run_autonomous_cycle(
    repo: Path,
    objective: str,
    env: EnvironmentConfig,
    analytics_summary: str = "not available",
    feature: str | None = None,
    skip_deploy: bool = False,
    max_turns: int = 40,
) -> AutonomousCycleReport:
    repo = repo.resolve()
    mem = Memory(repo)
    run_id = uuid.uuid4().hex[:8]
    session = OrchestratorSession(
        repo=repo,
        objective=objective,
        env=env,
        mem=mem,
        run_id=run_id,
        analytics_summary=analytics_summary,
        skip_deploy=skip_deploy,
    )
    session.note(f"autonomous cycle start | objective={objective!r} feature_hint={feature!r} skip_deploy={skip_deploy}")

    tools = _tools_for(session)
    server = create_sdk_mcp_server(name="agentos_brain", tools=tools)
    allowed_tools = [f"mcp__agentos_brain__{t.name}" for t in tools]

    prompt = f"Business objective: {objective}\n"
    if feature:
        prompt += f"A feature has been suggested to prioritize: {feature}\n"
    if skip_deploy:
        prompt += "Deployment is disabled for this run — do not call deploy_pre_prod or verify_pre_prod.\n"
    prompt += "Decide what to do and carry it out."

    options = ClaudeAgentOptions(
        cwd=str(repo),
        system_prompt=SYSTEM_PROMPT.format(objective=objective),
        mcp_servers={"agentos_brain": server},
        allowed_tools=allowed_tools,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
    )

    print(f"[agentos] run {run_id} starting | objective={objective!r}", flush=True)
    final_text = ""
    try:
        async for message in query(prompt=single_prompt_stream(prompt), options=options):
            if isinstance(message, ResultMessage):
                final_text = message.result or ""
                session.cost_usd += message.total_cost_usd or 0.0
    except Exception as exc:
        # Same rationale as agents/base.py's run_agent: an SDK/CLI-subprocess-level
        # exception here has nothing to do with the actual orchestration work, and
        # should be recorded as a failed run rather than crash the whole process with
        # an unhandled traceback.
        final_text = f"autonomous cycle raised: {exc}"
        session.note(f"autonomous cycle crashed: {exc}")
        mem.write("failures", f"{run_id}-autonomous", final_text)

    mem.write(
        "decisions",
        f"{run_id}-autonomous-summary",
        f"# Autonomous cycle {run_id}\n\nObjective: {objective}\n\n"
        + "\n".join(f"- {a}" for a in session.actions)
        + f"\n\nFinal message:\n{final_text}\n",
    )
    session.note("autonomous cycle complete")
    print(f"[agentos] run {run_id} finished | total cost: ${session.cost_usd:.4f}", flush=True)

    return AutonomousCycleReport(
        run_id=run_id, actions=session.actions, final_message=final_text, cost_usd=session.cost_usd
    )

"""Orchestrator Agent (vision.md 5.1) as a real decision-maker.

orchestrator.run_cycle() hardcodes the sequence (codebase -> discovery ->
implementation -> testing -> deploy -> feedback) in plain Python. This
module is the alternative vision.md actually describes: an agent that
"decides next best action" — it sees a fixed menu of seven tools, one per
specialized agent, and chooses which to call, in what order, and when this
run is done. There is no hardcoded script here; the sequence you see in a
given run is a real decision the model made, not a lookup.

Safety boundary, unchanged from the rest of the system: this "brain" is
never given Read/Write/Edit/Bash directly, only the seven tools below.
Each tool delegates to one of the existing, narrowly-scoped agents
(agents/codebase.py, discovery.py, etc.) — that's where actual filesystem
and shell access lives, gated exactly as it was before this module existed.
Production is deliberately not one of the seven tools; promote_prod() stays
reachable only via `agentos promote` (human) or the debug-prod
auto-remediate path, never from an autonomous sequencing decision. And the
one invariant that must hold no matter what the model decides — never
deploy before tests pass — is enforced in deploy_beta's handler itself
(a real boolean check), not left to the system prompt.
"""

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query, tool

from agentos.agents import codebase, deployment, discovery, feedback, implementation, testing
from agentos.agents.base import single_prompt_stream
from agentos.environments import EnvironmentConfig
from agentos.memory import Memory
from agentos.ranking import rank


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
    current_feature: str | None = None
    cost_usd: float = 0.0
    skip_deploy: bool = False
    actions: list[str] = field(default_factory=list)

    def note(self, action: str) -> None:
        self.actions.append(action)
        self.mem.log(self.run_id, action)


def _tools_for(session: OrchestratorSession) -> list:
    @tool(
        "understand_codebase",
        "Scan the repo and produce/refresh the codebase understanding summary. "
        "Call this before discover_opportunities or implement_feature.",
        {},
    )
    async def understand_codebase(_args):
        cb = await codebase.run(session.repo)
        session.cost_usd += cb.cost_usd
        session.mem.write("architecture", "codebase", cb.text)
        if cb.ok:
            session.cb_summary = cb.text
        session.note(f"understand_codebase: ok={cb.ok}")
        return {
            "content": [{"type": "text", "text": f"[{'ok' if cb.ok else 'failed'}] {cb.text[:4000]}"}],
            "is_error": not cb.ok,
        }

    @tool(
        "check_backlog",
        "See what's already shipped (don't repeat it) and what known bugs are pending "
        "from production (a confirmed bug always outranks a nice-to-have feature).",
        {},
    )
    async def check_backlog(_args):
        shipped = session.mem.shipped_features()
        bugs = session.mem.known_bugs()
        text = (
            f"Already shipped: {shipped or '(none)'}\n\n"
            f"Known bugs awaiting a fix: {json.dumps(bugs, indent=2) if bugs else '(none)'}"
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
        if session.cb_summary is None:
            return {"content": [{"type": "text", "text": "Call understand_codebase first."}], "is_error": True}
        disc = await discovery.run(
            session.repo,
            session.objective,
            session.cb_summary,
            session.analytics_summary,
            session.mem.shipped_features(),
            session.mem.known_bugs(),
        )
        session.cost_usd += disc.cost_usd
        session.mem.write("decisions", f"{session.run_id}-discovery", disc.text)
        opportunities = rank((disc.json_data or {}).get("opportunities", []))
        session.note(f"discover_opportunities: {len(opportunities)} candidates")
        if not disc.ok or not opportunities:
            return {"content": [{"type": "text", "text": "Discovery failed to produce opportunities."}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(opportunities, indent=2)}]}

    @tool(
        "implement_feature",
        "Build a specific feature. Pass a concrete, self-contained brief, not just a name.",
        {"feature_brief": str},
    )
    async def implement_feature(args):
        if session.cb_summary is None:
            return {"content": [{"type": "text", "text": "Call understand_codebase first."}], "is_error": True}
        brief = args["feature_brief"]
        impl = await implementation.run(session.repo, session.objective, brief, session.cb_summary, session.env)
        session.cost_usd += impl.cost_usd
        feature_name = (impl.json_data or {}).get("feature") or brief.split(":")[0].strip()
        session.mem.write("features", f"{session.run_id}-{_slug(feature_name)}", impl.text)
        session.tests_passed = False  # any new change invalidates the last test result
        session.note(f"implement_feature: ok={impl.ok} feature={feature_name!r}")
        if not impl.ok:
            session.mem.write("failures", f"{session.run_id}-implementation", impl.text)
            return {"content": [{"type": "text", "text": f"Implementation failed: {impl.text[:2000]}"}], "is_error": True}
        session.mem.record_shipped(feature_name)
        session.current_feature = feature_name
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Implemented and committed {feature_name!r}. Call run_tests before deploy_beta.",
                }
            ]
        }

    @tool(
        "run_tests",
        "Independently verify the current state of the repo. Required before deploy_beta "
        "will do anything — it refuses if this hasn't passed since the last implementation.",
        {},
    )
    async def run_tests(_args):
        if session.cb_summary is None:
            return {"content": [{"type": "text", "text": "Call understand_codebase first."}], "is_error": True}
        test = await testing.run(session.repo, session.cb_summary)
        session.cost_usd += test.cost_usd
        passed = test.ok and (test.json_data or {}).get("status") != "fail"
        session.tests_passed = passed
        session.note(f"run_tests: passed={passed}")
        if not passed:
            session.mem.write("failures", f"{session.run_id}-testing", test.text)
        return {
            "content": [{"type": "text", "text": f"Tests {'PASSED' if passed else 'FAILED'}. {test.text[:2000]}"}],
            "is_error": not passed,
        }

    @tool(
        "deploy_beta",
        "Deploy the current state to the beta environment.",
        {},
    )
    async def deploy_beta(_args):
        if session.skip_deploy:
            session.note("deploy_beta: skipped (skip_deploy set for this run)")
            return {"content": [{"type": "text", "text": "Skipped: this run was started with deploy disabled."}]}
        if not session.tests_passed:
            session.note("deploy_beta: refused, tests not passed")
            return {
                "content": [{"type": "text", "text": "Refused: run_tests must pass before deploying to beta."}],
                "is_error": True,
            }
        deploy = await deployment.deploy_beta(session.repo, session.env)
        session.cost_usd += deploy.cost_usd
        ok = deploy.ok and (deploy.json_data or {}).get("status") != "failed"
        session.note(f"deploy_beta: ok={ok}")
        if not ok:
            session.mem.write("failures", f"{session.run_id}-deployment", deploy.text)
        return {"content": [{"type": "text", "text": deploy.text[:2000]}], "is_error": not ok}

    @tool(
        "assess_feedback",
        "Check whether the shipped feature is actually measurable (instrumentation) and "
        "name concrete success metrics. Call after a successful deploy_beta.",
        {},
    )
    async def assess_feedback(_args):
        feature = session.current_feature or "unknown feature"
        fb = await feedback.run(session.repo, session.objective, feature)
        session.cost_usd += fb.cost_usd
        session.mem.write("metrics", f"{session.run_id}-{_slug(feature)}", fb.text)
        session.note("assess_feedback")
        return {"content": [{"type": "text", "text": fb.text[:2000]}]}

    return [
        understand_codebase,
        check_backlog,
        discover_opportunities,
        implement_feature,
        run_tests,
        deploy_beta,
        assess_feedback,
    ]


SYSTEM_PROMPT = """You are the Orchestrator Agent in an autonomous product \
engineering system (vision.md 5.1). You decide which specialized agent to \
invoke next, in what order, and when this run is complete — there is no \
fixed script to follow. You have exactly seven tools, each delegating to a \
specialized agent: understand_codebase, check_backlog, \
discover_opportunities, implement_feature, run_tests, deploy_beta, \
assess_feedback. You do not have Read/Write/Edit/Bash yourself. Production \
is deliberately not reachable from this session under any circumstance.

Use judgment, not a rigid script, but this is generally sound:
1. Understand the codebase before deciding anything.
2. Check the backlog — a known production bug always outranks a \
   nice-to-have feature.
3. If no feature was suggested to you, discover opportunities and pick one.
4. Implement it, then run tests. deploy_beta refuses if tests haven't \
   passed since the last implementation — if that happens, fix the \
   underlying issue and re-test, don't just retry the deploy.
5. Once deployed, assess feedback so impact is actually measurable later.
6. Stop once you've completed one meaningful unit of work for this run, or \
   explain plainly why you stopped short (e.g. tests kept failing).

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
        prompt += "Deployment is disabled for this run — do not call deploy_beta.\n"
    prompt += "Decide what to do and carry it out."

    options = ClaudeAgentOptions(
        cwd=str(repo),
        system_prompt=SYSTEM_PROMPT.format(objective=objective),
        mcp_servers={"agentos_brain": server},
        allowed_tools=allowed_tools,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
    )

    final_text = ""
    async for message in query(prompt=single_prompt_stream(prompt), options=options):
        if isinstance(message, ResultMessage):
            final_text = message.result or ""
            session.cost_usd += message.total_cost_usd or 0.0

    mem.write(
        "decisions",
        f"{run_id}-autonomous-summary",
        f"# Autonomous cycle {run_id}\n\nObjective: {objective}\n\n"
        + "\n".join(f"- {a}" for a in session.actions)
        + f"\n\nFinal message:\n{final_text}\n",
    )
    session.note("autonomous cycle complete")

    return AutonomousCycleReport(
        run_id=run_id, actions=session.actions, final_message=final_text, cost_usd=session.cost_usd
    )


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:60]

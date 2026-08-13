"""Orchestrator Agent (vision.md 5.1) as a real decision-maker.

orchestrator.run_cycle() hardcodes the sequence (codebase -> discovery ->
implementation -> testing -> deploy -> feedback) in plain Python. This
module is the alternative vision.md actually describes: an agent that
"decides next best action" — it sees a fixed menu of nine tools, eight tied
to a specific specialized agent (or agent mode) plus one generic escape
hatch (spawn_custom_agent, agents/generic.py) for task types that don't
warrant a dedicated Python module — and chooses which to call, in what
order, and when this run is done. There is no hardcoded script here; the
sequence you see in a given run is a real decision the model made, not a
lookup.

Safety boundary, unchanged from the rest of the system: this "brain" is
never given Read/Write/Edit/Bash directly, only the nine tools below.
Each tool delegates to one of the existing, narrowly-scoped agents
(agents/codebase.py, discovery.py, etc., or agents/generic.py for
spawn_custom_agent) — that's where actual filesystem and shell access
lives, gated exactly as it was before this module existed. Production is
deliberately not one of the nine tools (spawn_custom_agent included —
agents/generic.py::TaskSpec has no allow_prod field at all); promote_prod()
stays reachable only via `agentra promote` (human) or the debug-prod
auto-remediate path, never from an autonomous sequencing decision. And the
one invariant that must hold no matter what the model decides — never
deploy before local tests pass — is enforced in deploy_pre_prod's handler
itself (a real boolean check), not left to the system prompt.

Testing is deliberately two tools, not one: run_local_tests verifies the
code; verify_pre_prod independently verifies the live deployment afterward
(agents/testing.py has the full reasoning on why these are distinct passes).
"""

import hashlib
import json
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query, tool

from agentra.agents import codebase, deployment, discovery, feedback, implementation, testing
from agentra.agents.base import log_claude_message, run_log_scope, single_prompt_stream
from agentra.agents.generic import TaskSpec, spawn as spawn_generic
from agentra.environments import EnvironmentConfig, feature_branch_name
from agentra.memory import Memory
from agentra.ranking import rank

# Circuit breaker thresholds. Retrying a failing tool is only useful when the failure
# is about *what* was tried (a bad brief, a flaky test) -- an infrastructure error (a
# missing directory, a broken subprocess call) looks identical to the orchestrator LLM
# turn over turn, and prose telling it "don't retry forever" isn't reliable (see
# implementation.py's module docstring on why this codebase does control flow in Python,
# not prompts). So: cap consecutive failures of the *same* tool, and cap total spend,
# both enforced deterministically rather than left to the model's judgment.
MAX_CONSECUTIVE_TOOL_FAILURES = 2
MAX_CYCLE_COST_USD = 3.0

# Stagnation breaker threshold -- deliberately distinct from MAX_CONSECUTIVE_TOOL_FAILURES
# above. That one catches a tool erroring the same way over and over; this one catches the
# opposite failure mode, which is just as capable of running the clock (and the cost meter)
# out to max_turns: the orchestrator LLM calling tools that each individually report success
# (or a *different* failure each time, so record_failure never trips) while the run itself
# never actually moves -- e.g. re-running check_backlog or discover_opportunities every turn
# without ever acting on the answer, or calling implement_feature with the exact same brief
# repeatedly. See OrchestratorSession.record_tool_call / progress_snapshot below.
STAGNATION_WINDOW = 5


def _tool_call_hash(tool_name: str, args: dict) -> str:
    """Stable hash of (tool_name, args) -- stable meaning deterministic across processes
    and Python runs (unlike the builtin hash() of a str/tuple, which is salted per
    process), so it can be logged and compared run to run, not just within one call."""
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
    # Deliberately separate from pre_prod_url (which a later failed redeploy attempt in
    # this same session resets to None) -- once true, a merge+push to pre-prod already
    # durably happened and should be persisted regardless of what happens afterward.
    deployed_to_pre_prod: bool = False
    # True once deployment.deploy_pre_prod() was actually called, regardless
    # of whether it succeeded -- distinct from deployed_to_pre_prod above.
    # HEAD is guaranteed to be on pre_prod_branch by then either way (see
    # that call site), which is what makes persisting the audit trail safe
    # even after a failed deploy. Without this, a failed cycle's entire
    # decisions/failures record stayed on the container's ephemeral disk
    # only -- confirmed live, a real failed cycle's audit trail never made
    # it past that.
    deploy_attempted: bool = False
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
    # Sliding window of the last STAGNATION_WINDOW tool calls this run has made, each
    # entry (call_hash, state_changed) -- see record_tool_call.
    recent_tool_calls: list[tuple[str, bool]] = field(default_factory=list)
    stagnation_detected: bool = False

    @property
    def app_name(self) -> str:
        return self.repo.name

    def note(
        self, action: str, *, agent: str | None = None, ok: bool | None = None, cost_usd: float = 0.0, turns: int | None = None
    ) -> None:
        """ok/cost_usd/turns describe THIS call specifically (not
        self.cost_usd, which is the running cycle total) -- every call site
        already has the AgentResult these come from right where it calls
        note(), so passing them through costs nothing and turns the
        dashboard's "what did each agent do" view from parsing this same
        plain-text action string into real structured data (registry.py's
        agent_steps, durable -- unlike this action string, which only ever
        lands in mem.log()'s gitignored, non-durable local file).

        agent defaults to the leading "tool_name:" token in `action` (every
        tool-handler call site follows that convention) -- pass it
        explicitly for the handful of cycle-lifecycle notes (start/crashed/
        complete) that don't, so a long "objective=...' " message doesn't
        become the displayed agent name on the dashboard."""
        self.actions.append(action)
        self.mem.log(self.run_id, action)
        print(f"[agentra] {action} | cost so far: ${self.cost_usd:.4f}", flush=True)
        from agentra import registry

        resolved_agent = agent or action.split(":", 1)[0].split("[", 1)[0].strip()
        registry.record_agent_step(self.app_name, self.run_id, resolved_agent, ok, cost_usd, turns, action)

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

    def progress_snapshot(self) -> tuple:
        """A cheap, comparable snapshot of the fields that represent this run actually
        having moved forward. Deliberately excludes cost_usd and len(actions) -- both
        change on nearly every call (an LLM call always costs something; note() is
        called from nearly every handler) and so would make the state-change signal
        below meaningless -- everything would look like "progress"."""
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
        """Stagnation breaker, distinct from record_failure's per-tool-name
        consecutive-*failure* breaker above. Tracks a sliding window of the last
        STAGNATION_WINDOW tool calls this run made (as (hash-of-tool-and-args,
        did-state-change) pairs); if the window fills up with the same call
        producing no observable change every single time, the run isn't going
        anywhere -- stop it, same as the failure breaker does, rather than let it
        burn turns/cost until max_turns.
        """
        if self.hard_stop_reason:
            return  # already tripped (this breaker or the other) -- nothing new to track
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
        "discover_opportunities. Priority order for what to work on next: (1) an "
        "in-progress multi-part feature -- resume it (implement_feature with "
        "sub_feature_of set to its id) before starting anything new, so it's never "
        "silently abandoned; (2) a known bug -- always outranks a nice-to-have "
        "feature; (3) the feature request queue -- customer/admin submitted, "
        "outranks your own ideation; (4) only if all three are empty, call "
        "discover_opportunities to ideate something new. Also shows what's already "
        "shipped, so you don't repeat it.",
        {},
    )
    async def check_backlog(_args):
        if stop := session.check_hard_stop():
            return stop
        shipped = [f["feature"] for f in session.mem.shipped_features()]
        in_progress = session.mem.in_progress_features()
        bugs = session.mem.known_bugs()
        queue = session.mem.feature_queue()
        text = (
            f"In-progress multi-part features (resume these first): {json.dumps(in_progress, indent=2) if in_progress else '(none)'}\n\n"
            f"Known bugs awaiting a fix: {json.dumps(bugs, indent=2) if bugs else '(none)'}\n\n"
            f"Feature request queue: {json.dumps(queue, indent=2) if queue else '(none)'}\n\n"
            f"Already shipped: {shipped or '(none)'}"
        )
        session.note("check_backlog", ok=True)
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "discover_opportunities",
        "Last resort: only call this once check_backlog shows no in-progress "
        "feature, no known bug, and an empty feature queue. Asks the Product "
        "Discovery Agent for ranked NEW feature opportunities from the codebase, "
        "analytics, and backlog. Requires understand_codebase first.",
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
            session.mem.known_bugs(),
            session.mem.feature_queue(),
        )
        session.cost_usd += disc.cost_usd
        opportunities = rank((disc.json_data or {}).get("opportunities", []))
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
        return {"content": [{"type": "text", "text": json.dumps(opportunities, indent=2)}]}

    @tool(
        "implement_feature",
        "Build a specific feature. Pass a concrete, self-contained brief, not just a name. "
        "If this brief comes from check_backlog's known bugs or feature queue (not your own "
        "idea), pass its id in resolves_id with the matching resolves_origin -- otherwise it "
        "never gets cleared and will keep resurfacing every future cycle. Leave both empty "
        "strings for your own autonomous ideas.\n"
        "If a feature is too large for one call, split it across multiple implement_feature "
        "calls: set more_parts_expected=true on every call except the last one for that "
        "feature. On the FIRST call for a multi-part feature, leave sub_feature_of empty -- "
        "a parent tracking issue is created (or, if resolves_id was set, that feature_queue "
        "issue becomes the parent) and its id is given back to you in the result as 'issue "
        "#N'. On every call AFTER the first for the same feature, pass that id in "
        "sub_feature_of and leave resolves_id empty. The parent stays open, discoverable by "
        "check_backlog, until the call where more_parts_expected=false -- that's what marks "
        "the whole feature done, so always set it false on the final part.\n"
        "Leave sub_feature_of empty and more_parts_expected false (the defaults) for a "
        "single-call, self-contained feature -- the common case.",
        {
            "feature_brief": str,
            "resolves_origin": str,
            "resolves_id": str,
            "sub_feature_of": str,
            "more_parts_expected": bool,
        },
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
            session.note(f"implement_feature: raised {exc!r}", ok=False)
            return {"content": [{"type": "text", "text": f"implement_feature raised: {exc}"}], "is_error": True}
        session.cost_usd += impl.cost_usd
        feature_name = (impl.json_data or {}).get("feature") or brief.split(":")[0].strip()
        session.tests_passed = False  # any new change invalidates the last test result
        session.pre_prod_verified = False  # ...and any prior live verification too
        session.note(
            f"implement_feature: ok={impl.ok} feature={feature_name!r}",
            ok=impl.ok,
            cost_usd=impl.cost_usd,
            turns=impl.turns,
        )
        if not impl.ok:
            session.mem.record_failure(session.run_id, "implementation", impl.text)
            session.record_failure("implement_feature")
            return {"content": [{"type": "text", "text": f"Implementation failed: {impl.text[:2000]}"}], "is_error": True}
        session.record_success("implement_feature")
        commit_sha = None
        try:
            head = subprocess.run(
                ["git", "-C", str(session.repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
            )
            if head.returncode == 0:
                commit_sha = head.stdout.strip()
        except Exception:
            pass  # dashboard's shipped list just shows no artifact link -- not worth failing the cycle over
        resolves_origin = args.get("resolves_origin") or ""
        resolves_id = args.get("resolves_id") or ""
        sub_feature_of = args.get("sub_feature_of") or ""
        more_parts_expected = bool(args.get("more_parts_expected"))
        # record_shipped closes a GitHub 'enhancement' issue as the shipped record,
        # stamping run_id/commit_sha into it -- the originating feature_queue issue
        # itself when this resolves one, a linked sub-issue of a multi-part
        # feature's (left-open-until-done) parent, or a fresh issue created and
        # closed immediately for a single-call feature. See its own docstring.
        shipped = session.mem.record_shipped(
            feature_name,
            commit_sha=commit_sha,
            run_id=session.run_id,
            resolves_id=resolves_id if resolves_origin == "feature_queue" else None,
            sub_feature_of=sub_feature_of or None,
            more_parts_expected=more_parts_expected,
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
        # more_parts_expected is the model's own stated intent for THIS call --
        # trust it directly rather than re-deriving "is there more to do" from
        # the args, so the hint always matches what the model actually asked for.
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
        test = await testing.run_local(session.repo, session.cb_summary, session.mem)
        session.cost_usd += test.cost_usd
        data = test.json_data or {}
        passed = test.ok and data.get("status") != "fail"
        session.tests_passed = passed
        # The structured result (lint/typecheck status, which tests failed) used to only
        # get durably recorded on failure (mem.record_failure(...) below) -- on a pass
        # it vanished once the process ended, so the dashboard's "testing artifacts" view
        # had nothing to show for a clean run. note()'s message is already durable
        # (agent_steps.summary + this run's own actions list), so folding the detail in
        # here costs nothing new to store.
        detail = f"lint={data.get('lint_status', '?')} typecheck={data.get('typecheck_status', '?')}"
        if data.get("failed_tests"):
            detail += f" failed={data['failed_tests']}"
        session.note(f"run_local_tests: passed={passed} | {detail}", ok=passed, cost_usd=test.cost_usd, turns=test.turns)
        if not passed:
            session.mem.record_failure(session.run_id, "testing", test.text)
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
            session.note("deploy_pre_prod: skipped (skip_deploy set for this run)", ok=None)
            return {"content": [{"type": "text", "text": "Skipped: this run was started with deploy disabled."}]}
        if not session.tests_passed:
            session.note("deploy_pre_prod: refused, tests not passed", ok=False)
            return {
                "content": [{"type": "text", "text": "Refused: run_local_tests must pass before deploying to pre-prod."}],
                "is_error": True,
            }
        if session.feature_branch is None:
            session.note("deploy_pre_prod: refused, no feature branch (implement_feature never called)", ok=False)
            return {
                "content": [{"type": "text", "text": "Refused: nothing to deploy -- call implement_feature first."}],
                "is_error": True,
            }
        deploy = await deployment.deploy_pre_prod(session.repo, session.env, session.feature_branch)
        # deployment.deploy_pre_prod's first action is always
        # _sync_branch_to_remote(repo, pre_prod_branch) -- by the time this
        # call returns, success or failure, HEAD is guaranteed to be on
        # pre_prod_branch (a merge failure aborts back onto it, never
        # leaves HEAD on the feature branch). That's what makes it safe to
        # persist the audit trail below regardless of deploy outcome.
        session.deploy_attempted = True
        session.cost_usd += deploy.cost_usd
        ok = deploy.ok and (deploy.json_data or {}).get("status") != "failed"
        session.pre_prod_url = (deploy.json_data or {}).get("preview_url") if ok else None
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
        test = await testing.run_pre_prod(session.repo, session.cb_summary or "", session.pre_prod_url, session.run_id)
        session.cost_usd += test.cost_usd
        data = test.json_data or {}
        passed = test.ok and data.get("status") != "fail"
        session.pre_prod_verified = passed
        detail = f"reachable={data.get('reachable', '?')} feature_verified={data.get('feature_verified', '?')}"
        session.note(f"verify_pre_prod: passed={passed} | {detail}", ok=passed, cost_usd=test.cost_usd, turns=test.turns)
        if not passed:
            session.mem.record_failure(session.run_id, "pre-prod-verification", test.text)
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
        session.note("assess_feedback", ok=fb.ok, cost_usd=fb.cost_usd, turns=fb.turns)
        return {"content": [{"type": "text", "text": fb.text[:2000]}]}

    _SPAWNABLE_TOOLS = {"Read", "Write", "Edit", "Glob", "Grep", "Bash"}

    @tool(
        "spawn_custom_agent",
        "Spawn a one-off sub-agent for a task that doesn't fit any tool above -- e.g. a "
        "one-time audit, a research question, a cleanup pass that isn't 'implement a "
        "feature'. Give it a short task_name (for logs), a complete prompt, a system_prompt "
        "describing its role and constraints, and allowed_tools as a comma-separated list "
        "chosen from Read, Write, Edit, Glob, Grep, Bash -- grant only what the task needs. "
        "This never has production access no matter what tools you grant.",
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

    tools = [
        understand_codebase,
        check_backlog,
        discover_opportunities,
        implement_feature,
        run_local_tests,
        deploy_pre_prod,
        verify_pre_prod,
        assess_feedback,
        spawn_custom_agent,
    ]
    # Feed every tool call into the stagnation breaker uniformly, rather than
    # threading a before/after snapshot + record_tool_call call through each of the
    # nine handlers above individually (easy to forget on the next tool added here).
    for t in tools:
        t.handler = _stagnation_tracked(session, t.name, t.handler)
    return tools


def _stagnation_tracked(session: "OrchestratorSession", tool_name: str, handler):
    """Wrap a tool handler so the stagnation breaker (OrchestratorSession.
    record_tool_call) observes every call this session makes, regardless of which
    tool -- the state-change signal is a before/after diff of session.progress_snapshot()
    taken around the handler's own work."""

    async def wrapped(args):
        before = session.progress_snapshot()
        result = await handler(args)
        after = session.progress_snapshot()
        session.record_tool_call(tool_name, args, state_changed=before != after)
        return result

    return wrapped


SYSTEM_PROMPT = """You are the Orchestrator Agent in an autonomous product \
engineering system (vision.md 5.1). You decide which specialized agent to \
invoke next, in what order, and when this run is complete — there is no \
fixed script to follow. You have exactly nine tools, eight delegating to a \
specialized agent: understand_codebase, check_backlog, \
discover_opportunities, implement_feature, run_local_tests, deploy_pre_prod, \
verify_pre_prod, assess_feedback — plus spawn_custom_agent, a generic \
sub-agent for a one-off task that doesn't fit any of the other eight (an \
audit, a research question, a cleanup pass that isn't "implement a \
feature"). You do not have Read/Write/Edit/Bash yourself; spawn_custom_agent \
grants only what you explicitly ask for, to that one sub-agent, for that \
one task. Production is deliberately not reachable from this session under \
any circumstance, including via spawn_custom_agent.

Use judgment, not a rigid script, but this is generally sound:
1. Understand the codebase before deciding anything.
2. Check the backlog — this alone tells you what to work on next, in \
   priority order: (a) an in-progress multi-part feature — resume it \
   (implement_feature with sub_feature_of set to its id) before starting \
   anything new, so it's never silently abandoned mid-way; (b) a known \
   production bug — always outranks a nice-to-have feature; (c) the \
   feature request queue — customer/admin submitted, outranks your own \
   ideation. If you implement something straight from check_backlog's \
   output, pass its id/run_id through implement_feature's \
   resolves_origin+resolves_id so it gets cleared — otherwise it resurfaces \
   every future cycle even after you fix it.
3. Only if check_backlog shows none of the above (or a feature was \
   explicitly suggested to you), discover opportunities and pick one.
4. Implement it, then run_local_tests. deploy_pre_prod refuses if local \
   tests haven't passed since the last implementation — if that happens, \
   fix the underlying issue and re-test, don't just retry the deploy.
5. Once deployed, call verify_pre_prod — this checks the actual live \
   deployment, which is a different and more important question than "did \
   local tests pass." A deploy that returns 200 on the homepage but whose \
   feature doesn't actually work is a failure verify_pre_prod exists to catch.
6. Once verified live, assess feedback so impact is actually measurable later.
7. Reach for spawn_custom_agent only for work that genuinely isn't one of \
   the other eight steps — don't use it to reimplement implement_feature or \
   deploy_pre_prod with a custom prompt.
8. Stop once you've completed one meaningful unit of work for this run, or \
   explain plainly why you stopped short (e.g. tests kept failing, or the \
   live deployment didn't check out).

In your final summary, never claim a benefit is already realized if it's \
actually gated on something that hasn't happened yet (a dormant/inactive \
pipeline, an unconfigured service, a pending human action) — say plainly \
what's still blocking it instead. Confirmed live: a cycle's own summary said \
"CI will now fail loudly on future regressions" about a workflow file that \
was never wired into GitHub Actions at all — that's not what happened, and \
saying so misleads whoever reads this run later.

Business objective: {objective}
"""


@dataclass
class AutonomousCycleReport:
    run_id: str
    actions: list[str]
    final_message: str
    cost_usd: float
    # session.current_feature, if implement_feature was ever called this
    # cycle -- None for cycles that never got past discovery/check_backlog
    # (e.g. nothing due, or the whole cycle was read-only investigation).
    # The dashboard shows this instead of the app's static objective, which
    # is the same multi-sentence mission statement for every run of an app
    # and says nothing about what THIS run actually did.
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
    """run_id: pass the caller's own id to use as this cycle's id instead of
    minting a fresh one -- server.py passes its run_key here, so the id
    used to poll/track a run (GET /runs/{run_key}) is the exact same id
    agent_steps.run_id and Memory's per-run log file use, instead of two
    unrelated uuids for what's really one run. CLI callers pass nothing and
    get a fresh id as before."""
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
    session.note(
        f"autonomous cycle start | objective={objective!r} feature_hint={feature!r} skip_deploy={skip_deploy}",
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
    prompt += "Decide what to do and carry it out."

    options = ClaudeAgentOptions(
        cwd=str(repo),
        system_prompt=SYSTEM_PROMPT.format(objective=objective),
        mcp_servers={"agentra_brain": server},
        allowed_tools=allowed_tools,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
    )

    print(f"[agentra] run {run_id} starting | objective={objective!r}", flush=True)
    final_text = ""
    try:
        with run_log_scope(lambda line: mem.log(run_id, line)):
            async for message in query(prompt=single_prompt_stream(prompt), options=options):
                # Explicit logger (not log_claude_message's own _RUN_LOGGER fallback) so
                # this top-level orchestrator turn's own lines get the same "[Label] ..."
                # prefix agents/base.py::run_agent gives every nested specialized-agent
                # call -- otherwise these lines would be the one unlabeled gap in an
                # otherwise fully agent-tagged live log.
                log_claude_message(message, lambda line: mem.log(run_id, f"[Orchestrator] {line}"))
                if isinstance(message, ResultMessage):
                    final_text = message.result or ""
                    session.cost_usd += message.total_cost_usd or 0.0
    except Exception as exc:
        # Same rationale as agents/base.py's run_agent: an SDK/CLI-subprocess-level
        # exception here has nothing to do with the actual orchestration work, and
        # should be recorded as a failed run rather than crash the whole process with
        # an unhandled traceback.
        final_text = f"autonomous cycle raised: {exc}"
        session.note(f"autonomous cycle crashed: {exc}", agent="cycle", ok=False)
        mem.record_failure(run_id, "autonomous-cycle", final_text)

    if session.stagnation_detected:
        # Don't rely solely on the model having actually followed the "summarize and end
        # your turn" instruction in hard_stop_reason -- make the reason this cycle ended
        # early explicit in the durable record regardless of what final_text says.
        stagnation_note = (
            f"Cycle terminated early: no progress detected. {session.hard_stop_reason}"
        )
        final_text = f"{final_text}\n\n{stagnation_note}" if final_text else stagnation_note
        session.note("stagnation breaker: cycle terminated early, no progress", agent="cycle", ok=False)

    # The run's own log already captures this cycle blow-by-blow (mem.log()
    # calls throughout, Firestore-mirrored), and standup.py's
    # generate_standup_updates() reads recent_log_lines() as its "yesterday"
    # input -- so an investigation-only cycle that shipped nothing still
    # leaves a real trace with no separate ledger needed here.

    # Persist unconditionally (not just when a deploy happened): commit_and_push
    # is a no-op when nothing under .agentra/ is dirty, so this is always safe,
    # and it's the only way a non-deploying cycle's decisions/failures/work
    # updates above survive past this container instance. See
    # deployment.persist_audit_trail's docstring for why this exists.
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

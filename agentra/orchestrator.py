"""Orchestrator Agent (vision.md 5.1).

Two entry points:

run_cycle() — the fixed-pipeline loop (CLI: `agentra run --fixed-pipeline`):
understand codebase -> discover (or accept) a feature -> implement -> test ->
deploy to PRE-PROD -> assess measurability -> repeat, in that hardcoded
order. Not the default anymore — agents/brain.py::run_autonomous_cycle() is,
letting the Orchestrator Agent itself decide which specialized agent to call
and when, rather than following this fixed script. This function still
exists for a deterministic, always-the-same-order path when that's what's
wanted. Never touches production either way. If no feature is given, the
Product Discovery Agent (5.3) picks one, prioritizing any known bugs filed
by the Production Debugging Agent above ordinary feature work.

run_prod_debug_cycle() — on-demand: diagnose a production issue. If the
app's EnvironmentConfig.auto_remediate_prod is off (the default), it stops
at diagnosis and files a known bug for the next run_cycle() to pick up. If
it's on, it builds the fix, proves it in pre-prod, and only then promotes to
prod — the one path in this system allowed to touch production without a
human in the loop, and only because the app explicitly opted in.
"""

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agentra.agents import codebase, deployment, discovery, feedback, implementation, prod_debug, testing
from agentra.environments import EnvironmentConfig, feature_branch_name, slug
from agentra import environments, registry
from agentra.memory import Memory
from agentra.ranking import rank


@dataclass
class CycleReport:
    run_id: str
    feature: str
    codebase_ok: bool
    implementation_ok: bool
    testing_ok: bool
    deployment_ok: bool | None
    opportunities_considered: list[dict] = field(default_factory=list)
    summary: str = ""
    pre_prod_verified: bool | None = None


@dataclass
class ProdDebugReport:
    run_id: str
    root_cause_found: bool
    severity: str | None
    fix_attempted: bool
    promoted_to_prod: bool
    diagnosis_text: str


def _load_env(repo: Path, mem: Memory, run_id: str) -> EnvironmentConfig:
    env = environments.load(repo)
    if env is None:
        mem.log(run_id, "no .agentra/environments.yaml found; using defaults (run `agentra env init` to configure)")
        env = EnvironmentConfig()
    return env


async def run_cycle(
    repo: Path,
    objective: str,
    feature: str | None = None,
    analytics_summary: str = "not available",
    skip_deploy: bool = False,
) -> CycleReport:
    repo = repo.resolve()
    mem = Memory(repo)
    run_id = uuid.uuid4().hex[:8]
    mem.log(run_id, f"cycle start | objective={objective!r} feature={feature!r}")
    env = _load_env(repo, mem, run_id)

    mem.log(run_id, "codebase agent: starting")
    cb = await codebase.run(repo)
    mem.write("architecture", "codebase", cb.text)
    mem.log(run_id, f"codebase agent: ok={cb.ok} turns={cb.turns} cost=${cb.cost_usd:.4f}")
    if not cb.ok:
        return CycleReport(run_id, feature or "", False, False, False, None, [], "codebase understanding failed; aborting cycle")

    opportunities: list[dict] = []
    feature_brief = feature or ""
    top: dict | None = None
    if feature is None:
        mem.log(run_id, "discovery agent: starting")
        disc = await discovery.run(
            repo, objective, cb.text, analytics_summary, mem.shipped_features(), mem.known_bugs(), mem.feature_queue()
        )
        mem.write("decisions", f"{run_id}-discovery", disc.text)
        mem.log(run_id, f"discovery agent: ok={disc.ok} turns={disc.turns} cost=${disc.cost_usd:.4f}")
        opportunities = rank((disc.json_data or {}).get("opportunities", []))
        if not disc.ok or not opportunities:
            mem.write("failures", f"{run_id}-discovery", disc.text)
            return CycleReport(run_id, "", True, False, False, None, [], "discovery failed to produce any feature opportunity; aborting cycle")
        top = opportunities[0]
        feature = top["feature"]
        feature_brief = f"{top['feature']}: {top.get('description', '')} (reason: {top.get('reason', '')})"
        mem.log(run_id, f"discovery agent: selected {feature!r} from {len(opportunities)} candidates")

    feature_branch = feature_branch_name(env, run_id, feature)
    mem.log(run_id, f"implementation agent: starting on dedicated branch {feature_branch!r}")
    impl = await implementation.run(repo, objective, feature_brief, cb.text, env, feature_branch)
    mem.write("features", f"{run_id}-{slug(feature)}", impl.text)
    mem.log(run_id, f"implementation agent: ok={impl.ok} turns={impl.turns} cost=${impl.cost_usd:.4f}")
    if not impl.ok:
        mem.write("failures", f"{run_id}-implementation", impl.text)
        return CycleReport(run_id, feature, True, False, False, None, opportunities, "implementation failed; aborting cycle")
    mem.record_shipped(feature)
    # Clear the originating backlog entry so it doesn't keep resurfacing every cycle --
    # only known_bug/feature_queue-origin opportunities carry an id (see discovery.py).
    if top and top.get("id"):
        if top.get("origin") == "known_bug":
            mem.clear_known_bug(top["id"])
        elif top.get("origin") == "feature_queue":
            mem.clear_feature_request(top["id"])

    mem.log(run_id, "testing agent: starting (local)")
    test = await testing.run_local(repo, cb.text)
    mem.log(run_id, f"testing agent (local): ok={test.ok} turns={test.turns} cost=${test.cost_usd:.4f}")
    test_passed = test.ok and (test.json_data or {}).get("status") != "fail"
    if not test_passed:
        mem.write("failures", f"{run_id}-testing", test.text)

    deploy_ok = None
    pre_prod_verified = None
    if not skip_deploy and test_passed:
        mem.log(run_id, "deployment agent: deploying to pre-prod")
        deploy = await deployment.deploy_pre_prod(repo, env, feature_branch)
        mem.log(run_id, f"deployment agent: ok={deploy.ok} turns={deploy.turns} cost=${deploy.cost_usd:.4f}")
        deploy_ok = deploy.ok and (deploy.json_data or {}).get("status") != "failed"
        if not deploy_ok:
            mem.write("failures", f"{run_id}-deployment", deploy.text)
        else:
            preview_url = (deploy.json_data or {}).get("preview_url")
            if preview_url:
                mem.log(run_id, "testing agent: starting (live pre-prod verification)")
                pre_prod_test = await testing.run_pre_prod(repo, cb.text, preview_url)
                mem.log(
                    run_id,
                    f"testing agent (pre-prod): ok={pre_prod_test.ok} turns={pre_prod_test.turns} "
                    f"cost=${pre_prod_test.cost_usd:.4f}",
                )
                pre_prod_verified = pre_prod_test.ok and (pre_prod_test.json_data or {}).get("status") != "fail"
                if not pre_prod_verified:
                    mem.write("failures", f"{run_id}-pre-prod-verification", pre_prod_test.text)
            else:
                mem.log(run_id, "deployment reported no preview_url; skipping live verification")

    # If nothing was deployed this cycle, local passing tests are all there is to go on.
    # If something was deployed, feedback about measurable impact only makes sense once
    # the live deployment has actually been verified — not just "the deploy step didn't error".
    feedback_ready = test_passed if skip_deploy else bool(pre_prod_verified)
    if feedback_ready:
        mem.log(run_id, "feedback agent: starting")
        fb = await feedback.run(repo, objective, feature)
        mem.write("metrics", f"{run_id}-{slug(feature)}", fb.text)
        mem.log(run_id, f"feedback agent: ok={fb.ok} turns={fb.turns} cost=${fb.cost_usd:.4f}")

    mem.write(
        "decisions",
        f"{run_id}-summary",
        f"# Cycle {run_id}\n\nObjective: {objective}\nFeature: {feature}\n\n"
        f"- codebase: ok\n- implementation: {impl.ok}\n- testing (local): {test_passed}\n"
        f"- deployment (pre-prod): {deploy_ok}\n- verified live in pre-prod: {pre_prod_verified}\n"
        f"\nProduction promotion is a separate, human-gated step: `agentra promote --repo {repo}`.\n",
    )

    if deploy_ok:
        # Only meaningful once merged onto a durable, shared branch (pre-prod) -- a feature
        # branch that was never deployed is about to be abandoned regardless. Must come after
        # every mem.write above so feedback/decisions ride along too, not just shipped.json.
        persist_error = deployment.persist_audit_trail(repo, env.pre_prod_branch)
        if persist_error:
            mem.log(run_id, f"persist_audit_trail: failed: {persist_error}")

    mem.log(run_id, "cycle complete")

    return CycleReport(
        run_id=run_id,
        feature=feature,
        codebase_ok=True,
        implementation_ok=impl.ok,
        testing_ok=test_passed,
        deployment_ok=deploy_ok,
        opportunities_considered=opportunities,
        summary=(
            f"feature={feature!r} implementation_ok={impl.ok} testing_ok={test_passed} "
            f"pre_prod_deployed={deploy_ok} pre_prod_verified={pre_prod_verified}"
        ),
        pre_prod_verified=pre_prod_verified,
    )


async def run_promote(repo: Path) -> dict:
    """Human-triggered prod promotion: `agentra promote`. Always allow_prod=True
    here because a human explicitly invoked it — this is the approval gate."""
    repo = repo.resolve()
    mem = Memory(repo)
    run_id = uuid.uuid4().hex[:8]
    env = _load_env(repo, mem, run_id)
    mem.log(run_id, "promote: human-approved promotion to production starting")
    promote = await deployment.promote_prod(repo, env)
    ok = promote.ok and (promote.json_data or {}).get("status") != "failed"
    mem.log(run_id, f"promote: ok={ok}")
    if not ok:
        mem.write("failures", f"{run_id}-prod-promote", promote.text)
    return {"run_id": run_id, "ok": ok, "text": promote.text}


async def run_prod_debug_cycle(
    repo: Path,
    objective: str,
    symptom: str | None = None,
    run_id: str | None = None,
) -> ProdDebugReport:
    """run_id: see run_autonomous_cycle's docstring -- same reasoning,
    lets server.py's run_key double as this cycle's id."""
    repo = repo.resolve()
    mem = Memory(repo)
    run_id = run_id or uuid.uuid4().hex[:8]
    mem.log(run_id, f"prod-debug start | symptom={symptom!r}")
    env = _load_env(repo, mem, run_id)

    mem.log(run_id, "prod-debug agent: diagnosing")
    diag = await prod_debug.diagnose(repo, env, symptom)
    mem.write("failures", f"{run_id}-prod-issue", diag.text)
    mem.log(run_id, f"prod-debug agent: ok={diag.ok} turns={diag.turns} cost=${diag.cost_usd:.4f}")
    registry.record_agent_step(
        repo.name, run_id, "prod_debug", diag.ok, diag.cost_usd, diag.turns, f"diagnosing: ok={diag.ok}"
    )

    data = diag.json_data or {}
    if not diag.ok or not data.get("root_cause_found"):
        mem.log(run_id, "prod-debug: no confident root cause found; stopping")
        return ProdDebugReport(run_id, False, None, False, False, diag.text)

    severity = data.get("severity", "unknown")
    proposed_fix = data.get("proposed_fix", "")

    if not env.auto_remediate_prod:
        mem.record_known_bug(run_id, severity, data.get("diagnosis", ""), proposed_fix)
        mem.log(run_id, "prod-debug: auto_remediate_prod is off; filed as known bug for next discovery cycle")
        return ProdDebugReport(run_id, True, severity, False, False, diag.text)

    mem.log(run_id, "prod-debug: auto_remediate_prod is on; building fix")
    cb = await codebase.run(repo)
    registry.record_agent_step(repo.name, run_id, "understand_codebase", cb.ok, cb.cost_usd, cb.turns, "understand_codebase: ok=%s" % cb.ok)
    feature_branch = feature_branch_name(env, run_id, f"hotfix-{severity}")
    impl = await implementation.run(repo, objective, f"Hotfix: {proposed_fix}", cb.text, env, feature_branch)
    mem.write("features", f"{run_id}-hotfix", impl.text)
    mem.log(run_id, f"prod-debug: implementation ok={impl.ok}")
    registry.record_agent_step(
        repo.name, run_id, "implement_feature", impl.ok, impl.cost_usd, impl.turns, f"hotfix implementation: ok={impl.ok}"
    )
    if not impl.ok:
        mem.write("failures", f"{run_id}-hotfix-implementation", impl.text)
        return ProdDebugReport(run_id, True, severity, False, False, diag.text)

    mem.log(run_id, "prod-debug: deploying hotfix to pre-prod for verification")
    deploy = await deployment.deploy_pre_prod(repo, env, feature_branch)
    pre_prod_ok = deploy.ok and (deploy.json_data or {}).get("status") != "failed"
    registry.record_agent_step(
        repo.name, run_id, "deploy_pre_prod", pre_prod_ok, deploy.cost_usd, deploy.turns, f"hotfix deploy: ok={pre_prod_ok}"
    )
    if not pre_prod_ok:
        mem.write("failures", f"{run_id}-hotfix-pre-prod-deploy", deploy.text)
        mem.log(run_id, "prod-debug: pre-prod deploy failed; NOT promoting to prod")
        return ProdDebugReport(run_id, True, severity, True, False, diag.text)

    preview_url = (deploy.json_data or {}).get("preview_url")
    if not preview_url:
        mem.write(
            "failures",
            f"{run_id}-hotfix-pre-prod-verify",
            "Deploy reported success but returned no preview_url — cannot verify the live "
            "deployment before promoting to prod, so refusing to promote.",
        )
        mem.log(run_id, "prod-debug: no preview_url returned; NOT promoting to prod")
        return ProdDebugReport(run_id, True, severity, True, False, diag.text)

    # Must verify the actual live deployment here, not re-run local tests — the whole point
    # of this gate is proving the hotfix works once deployed, before it's ever allowed near prod.
    test = await testing.run_pre_prod(repo, cb.text, preview_url)
    test_passed = test.ok and (test.json_data or {}).get("status") != "fail"
    mem.log(run_id, f"prod-debug: live pre-prod verification passed={test_passed}")
    registry.record_agent_step(
        repo.name, run_id, "verify_pre_prod", test_passed, test.cost_usd, test.turns, f"hotfix live verification: passed={test_passed}"
    )
    if not test_passed:
        mem.write("failures", f"{run_id}-hotfix-testing", test.text)
        mem.log(run_id, "prod-debug: hotfix failed live pre-prod verification; NOT promoting to prod")
        return ProdDebugReport(run_id, True, severity, True, False, diag.text)

    mem.log(run_id, "prod-debug: hotfix verified live in pre-prod; auto-promoting to prod (auto_remediate_prod=true)")
    promote = await deployment.promote_prod(repo, env)
    promoted_ok = promote.ok and (promote.json_data or {}).get("status") != "failed"
    mem.log(run_id, f"prod-debug: promotion ok={promoted_ok}")
    registry.record_agent_step(
        repo.name, run_id, "promote_prod", promoted_ok, promote.cost_usd, promote.turns, f"prod promotion: ok={promoted_ok}"
    )
    if not promoted_ok:
        mem.write("failures", f"{run_id}-hotfix-prod-promote", promote.text)

    mem.write(
        "decisions",
        f"{run_id}-hotfix-summary",
        f"# Prod hotfix {run_id}\n\nSeverity: {severity}\nDiagnosis: {data.get('diagnosis', '')}\n"
        f"Fix: {proposed_fix}\n\n- pre-prod deploy: {pre_prod_ok}\n- verified live in pre-prod: {test_passed}\n"
        f"- promoted to prod: {promoted_ok}\n",
    )

    return ProdDebugReport(run_id, True, severity, True, promoted_ok, diag.text)

"""Orchestrator Agent."""

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agentra.agents import codebase, deployment, discovery, feedback, implementation, prod_debug, testing
from agentra.agents.base import run_log_scope
from agentra.environments import EnvironmentConfig, feature_branch_name
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
    with run_log_scope(lambda line: mem.log(run_id, line)):
        mem.log(run_id, f"cycle start | objective={objective!r} feature={feature!r}")
        env = _load_env(repo, mem, run_id)

        mem.log(run_id, "codebase agent: starting")
        cb = await codebase.run_cached(repo, mem)
        mem.log(run_id, f"codebase agent: ok={cb.ok} turns={cb.turns} cost=${cb.cost_usd:.4f}")
        if not cb.ok:
            # GitHub issue #42: this is the earliest agent-subprocess call in the
            mem.record_failure(run_id, "understand_codebase", cb.text)
            return CycleReport(run_id, feature or "", False, False, False, None, [], "codebase understanding failed; aborting cycle")

        # GitHub issue #42 hardening: this pipeline has no blocking_bugs()
        cleared = mem.clear_resolved_auth_bugs(run_id)
        if cleared:
            mem.log(run_id, f"auto-cleared previously-blocking Claude Code auth-failure bug(s): {', '.join('#' + c for c in cleared)}")

        # GitHub issue #60 hardening: close out bugs matching an already-handled
        # transient/quota condition (e.g. Claude Code CLI weekly/usage-limit
        # errors) that were filed before that detection existed.
        cleared_transient = mem.clear_resolved_transient_bugs(run_id)
        if cleared_transient:
            mem.log(
                run_id,
                f"auto-closed known bug(s) matching an already-handled transient/quota condition: "
                f"{', '.join('#' + c for c in cleared_transient)}",
            )

        opportunities: list[dict] = []
        feature_brief = feature or ""
        top: dict | None = None
        if feature is None:
            mem.log(run_id, "discovery agent: starting")
            disc = await discovery.run(
                repo,
                objective,
                cb.text,
                analytics_summary,
                [f["feature"] for f in mem.shipped_features()],
                mem.known_bugs(),
                mem.feature_queue(),
            )
            mem.log(run_id, f"discovery agent: ok={disc.ok} turns={disc.turns} cost=${disc.cost_usd:.4f}")
            opportunities = rank((disc.json_data or {}).get("opportunities", []))
            if not disc.ok or not opportunities:
                mem.record_failure(run_id, "discovery", disc.text)
                return CycleReport(run_id, "", True, False, False, None, [], "discovery failed to produce any feature opportunity; aborting cycle")
            top = opportunities[0]
            feature = top["feature"]
            feature_brief = f"{top['feature']}: {top.get('description', '')} (reason: {top.get('reason', '')})"
            mem.log(run_id, f"discovery agent: selected {feature!r} from {len(opportunities)} candidates")

        # session_id: threads one Claude session across implementation -> testing ->
        session_id: str | None = None

        feature_branch = feature_branch_name(env, run_id, feature)
        mem.log(run_id, f"implementation agent: starting on dedicated branch {feature_branch!r}")
        impl = await implementation.run(repo, objective, feature_brief, cb.text, env, feature_branch, session_id=session_id)
        session_id = impl.session_id or session_id
        mem.log(run_id, f"implementation agent: ok={impl.ok} turns={impl.turns} cost=${impl.cost_usd:.4f}")
        if not impl.ok:
            mem.record_failure(run_id, "implementation", impl.text)
            return CycleReport(run_id, feature, True, False, False, None, opportunities, "implementation failed; aborting cycle")
        # record_code_complete stamps status:code_complete on the GitHub 'feature'-labeled issue --
        resolves_id = top["id"] if top and top.get("origin") == "feature_queue" else None
        mem.record_code_complete(feature, run_id=run_id, resolves_id=resolves_id, session_id=session_id)
        mem.append_documentation(f"Code complete: **{feature}**: {feature_brief[:300]}")
        if top and top.get("id") and top.get("origin") == "known_bug":
            resolution_note = f"Resolved by agentra: code complete as {feature!r} (run {run_id})"
            mem.clear_known_bug(top["id"], resolution_note)

        mem.log(run_id, "testing agent: starting (local)")
        test = await testing.run_local(repo, cb.text, mem, session_id=session_id)
        session_id = test.session_id or session_id
        mem.log(run_id, f"testing agent (local): ok={test.ok} turns={test.turns} cost=${test.cost_usd:.4f}")
        test_passed = test.ok and (test.json_data or {}).get("status") != "fail"
        if not test_passed:
            mem.record_failure(run_id, "testing", test.text)

        deploy_ok = None
        pre_prod_verified = None
        if not skip_deploy and test_passed:
            mem.log(run_id, "deployment agent: deploying to pre-prod")
            pre_prod_strategy = deployment.PRE_PROD_STRATEGIES[env.deploy_strategy]
            deploy = await pre_prod_strategy(repo, env, feature_branch, run_id, session_id)
            session_id = deploy.session_id or session_id
            mem.log(run_id, f"deployment agent: ok={deploy.ok} turns={deploy.turns} cost=${deploy.cost_usd:.4f}")
            deploy_ok = deploy.ok and (deploy.json_data or {}).get("status") != "failed"
            if not deploy_ok:
                mem.record_failure(run_id, "deployment", deploy.text)
            else:
                preview_url = (deploy.json_data or {}).get("preview_url")
                if preview_url:
                    mem.log(run_id, "testing agent: starting (live pre-prod verification)")
                    pre_prod_test = await testing.run_pre_prod(repo, cb.text, preview_url, run_id, session_id=session_id)
                    session_id = pre_prod_test.session_id or session_id
                    mem.log(
                        run_id,
                        f"testing agent (pre-prod): ok={pre_prod_test.ok} turns={pre_prod_test.turns} "
                        f"cost=${pre_prod_test.cost_usd:.4f}",
                    )
                    pre_prod_verified = pre_prod_test.ok and (pre_prod_test.json_data or {}).get("status") != "fail"
                    if not pre_prod_verified:
                        mem.record_failure(run_id, "pre-prod-verification", pre_prod_test.text)
                else:
                    mem.log(run_id, "deployment reported no preview_url; skipping live verification")

        # If nothing was deployed this cycle, local passing tests are all there is to go on.
        feedback_ready = test_passed if skip_deploy else bool(pre_prod_verified)
        if feedback_ready:
            mem.log(run_id, "feedback agent: starting")
            fb = await feedback.run(repo, objective, feature, session_id=session_id)
            session_id = fb.session_id or session_id
            mem.log(run_id, f"feedback agent: ok={fb.ok} turns={fb.turns} cost=${fb.cost_usd:.4f}")

        if deploy_ok:
            # Only meaningful once merged onto a durable, shared branch (pre-prod) -- a feature
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


async def run_promote(repo: Path, run_id: str | None = None) -> dict:
    """Human-triggered prod promotion: `agentra promote`, or the dashboard's Promote button (server.py's /apps/{name}/promote)."""
    repo = repo.resolve()
    mem = Memory(repo)
    run_id = run_id or uuid.uuid4().hex[:8]
    with run_log_scope(lambda line: mem.log(run_id, line)):
        env = _load_env(repo, mem, run_id)
        pending = mem.pending_promotion_features()
        session_id = None
        if pending:
            def _recency_key(f: dict) -> tuple:
                ext_id = f.get("external_id") or ""
                return (f.get("updated_at") or f.get("ts") or "", int(ext_id) if ext_id.isdigit() else 0)

            chosen = max(pending, key=_recency_key)
            session_id = chosen.get("session_id")
            mem.log(
                run_id,
                f"promote: resuming build session from {chosen['feature']!r} "
                f"(external_id={chosen.get('external_id')}, session_id={session_id!r})",
            )
            for other in pending:
                if other is not chosen:
                    mem.log(
                        run_id,
                        f"promote: {other['feature']!r} (external_id={other.get('external_id')}) "
                        f"carried by this promote run, not independently resumed",
                    )
        mem.log(run_id, "promote: human-approved promotion to production starting")
        strategy = deployment.PROD_STRATEGIES[env.deploy_strategy]
        promote = await strategy(repo, env, run_id, session_id)
        ok = promote.ok and (promote.json_data or {}).get("status") != "failed"
        mem.log(run_id, f"promote: ok={ok}")
        if not ok:
            mem.record_failure(run_id, "prod-promote", promote.text)
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
    with run_log_scope(lambda line: mem.log(run_id, line)):
        mem.log(run_id, f"prod-debug start | symptom={symptom!r}")
        env = _load_env(repo, mem, run_id)

        mem.log(run_id, "prod-debug agent: diagnosing")
        diag = await prod_debug.diagnose(repo, env, symptom)
        mem.log(run_id, f"prod-debug agent: ok={diag.ok} turns={diag.turns} cost=${diag.cost_usd:.4f}")
        if not diag.ok:
            # The agent itself failed (crashed/errored) -- distinct from
            mem.record_failure(run_id, "prod-debug", diag.text)
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
        # One Claude session across build -> deploy -> verify -> promote for this
        session_id: str | None = None
        cb = await codebase.run_cached(repo, mem)
        registry.record_agent_step(repo.name, run_id, "understand_codebase", cb.ok, cb.cost_usd, cb.turns, "understand_codebase: ok=%s" % cb.ok)
        feature_branch = feature_branch_name(env, run_id, f"hotfix-{severity}")
        impl = await implementation.run(repo, objective, f"Hotfix: {proposed_fix}", cb.text, env, feature_branch, session_id=session_id)
        session_id = impl.session_id or session_id
        mem.log(run_id, f"prod-debug: implementation ok={impl.ok}")
        registry.record_agent_step(
            repo.name, run_id, "implement_feature", impl.ok, impl.cost_usd, impl.turns, f"hotfix implementation: ok={impl.ok}"
        )
        if not impl.ok:
            mem.record_failure(run_id, "hotfix-implementation", impl.text)
            return ProdDebugReport(run_id, True, severity, False, False, diag.text)

        mem.log(run_id, "prod-debug: deploying hotfix to pre-prod for verification")
        pre_prod_strategy = deployment.PRE_PROD_STRATEGIES[env.deploy_strategy]
        deploy = await pre_prod_strategy(repo, env, feature_branch, run_id, session_id)
        session_id = deploy.session_id or session_id
        pre_prod_ok = deploy.ok and (deploy.json_data or {}).get("status") != "failed"
        registry.record_agent_step(
            repo.name, run_id, "deploy_pre_prod", pre_prod_ok, deploy.cost_usd, deploy.turns, f"hotfix deploy: ok={pre_prod_ok}"
        )
        if not pre_prod_ok:
            mem.record_failure(run_id, "hotfix-pre-prod-deploy", deploy.text)
            mem.log(run_id, "prod-debug: pre-prod deploy failed; NOT promoting to prod")
            return ProdDebugReport(run_id, True, severity, True, False, diag.text)

        preview_url = (deploy.json_data or {}).get("preview_url")
        if not preview_url:
            # Not a failure to file anywhere -- expected/normal for any app
            mem.log(run_id, "prod-debug: no preview_url returned; NOT promoting to prod")
            return ProdDebugReport(run_id, True, severity, True, False, diag.text)

        # Must verify the actual live deployment here, not re-run local tests — the whole point
        test = await testing.run_pre_prod(repo, cb.text, preview_url, run_id, session_id=session_id)
        session_id = test.session_id or session_id
        test_passed = test.ok and (test.json_data or {}).get("status") != "fail"
        mem.log(run_id, f"prod-debug: live pre-prod verification passed={test_passed}")
        registry.record_agent_step(
            repo.name, run_id, "verify_pre_prod", test_passed, test.cost_usd, test.turns, f"hotfix live verification: passed={test_passed}"
        )
        if not test_passed:
            mem.record_failure(run_id, "hotfix-testing", test.text)
            mem.log(run_id, "prod-debug: hotfix failed live pre-prod verification; NOT promoting to prod")
            return ProdDebugReport(run_id, True, severity, True, False, diag.text)

        mem.log(run_id, "prod-debug: hotfix verified live in pre-prod; auto-promoting to prod (auto_remediate_prod=true)")
        prod_strategy = deployment.PROD_STRATEGIES[env.deploy_strategy]
        promote = await prod_strategy(repo, env, run_id, session_id)
        promoted_ok = promote.ok and (promote.json_data or {}).get("status") != "failed"
        mem.log(run_id, f"prod-debug: promotion ok={promoted_ok}")
        registry.record_agent_step(
            repo.name, run_id, "promote_prod", promoted_ok, promote.cost_usd, promote.turns, f"prod promotion: ok={promoted_ok}"
        )
        if not promoted_ok:
            mem.record_failure(run_id, "hotfix-prod-promote", promote.text)

        return ProdDebugReport(run_id, True, severity, True, promoted_ok, diag.text)

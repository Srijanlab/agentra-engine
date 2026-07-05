"""Orchestrator Agent (vision.md 5.1).

Drives one full cycle: understand codebase -> discover (or accept) a feature
-> implement -> test -> deploy to preview -> assess measurability -> repeat.
If no feature is given, the Product Discovery Agent (vision.md 5.3) picks one
from the codebase, analytics, and what's already shipped — this is what makes
the system autonomous instead of a prompt-driven assistant.
"""

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agentos.agents import codebase, deployment, discovery, feedback, implementation, testing
from agentos.memory import Memory
from agentos.ranking import rank


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

    mem.log(run_id, "codebase agent: starting")
    cb = await codebase.run(repo)
    mem.write("architecture", "codebase", cb.text)
    mem.log(run_id, f"codebase agent: ok={cb.ok} turns={cb.turns} cost=${cb.cost_usd:.4f}")
    if not cb.ok:
        return CycleReport(run_id, feature or "", False, False, False, None, [], "codebase understanding failed; aborting cycle")

    opportunities: list[dict] = []
    feature_brief = feature or ""
    if feature is None:
        mem.log(run_id, "discovery agent: starting")
        disc = await discovery.run(repo, objective, cb.text, analytics_summary, mem.shipped_features())
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

    mem.log(run_id, "implementation agent: starting")
    impl = await implementation.run(repo, objective, feature_brief, cb.text)
    mem.write("features", f"{run_id}-{_slug(feature)}", impl.text)
    mem.log(run_id, f"implementation agent: ok={impl.ok} turns={impl.turns} cost=${impl.cost_usd:.4f}")
    if not impl.ok:
        mem.write("failures", f"{run_id}-implementation", impl.text)
        return CycleReport(run_id, feature, True, False, False, None, opportunities, "implementation failed; aborting cycle")
    mem.record_shipped(feature)

    mem.log(run_id, "testing agent: starting")
    test = await testing.run(repo, cb.text)
    mem.log(run_id, f"testing agent: ok={test.ok} turns={test.turns} cost=${test.cost_usd:.4f}")
    test_passed = test.ok and (test.json_data or {}).get("status") != "fail"
    if not test_passed:
        mem.write("failures", f"{run_id}-testing", test.text)

    deploy_ok = None
    if not skip_deploy and test_passed:
        mem.log(run_id, "deployment agent: starting")
        deploy = await deployment.run(repo)
        mem.log(run_id, f"deployment agent: ok={deploy.ok} turns={deploy.turns} cost=${deploy.cost_usd:.4f}")
        deploy_ok = deploy.ok and (deploy.json_data or {}).get("status") != "failed"
        if not deploy_ok:
            mem.write("failures", f"{run_id}-deployment", deploy.text)

    if test_passed:
        mem.log(run_id, "feedback agent: starting")
        fb = await feedback.run(repo, objective, feature)
        mem.write("metrics", f"{run_id}-{_slug(feature)}", fb.text)
        mem.log(run_id, f"feedback agent: ok={fb.ok} turns={fb.turns} cost=${fb.cost_usd:.4f}")

    mem.write(
        "decisions",
        f"{run_id}-summary",
        f"# Cycle {run_id}\n\nObjective: {objective}\nFeature: {feature}\n\n"
        f"- codebase: ok\n- implementation: {impl.ok}\n- testing: {test_passed}\n- deployment: {deploy_ok}\n",
    )
    mem.log(run_id, "cycle complete")

    return CycleReport(
        run_id=run_id,
        feature=feature,
        codebase_ok=True,
        implementation_ok=impl.ok,
        testing_ok=test_passed,
        deployment_ok=deploy_ok,
        opportunities_considered=opportunities,
        summary=f"feature={feature!r} implementation_ok={impl.ok} testing_ok={test_passed} deployment_ok={deploy_ok}",
    )


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:60]

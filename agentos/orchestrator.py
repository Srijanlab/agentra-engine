"""Orchestrator Agent (vision.md 5.1) — Phase 1 MVP.

Drives one full cycle: understand codebase -> implement one feature -> test
-> deploy to preview. Feature selection is still human-provided at this
stage; the Product Discovery Agent (vision.md 5.3, Phase 2) that picks
features from analytics is not built yet.
"""

import uuid
from dataclasses import dataclass
from pathlib import Path

from agentos.agents import codebase, deployment, implementation, testing
from agentos.memory import Memory


@dataclass
class CycleReport:
    run_id: str
    codebase_ok: bool
    implementation_ok: bool
    testing_ok: bool
    deployment_ok: bool | None
    summary: str


async def run_cycle(repo: Path, objective: str, feature: str, skip_deploy: bool = False) -> CycleReport:
    repo = repo.resolve()
    mem = Memory(repo)
    run_id = uuid.uuid4().hex[:8]
    mem.log(run_id, f"cycle start | objective={objective!r} feature={feature!r}")

    mem.log(run_id, "codebase agent: starting")
    cb = await codebase.run(repo)
    mem.write("architecture", "codebase", cb.text)
    mem.log(run_id, f"codebase agent: ok={cb.ok} turns={cb.turns} cost=${cb.cost_usd:.4f}")
    if not cb.ok:
        return CycleReport(run_id, False, False, False, None, "codebase understanding failed; aborting cycle")

    mem.log(run_id, "implementation agent: starting")
    impl = await implementation.run(repo, objective, feature, cb.text)
    mem.write("features", f"{run_id}-{_slug(feature)}", impl.text)
    mem.log(run_id, f"implementation agent: ok={impl.ok} turns={impl.turns} cost=${impl.cost_usd:.4f}")
    if not impl.ok:
        mem.write("failures", f"{run_id}-implementation", impl.text)
        return CycleReport(run_id, True, False, False, None, "implementation failed; aborting cycle")

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

    mem.write(
        "decisions",
        f"{run_id}-summary",
        f"# Cycle {run_id}\n\nObjective: {objective}\nFeature: {feature}\n\n"
        f"- codebase: ok\n- implementation: {impl.ok}\n- testing: {test_passed}\n- deployment: {deploy_ok}\n",
    )
    mem.log(run_id, "cycle complete")

    return CycleReport(
        run_id=run_id,
        codebase_ok=True,
        implementation_ok=impl.ok,
        testing_ok=test_passed,
        deployment_ok=deploy_ok,
        summary=f"feature={feature!r} implementation_ok={impl.ok} testing_ok={test_passed} deployment_ok={deploy_ok}",
    )


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:60]

"""Seeds realistic local fixture data so `agentra dev` shows a fully
populated dashboard without any real GitHub App or Firestore credentials --
the local-testing capability that was missing: previously the only way to
see the dashboard filled in was hand-rolled Playwright route mocks that
never touched a real browser session.

Only ever writes under AGENTRA_HOME (dev.sh points that at a scratch
directory, never the real ~/.agentra), and only matters when
AGENTRA_DEV_MODE=1 is set -- see server.py's github connector bypass for
the other half of dev mode (fakes an "installed" GitHub App so the
dashboard's ConnectGate doesn't block on a real credential).
"""

from __future__ import annotations

import datetime as dt
import time

from agentra import registry
from agentra.memory import Memory

_APPS = {
    "agentra": {
        "objective": "Improve agentra itself: hunt down and fix known gaps in its own codebase.",
        "shipped": ["Stagnation breaker", "Firestore-backed runs", "SrijanLab dashboard theme"],
        "bugs": [
            {
                "severity": "medium",
                "diagnosis": "The alarm trigger endpoint has no per-request auth.",
                "proposed_fix": "Add a shared-secret header check before wiring Cloud Monitoring to it.",
            }
        ],
    },
    "cap": {
        "objective": "Ship the creator dashboard's approvals queue.",
        "shipped": ["Approvals queue UI", "Bulk approve action"],
        "bugs": [
            {
                "severity": "high",
                "diagnosis": "Approvals queue pagination is off by one on the last page.",
                "proposed_fix": "Fix the offset calculation in listApprovals(); add a regression test.",
            }
        ],
    },
}

_FEATURE_QUEUE = {
    "cap": [{"description": "Add a keyboard shortcut to approve the focused item.", "source": "customer"}],
}


def seed(force: bool = False) -> None:
    """No-op if apps are already registered, unless force=True -- so
    re-running `agentra dev` doesn't duplicate fixture data every time."""
    if registry.list_apps() and not force:
        return

    dev_repos_root = registry.AGENTRA_HOME / "dev_repos"
    for name, fixture in _APPS.items():
        repo = dev_repos_root / name
        repo.mkdir(parents=True, exist_ok=True)
        mem = Memory(repo)
        mem.set_objective(fixture["objective"])
        for feature in fixture["shipped"]:
            mem.record_shipped(feature)
        for i, bug in enumerate(fixture["bugs"]):
            mem.record_known_bug(run_id=f"seed-{name}-{i}", **bug)
        for req in _FEATURE_QUEUE.get(name, []):
            mem.record_feature_request(**req)
        registry.register_app(name, str(repo))

    agentra_objective = _APPS["agentra"]["objective"]
    cap_objective = _APPS["cap"]["objective"]
    agentra_loop = registry.loop_id_for(agentra_objective)
    cap_loop = registry.loop_id_for(cap_objective)

    now = time.time()
    # Two runs for agentra under the same loop (same objective, unchanged
    # across both) -- what makes the Loops view worth showing instead of a
    # single-run demo: a real "end to end, across cycles" story.
    registry.record_run(
        "seed0001",
        app="agentra",
        source="scheduled",
        status="completed",
        started_at=now - 5000,
        objective=agentra_objective,
        loop_id=agentra_loop,
        result={
            "run_id": "seed0001",
            "cost_usd": 0.4021,
            "final_message": "Scanned the codebase and drafted the stagnation-breaker approach; implementation next cycle.",
            "actions": [
                "Scanned agents/brain.py for the existing circuit breaker pattern",
                "Wrote a design note on STAGNATION_WINDOW hash-based tracking",
            ],
        },
    )
    registry.record_run(
        "seed0004",
        app="agentra",
        source="scheduled",
        status="completed",
        started_at=now - 300,
        objective=agentra_objective,
        loop_id=agentra_loop,
        result={
            "run_id": "seed0004",
            "cost_usd": 0.9123,
            "final_message": (
                "Added a stagnation breaker to the orchestrator so cycles that make no "
                "real progress after 5 tool calls bail out cleanly instead of burning "
                "cost. Verified with 9 new tests."
            ),
            "actions": [
                "Implemented STAGNATION_WINDOW hash-based tracking",
                "Wrote tests/test_brain_stagnation.py (9 cases)",
                "Ran full test suite locally -- 67/67 passing",
                "Pushed to beta branch",
            ],
        },
    )
    registry.record_run(
        "seed0002",
        app="cap",
        source="on-demand",
        status="failed",
        started_at=now - 1200,
        objective=cap_objective,
        loop_id=cap_loop,
        error="pytest: 2 failed, 40 passed -- approvals queue pagination off-by-one",
    )
    registry.record_run(
        "seed0003",
        app="agentra",
        source="alarm",
        status="completed",
        started_at=now - 2000,
        objective=agentra_objective,
        loop_id=agentra_loop,
        result={
            "run_id": "seed0003",
            "cost_usd": 0.1850,
            "root_cause_found": True,
            "severity": "medium",
            "fix_attempted": True,
            "promoted_to_prod": False,
        },
    )

    for agent, ok, cost, turns, summary, app, run_id in [
        ("understand_codebase", True, 0.021, 3, "Scanned repo structure and existing patterns.", "agentra", "seed0001"),
        ("discover_opportunities", True, 0.381, 6, "Drafted the stagnation-breaker design note.", "agentra", "seed0001"),
        ("implement_feature", True, 0.84, 12, "Added stagnation breaker to OrchestratorSession.", "agentra", "seed0004"),
        ("deploy_pre_prod", True, 0.0723, 2, "Deployed to beta branch.", "agentra", "seed0004"),
        ("prod_debug", True, 0.185, 5, "Diagnosed unauthenticated alarm endpoint.", "agentra", "seed0003"),
        ("run_local_tests", False, 0.12, 4, "2 tests failing on approvals queue pagination.", "cap", "seed0002"),
    ]:
        registry.record_agent_step(app=app, run_id=run_id, agent=agent, ok=ok, cost_usd=cost, turns=turns, summary=summary)

    signals_path = registry.AGENTRA_HOME / "server.log"
    signals_path.parent.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    lines = [
        f"[{ts}] source=scheduled app='agentra' run_key=seed0004 agentra_run_id=seed0004 completed | cost=$0.9123",
        f"[{ts}] source=register app='cap' registered at {dev_repos_root / 'cap'}",
        f"[{ts}] source=alarm app='agentra' run_key=seed0003 agentra_run_id=seed0003 root_cause_found=True promoted_to_prod=False",
    ]
    with signals_path.open("a") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    seed()
    print(f"[agentra dev] seeded fixture data under {registry.AGENTRA_HOME}")

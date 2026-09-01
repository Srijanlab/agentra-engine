"""Seeds realistic local fixture data so `agentra dev` shows a fully populated dashboard without any real GitHub App or Firestore credentials -- the local-testing capability that was missing: previously the only way to see the dashboard filled in was hand-rolled Playwright route mocks that never touched a real browser session."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import time
from pathlib import Path

from agentra import registry
from agentra.agents.testing import report_path
from agentra.connectors import github_fake
from agentra.memory import Memory

_APPS = {
    "agentra": {
        "objective": "Improve agentra itself: hunt down and fix known gaps in its own codebase.",
        "repo_url": "https://github.com/RoshanSharma1/srijanlab-agentos.git",
        "bugs": [
            {
                "severity": "medium",
                "diagnosis": "The alarm trigger endpoint has no per-request auth.",
                "proposed_fix": "Add a shared-secret header check before wiring Cloud Monitoring to it.",
            },
            {
                "severity": "low",
                "diagnosis": "Signals panel timestamps render in UTC with no local-time hint.",
                "proposed_fix": "Format with toLocaleString() and show the zone abbreviation.",
            },
        ],
        "features": [
            {"description": "Per-app cost budget with a soft cap warning in the dashboard.", "source": "customer"},
            {"description": "Export a run's full agent-step transcript as markdown.", "source": "discovery"},
        ],
        "in_progress": [("Retry transient Firestore writes with backoff", "dev/9f2c-firestore-retry", "seed0006")],
        "code_complete": [
            ("Stagnation breaker for no-progress cycles", "9c104a7"),
            ("Firestore-backed run history", "ebbd94d"),
            ("Structured infra-cost gate", "3af9021"),
        ],
        "ready_to_review": [
            {
                "feature": "Two-way Slack human-input loop",
                "run_id": "seed0004",
                "report": {
                    "status": "pass",
                    "reachable": True,
                    "feature_verified": True,
                    "test_cases": [
                        {"criterion": "POST /slack/events verifies the request signature", "result": "pass", "evidence": "bad signature -> 403, valid -> 200"},
                        {"criterion": "A thread reply resumes the blocked run", "result": "pass", "evidence": "replied in thread, run went queued -> running"},
                        {"criterion": "Follow-up question lands in the same thread", "result": "pass", "evidence": "second escalation used thread_ts of the first"},
                    ],
                    "incidental_findings": [
                        {"severity": "low", "diagnosis": "The 'resuming' ack has no timestamp.", "proposed_fix": "Prefix it with the run key."},
                    ],
                    "notes": "All three criteria verified against the live pre-prod deploy.",
                },
            },
            {
                "feature": "Pre-prod health check: 120s window + crash logs",
                "run_id": "seed0008",
                "report": {
                    "status": "pass",
                    "reachable": True,
                    "feature_verified": True,
                    "test_cases": [
                        {"criterion": "A slow-starting sibling still passes within 120s", "result": "pass", "evidence": "cold container answered /health at ~74s"},
                        {"criterion": "A crashed sibling reports its last logs, not a bare timeout", "result": "pass", "evidence": "killed the process -> 'container exited during startup' + tail"},
                    ],
                    "incidental_findings": [],
                    "notes": "Timeout is env-overridable via AGENTRA_HEALTH_CHECK_ATTEMPTS.",
                },
            },
        ],
        "released": [
            ("SrijanLab dashboard theme", "b1d02aa"),
            ("Blue/green self-hosted promotion", "77c9e10"),
        ],
    },
    "cap": {
        "objective": "Ship the creator dashboard's approvals queue.",
        "repo_url": "git@github.com:ContentAutomationPlatform/ContentAutomationPlatform.git",
        "bugs": [
            {
                "severity": "high",
                "diagnosis": "Approvals queue pagination is off by one on the last page.",
                "proposed_fix": "Fix the offset calculation in listApprovals(); add a regression test.",
            },
        ],
        "features": [
            {"description": "Add a keyboard shortcut to approve the focused item.", "source": "customer"},
        ],
        "in_progress": [("Bulk-reject with a shared reason", "dev/4a1b-bulk-reject", "seed0007")],
        "code_complete": [
            ("Approvals queue UI", "a1b2c3d"),
        ],
        "ready_to_review": [
            {
                "feature": "Bulk approve action",
                "run_id": "seed0005",
                "report": {
                    "status": "pass",
                    "reachable": True,
                    "feature_verified": True,
                    "test_cases": [
                        {"criterion": "Selecting rows + Approve marks them all approved", "result": "pass", "evidence": "3 rows selected, all moved to Approved"},
                        {"criterion": "The action is disabled with no selection", "result": "pass", "evidence": "button disabled at 0 selected"},
                    ],
                    "incidental_findings": [],
                    "notes": None,
                },
            },
        ],
        "released": [
            ("Approvals list virtualization", "9de1c40"),
        ],
    },
}


def _git_init_with_remote(repo: Path, remote_url: str | None) -> None:
    """known_bugs/feature_queue/objective/environments are GitHub-only now (memory.py/environments.py derive the target via `git remote get-url origin` on this checkout, no local file fallback) -- without a real git repo + remote here, every fixture write below would silently no-op and the dev dashboard would show nothing seeded at all."""
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "agentra-dev"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text(f"# {repo.name} (dev fixture)\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "dev fixture"], cwd=repo, check=True, capture_output=True)
    if remote_url:
        subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=repo, check=True, capture_output=True)


def _walk_to_tested(mem: Memory, repo: Path, feature: str, run_id: str, report: dict) -> None:
    """in-progress -> code_complete -> shipped -> tested on one issue, plus the run<->issue
    link run_ids_for reads and the test-report artifact the Ready to Review section attaches."""
    fr = mem.record_feature_request(description=feature, source="discovery")
    if not fr or not fr.get("number"):
        return
    num = int(fr["number"])
    mem.record_in_progress_branch(num, f"dev/{run_id}", run_id=run_id)
    mem.record_code_complete(feature, run_id=run_id, resolves_id=str(num))
    mem.record_shipped_to_preprod([str(num)], run_id=run_id)
    mem.record_tested([str(num)], run_id=run_id)
    path = report_path(repo, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**report, "screenshot_captured": True}, indent=2))


def _walk_to_released(mem: Memory, repo: Path, feature: str, commit_sha: str) -> None:
    fr = mem.record_feature_request(description=feature, source="customer")
    if not fr or not fr.get("number"):
        return
    num = int(fr["number"])
    mem.record_code_complete(feature, commit_sha=commit_sha, resolves_id=str(num))
    mem.record_shipped_to_preprod([str(num)])
    mem.record_tested([str(num)])
    mem.record_released(feature, release_run_id="run_rel", commit_sha=commit_sha)
    mem.mark_status_done(num)


def seed(force: bool = False) -> None:
    """No-op if apps are already registered, unless force=True -- so
    re-running `agentra dev` doesn't duplicate fixture data every time."""
    if registry.list_apps() and not force:
        return

    github_fake.install(persist_path=registry.AGENTRA_HOME / "dev_github_fake.json")

    dev_repos_root = registry.AGENTRA_HOME / "dev_repos"
    for name, fixture in _APPS.items():
        repo = dev_repos_root / name
        repo.mkdir(parents=True, exist_ok=True)
        if not (repo / ".git").exists():
            _git_init_with_remote(repo, fixture.get("repo_url"))
        mem = Memory(repo)
        mem.set_objective(fixture["objective"])

        for i, bug in enumerate(fixture["bugs"]):
            mem.record_known_bug(run_id=f"seed-{name}-{i}", **bug)
        for req in fixture["features"]:
            mem.record_feature_request(**req)

        for feature, branch, run_id in fixture["in_progress"]:
            fr = mem.record_feature_request(description=feature, source="discovery")
            if fr and fr.get("number"):
                mem.record_in_progress_branch(int(fr["number"]), branch, run_id=run_id)

        for feature, sha in fixture["code_complete"]:
            mem.record_code_complete(feature, commit_sha=sha)

        for item in fixture["ready_to_review"]:
            _walk_to_tested(mem, repo, item["feature"], item["run_id"], item["report"])

        for feature, sha in fixture["released"]:
            _walk_to_released(mem, repo, feature, sha)

        registry.register_app(name, str(repo), repo_url=fixture.get("repo_url"), branch="main")

    _seed_runs()
    _seed_signals(dev_repos_root)


def _seed_runs() -> None:
    agentra_obj = _APPS["agentra"]["objective"]
    cap_obj = _APPS["cap"]["objective"]
    agentra_loop = registry.loop_id_for(agentra_obj)
    cap_loop = registry.loop_id_for(cap_obj)
    now = time.time()

    runs = [
        dict(run_key="seed0001", app="agentra", source="scheduled", status="completed", started_at=now - 21000,
             objective=agentra_obj, loop_id=agentra_loop, result={
                 "run_id": "seed0001", "cost_usd": 0.40, "feature": "Stagnation breaker for no-progress cycles",
                 "final_message": "Scanned the codebase and drafted the stagnation-breaker approach; implemented and pushed to beta.",
                 "actions": ["Scanned agents/brain for the circuit-breaker pattern", "Implemented STAGNATION_WINDOW tracking", "Wrote 9 tests"]}),
        dict(run_key="seed0002", app="cap", source="on-demand", status="failed", started_at=now - 14000,
             objective=cap_obj, loop_id=cap_loop, feature="Fix approvals queue pagination off-by-one",
             error="pytest: 2 failed, 40 passed -- approvals queue pagination off-by-one"),
        dict(run_key="seed0003", app="agentra", source="alarm", status="completed", started_at=now - 9000,
             objective=agentra_obj, loop_id=agentra_loop, result={
                 "run_id": "seed0003", "cost_usd": 0.185, "root_cause_found": True, "severity": "medium",
                 "fix_attempted": True, "promoted_to_prod": False, "feature": "Alarm endpoint auth"}),
        dict(run_key="seed0004", app="agentra", source="scheduled", status="completed", started_at=now - 5200,
             objective=agentra_obj, loop_id=agentra_loop, result={
                 "run_id": "seed0004", "cost_usd": 0.91, "feature": "Two-way Slack human-input loop",
                 "final_message": "Built the inbound /slack/events route + thread->run mapping; verified against pre-prod. Ready to review.",
                 "actions": ["Added POST /slack/events with signature verification", "registry.record_slack_thread/resolve_slack_thread", "Verified the resume loop in pre-prod"]}),
        dict(run_key="seed0005", app="cap", source="scheduled", status="completed", started_at=now - 3600,
             objective=cap_obj, loop_id=cap_loop, result={
                 "run_id": "seed0005", "cost_usd": 0.33, "feature": "Bulk approve action",
                 "final_message": "Shipped the bulk-approve action and live-verified it. One promote away from prod."}),
        dict(run_key="seed0006", app="agentra", source="scheduled", status="running", started_at=now - 240,
             objective=agentra_obj, loop_id=agentra_loop, feature="Retry transient Firestore writes with backoff"),
        dict(run_key="seed0007", app="cap", source="scheduled", status="queued", started_at=now - 20,
             objective=cap_obj, loop_id=cap_loop, feature="Bulk-reject with a shared reason"),
        dict(run_key="seed0008", app="agentra", source="scheduled", status="completed", started_at=now - 7800,
             objective=agentra_obj, loop_id=agentra_loop, result={
                 "run_id": "seed0008", "cost_usd": 0.52, "feature": "Pre-prod health check: 120s window + crash logs",
                 "final_message": "Widened the pre-prod health window to 120s and made a crashed sibling report its logs. Live-verified."}),
        # Same loop as seed0004 -- a first run that hit a Slack human-input pause,
        # then seed0004 resumed and finished it.
        dict(run_key="seed0009", app="agentra", source="scheduled", status="waiting_for_human", started_at=now - 9800,
             objective=agentra_obj,
             human_input={
                 "issue_number": None, "issue_url": None,
                 "question": "Should a GitHub-comment reply also resume the run, or Slack-thread only?",
                 "branch": "dev/9ac0-slack-loop", "session_id": None, "app": "agentra", "waiting_since": now - 9600,
                 "category": "product_direction",
             },
             result={
                 "run_id": "seed0009", "cost_usd": 0.44, "feature": "Two-way Slack human-input loop",
                 "final_message": "Paused: asked in Slack whether replies should also resume from a GitHub comment. Awaiting an answer."}),
        # Same loop as seed0001 -- a first pass that only got as far as the design note.
        dict(run_key="seed0010", app="agentra", source="scheduled", status="completed", started_at=now - 26000,
             objective=agentra_obj, result={
                 "run_id": "seed0010", "cost_usd": 0.12, "feature": "Stagnation breaker for no-progress cycles",
                 "final_message": "Scanned the codebase and drafted the STAGNATION_WINDOW approach; implementation next run."}),
    ]
    # One loop per tracked feature (loop_id derived from the feature, not the
    # objective) -- mirrors the "one loop = one issue" model.
    for r in runs:
        feat = r.get("feature") or (r.get("result") or {}).get("feature")
        if feat:
            r["loop_id"] = registry.loop_id_for(feat)
        registry.record_run(**r)

    steps = [
        ("understand_codebase", True, 0.02, 3, "Scanned repo structure and existing patterns.", "agentra", "seed0001"),
        ("implement_feature", True, 0.31, 9, "Added the stagnation breaker to OrchestratorSession.", "agentra", "seed0001"),
        ("run_local_tests", True, 0.07, 4, "67/67 passing after 9 new cases.", "agentra", "seed0001"),
        ("run_local_tests", False, 0.12, 4, "2 tests failing on approvals queue pagination.", "cap", "seed0002"),
        ("prod_debug", True, 0.185, 5, "Diagnosed the unauthenticated alarm endpoint.", "agentra", "seed0003"),
        ("implement_feature", True, 0.62, 14, "Built /slack/events + thread mapping.", "agentra", "seed0004"),
        ("deploy_pre_prod", True, 0.05, 2, "Merged to beta, sibling healthy in 74s.", "agentra", "seed0004"),
        ("verify_pre_prod", True, 0.24, 8, "All 3 acceptance criteria passed live.", "agentra", "seed0004"),
        ("implement_feature", True, 0.28, 8, "Bulk-approve action + disabled-state handling.", "cap", "seed0005"),
        ("implement_feature", None, 0.09, 3, "Working on Firestore retry/backoff...", "agentra", "seed0006"),
    ]
    for agent, ok, cost, turns, summary, app, run_id in steps:
        registry.record_agent_step(app=app, run_id=run_id, agent=agent, ok=ok, cost_usd=cost, turns=turns, summary=summary)


def _seed_signals(dev_repos_root: Path) -> None:
    signals_path = registry.AGENTRA_HOME / "server.log"
    signals_path.parent.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    lines = [
        f"[{ts}] source=register app='cap' registered at {dev_repos_root / 'cap'}",
        f"[{ts}] source=scheduled app='agentra' run_key=seed0004 agentra_run_id=seed0004 completed | cost=$0.91",
        f"[{ts}] source=alarm app='agentra' run_key=seed0003 root_cause_found=True promoted_to_prod=False",
        f"[{ts}] source=queue app='cap' run_key=seed0007 dispatched",
    ]
    with signals_path.open("a") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    seed()
    print(f"[agentra dev] seeded fixture data under {registry.AGENTRA_HOME}")

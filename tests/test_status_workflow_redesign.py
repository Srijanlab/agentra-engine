"""Regression lock-in for the status:code_complete -> status:shipped ->
status:tested -> status:done pipeline (replacing the old single
status:shipped stage), and check_backlog's new priority order:
need_human bugs excluded; shipped-pending-test, then code_complete-
pending-merge, then in-progress, then not-yet-started bugs/features.

Covers:
1. The three new lifecycle label transitions against the fake backend.
2. implement_feature stamps code_complete only when the push actually
   succeeded (AgentResult.pushed) -- a failed push is a real, surfaced
   failure, not silently ok=True.
3. deploy_pre_prod moves code_complete -> shipped on success (both the
   trivial-merge and live-deploy paths) and tracks which issues shipped
   this cycle.
4. verify_pre_prod moves shipped -> tested on a passing live check.
5. check_backlog surfaces all five buckets in priority order.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from agentra import registry
from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.connectors import github_fake, github_issues
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    return repo


def _session(mem: Memory) -> brain.OrchestratorSession:
    return brain.OrchestratorSession(
        repo=mem.repo, objective="test objective", env=EnvironmentConfig(), mem=mem,
        run_id="testrun1", cb_summary="a codebase summary",
    )


def _tool(session, name):
    return next(t for t in brain._tools_for(session) if t.name == name)


def _patch_registry(monkeypatch):
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)
    monkeypatch.setattr(registry, "record_run", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _stub_requirements(monkeypatch):
    async def fake_run(*a, **k):
        return AgentResult(ok=False, text="stubbed -- no spec", json_data=None, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.requirements, "run", fake_run)


# ── 1. Lifecycle label transitions against the fake backend ────────────────


def test_mark_code_complete_then_shipped_to_preprod_then_tested(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo_url = "https://github.com/acme/repo.git"
    issue = github_issues.create_issue(repo_url, "Some feature", "body", labels=["feature", "agentra"])
    n = issue["number"]

    github_issues.mark_code_complete(repo_url, n)
    assert "status:code_complete" in github_issues.get_issue(repo_url, n)["labels"]

    github_issues.mark_shipped_to_preprod(repo_url, n)
    labels = github_issues.get_issue(repo_url, n)["labels"]
    assert "status:code_complete" not in labels
    assert "status:shipped" in labels

    github_issues.mark_tested(repo_url, n)
    labels = github_issues.get_issue(repo_url, n)["labels"]
    assert "status:shipped" not in labels
    assert "status:tested" in labels


# ── 2. Push-failure gates code_complete ─────────────────────────────────────


def test_implement_feature_does_not_mark_code_complete_when_push_fails(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    session = _session(mem)
    _patch_registry(monkeypatch)

    async def fake_impl_run(*a, **k):
        return AgentResult(ok=True, text="implemented but push failed", json_data={"feature": "X"}, cost_usd=0.01, turns=1, push_failed=True)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)
    record_calls = []
    monkeypatch.setattr(mem, "record_code_complete", lambda *a, **k: record_calls.append(1) or {"issue_number": 1, "board_issue_number": 1})

    result = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": "New thing", "resolves_origin": "new"}))

    assert result["is_error"] is True
    assert "push" in result["content"][0]["text"].lower()
    assert record_calls == []  # never reached record_code_complete
    assert session.code_complete_issue_numbers == []


def test_implement_feature_marks_code_complete_when_push_succeeds(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    session = _session(mem)
    _patch_registry(monkeypatch)

    async def fake_impl_run(*a, **k):
        return AgentResult(ok=True, text="done", json_data={"feature": "X"}, cost_usd=0.01, turns=1)

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)
    monkeypatch.setattr(mem, "record_code_complete", lambda *a, **k: {"issue_number": 7, "board_issue_number": 7})
    monkeypatch.setattr(mem, "append_documentation", lambda *a, **k: None)

    result = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": "New thing", "resolves_origin": "new"}))

    assert result.get("is_error") is not True
    assert session.code_complete_issue_numbers == ["7"]


# ── 3 & 4. deploy_pre_prod / verify_pre_prod move the pipeline forward ─────


def test_deploy_pre_prod_trivial_merge_moves_code_complete_to_shipped_not_tested(tmp_path, monkeypatch):
    """Trivial merges skip verify_pre_prod entirely, so they must land on
    status:shipped and stay there -- never queued for a status:tested move."""
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    session = _session(mem)
    session.feature_branch = "dev/test-branch"
    session.tests_passed = True
    session.code_complete_issue_numbers = ["3"]
    _patch_registry(monkeypatch)

    from agentra import change_risk as change_risk_mod
    monkeypatch.setattr(change_risk_mod, "classify_change", lambda *a, **k: change_risk_mod.TRIVIAL)

    async def fake_merge(*a, **k):
        return AgentResult(ok=True, text="merged", json_data=None, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.deployment, "merge_to_pre_prod_only", fake_merge)
    moved_calls = []
    monkeypatch.setattr(mem, "record_shipped_to_preprod", lambda ids, run_id=None: moved_calls.append(list(ids)) or list(ids))

    result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert moved_calls == [["3"]]
    assert session.code_complete_issue_numbers == []
    assert session.shipped_this_cycle_issue_numbers == []  # deliberately not queued for testing


def test_deploy_pre_prod_live_deploy_moves_code_complete_to_shipped_and_queues_for_testing(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    session = _session(mem)
    session.feature_branch = "dev/test-branch"
    session.tests_passed = True
    session.code_complete_issue_numbers = ["3"]
    _patch_registry(monkeypatch)

    from agentra import change_risk as change_risk_mod
    monkeypatch.setattr(change_risk_mod, "classify_change", lambda *a, **k: change_risk_mod.STANDARD)

    async def fake_deploy(*a, **k):
        return AgentResult(ok=True, text="deployed", json_data={"status": "deployed", "preview_url": "https://preview"}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.deployment, "PRE_PROD_STRATEGIES", {"vercel_firebase": fake_deploy})
    session.env.deploy_strategy = "vercel_firebase"
    moved_calls = []
    monkeypatch.setattr(mem, "record_shipped_to_preprod", lambda ids, run_id=None: moved_calls.append(list(ids)) or list(ids))

    result = asyncio.run(_tool(session, "deploy_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert moved_calls == [["3"]]
    assert session.code_complete_issue_numbers == []
    assert session.shipped_this_cycle_issue_numbers == ["3"]


def test_verify_pre_prod_moves_shipped_to_tested_on_pass(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    session = _session(mem)
    session.pre_prod_url = "https://preview"
    session.shipped_this_cycle_issue_numbers = ["3"]
    _patch_registry(monkeypatch)

    async def fake_run_pre_prod(*a, **k):
        return AgentResult(ok=True, text="passed", json_data={"status": "pass", "reachable": True, "feature_verified": True}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.testing, "run_pre_prod", fake_run_pre_prod)
    tested_calls = []
    monkeypatch.setattr(mem, "record_tested", lambda ids, run_id=None: tested_calls.append(list(ids)) or list(ids))

    result = asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert result.get("is_error") is not True
    assert tested_calls == [["3"]]
    assert session.shipped_this_cycle_issue_numbers == []


def test_verify_pre_prod_does_not_advance_to_tested_on_failure(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    session = _session(mem)
    session.pre_prod_url = "https://preview"
    session.shipped_this_cycle_issue_numbers = ["3"]
    _patch_registry(monkeypatch)

    async def fake_run_pre_prod(*a, **k):
        return AgentResult(ok=True, text="failed", json_data={"status": "fail", "reachable": True, "feature_verified": False}, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.testing, "run_pre_prod", fake_run_pre_prod)
    monkeypatch.setattr(mem, "record_failure", lambda *a, **k: None)
    tested_calls = []
    monkeypatch.setattr(mem, "record_tested", lambda ids, run_id=None: tested_calls.append(list(ids)) or list(ids))

    result = asyncio.run(_tool(session, "verify_pre_prod").handler({}))

    assert result["is_error"] is True
    assert tested_calls == []
    assert session.shipped_this_cycle_issue_numbers == ["3"]  # still queued for a future pass


# ── 5. check_backlog's priority order ───────────────────────────────────────


def test_check_backlog_surfaces_all_five_buckets_in_priority_order(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    repo_url = mem._repo_url()
    session = _session(mem)
    _patch_registry(monkeypatch)

    shipped_issue = github_issues.create_issue(repo_url, "Shipped feature", "body", labels=["feature", "agentra"])
    github_issues.mark_code_complete(repo_url, shipped_issue["number"])
    github_issues.mark_shipped_to_preprod(repo_url, shipped_issue["number"])

    code_complete_issue = github_issues.create_issue(repo_url, "Code-complete feature", "body", labels=["feature", "agentra"])
    github_issues.mark_code_complete(repo_url, code_complete_issue["number"])

    bug_issue = github_issues.create_issue(repo_url, "A real bug", "body", labels=["bug", "agentra"])
    need_human_issue = github_issues.create_issue(repo_url, "Needs a human bug", "body", labels=["bug", "agentra", "need_human"])
    queue_issue = github_issues.create_issue(repo_url, "A feature request", "body", labels=["feature", "agentra"])

    result = asyncio.run(_tool(session, "check_backlog").handler({}))
    text = result["content"][0]["text"]

    # Priority order: shipped-pending-test text appears before code-complete,
    # which appears before the bug backlog, which appears before the queue.
    assert text.index("Shipped feature") < text.index("Code-complete feature")
    assert text.index("Code-complete feature") < text.index("A real bug")
    assert text.index("A real bug") < text.index("A feature request")
    # need_human bugs are excluded from the bugs bucket specifically (bucket 5).
    bugs_section = text[text.index("5. Known bugs"):text.index("6. Feature request queue")]
    assert "Needs a human bug" not in bugs_section

    assert str(shipped_issue["number"]) in session.backlog_ids_shown
    assert str(code_complete_issue["number"]) in session.backlog_ids_shown
    assert str(bug_issue["number"]) in session.backlog_ids_shown
    assert str(queue_issue["number"]) in session.backlog_ids_shown


def test_check_backlog_ranks_in_progress_single_part_items_above_bugs_and_queue(tmp_path, monkeypatch):
    """GitHub issue #87: a single-part bug/feature already stamped status:in-progress (real
    work started -- a branch, prior attempts) must not rank alongside never-touched backlog
    items. It gets its own bucket (4), above known bugs (5) and the feature queue (6), and is
    excluded from both of those so it isn't listed twice."""
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    repo_url = mem._repo_url()
    session = _session(mem)
    _patch_registry(monkeypatch)

    in_progress_bug = github_issues.create_issue(repo_url, "An in-progress bug", "body", labels=["bug", "agentra"])
    mem.record_in_progress_branch(in_progress_bug["number"], "dev/in-progress-bug-branch")

    in_progress_feature = github_issues.create_issue(repo_url, "An in-progress feature", "body", labels=["feature", "agentra"])
    mem.record_in_progress_branch(in_progress_feature["number"], "dev/in-progress-feature-branch")

    fresh_bug = github_issues.create_issue(repo_url, "A fresh untouched bug", "body", labels=["bug", "agentra"])
    fresh_feature = github_issues.create_issue(repo_url, "A fresh untouched feature", "body", labels=["feature", "agentra"])

    result = asyncio.run(_tool(session, "check_backlog").handler({}))
    text = result["content"][0]["text"]

    assert text.index("An in-progress bug") < text.index("5. Known bugs")
    assert text.index("An in-progress feature") < text.index("5. Known bugs")

    bucket_4 = text[text.index("4. In-progress single-part"):text.index("5. Known bugs")]
    assert "An in-progress bug" in bucket_4
    assert "An in-progress feature" in bucket_4
    assert "dev/in-progress-bug-branch" in bucket_4

    # Not double-listed in their native buckets.
    bugs_section = text[text.index("5. Known bugs"):text.index("6. Feature request queue")]
    assert "An in-progress bug" not in bugs_section
    assert "A fresh untouched bug" in bugs_section

    queue_section = text[text.index("6. Feature request queue"):text.index("Already shipped")]
    assert "An in-progress feature" not in queue_section
    assert "A fresh untouched feature" in queue_section

    assert str(in_progress_bug["number"]) in session.backlog_ids_shown
    assert str(in_progress_feature["number"]) in session.backlog_ids_shown

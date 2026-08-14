"""Regression tests for Memory.known_bugs()/feature_queue() (and their
record_*/clear_* counterparts): GitHub Issues are the ONLY backlog store
-- no local known_bugs.json/feature_queue.json mirror at all, a
deliberate availability tradeoff (see memory.py's module comment). A repo
with no github.com remote, or an unreachable GitHub API, simply has no
visible backlog -- reads return [], writes are a no-op (logged as an
error, since there's nowhere else for the report to go).

Real local git repos on disk (git init + `git remote add origin ...`),
same pattern as test_registry_sync.py. github_issues' actual HTTP calls
are monkeypatched -- no real GitHub API traffic.
"""

import subprocess
from pathlib import Path

from agentra.connectors import github_issues
from agentra.memory import Memory


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(path: Path, remote: str | None = "https://github.com/acme/app.git") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    if remote:
        _git(path, "remote", "add", "origin", remote)
    return path


# ── known_bugs() reads live from GitHub ───────────────────────────────────


def test_known_bugs_reads_from_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues,
        "list_open_issues",
        lambda repo_url, labels=None: [
            {"number": 7, "title": "Pagination bug", "body": "off by one", "html_url": "https://github.com/acme/app/issues/7"}
        ],
    )

    bugs = mem.known_bugs()

    assert bugs == [
        {
            "run_id": "7",
            "severity": "medium",
            "diagnosis": "Pagination bug",
            "proposed_fix": "off by one",
            "source": "github",
            "external_id": "7",
            "html_url": "https://github.com/acme/app/issues/7",
            "needs_human": False,
            "blocking_agentra": False,
        }
    ]


def test_known_bugs_surfaces_need_human_and_blocking_agentra_labels(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues,
        "list_open_issues",
        lambda repo_url, labels=None: [
            {
                "number": 7,
                "title": "403 Write access to repository not granted",
                "body": "...",
                "labels": [{"name": "bug"}, {"name": "need_human"}, {"name": "blocking_agentra"}],
            }
        ],
    )

    bugs = mem.known_bugs()

    assert bugs[0]["needs_human"] is True
    assert bugs[0]["blocking_agentra"] is True


def test_blocking_bugs_filters_to_only_blocking_agentra(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues,
        "list_open_issues",
        lambda repo_url, labels=None: [
            {"number": 5, "title": "Cosmetic bug", "labels": [{"name": "bug"}]},
            {
                "number": 7,
                "title": "403 Write access to repository not granted",
                "labels": [{"name": "bug"}, {"name": "need_human"}, {"name": "blocking_agentra"}],
            },
        ],
    )

    blocking = mem.blocking_bugs()

    assert [b["external_id"] for b in blocking] == ["7"]


def test_known_bugs_excludes_a_bug_already_marked_shipped(tmp_path, monkeypatch):
    # A fixed bug awaiting production promotion is still open (mark_shipped
    # leaves it that way), but it's no longer "a bug still needing a fix" --
    # see closed_bugs() for where it surfaces instead.
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues,
        "list_open_issues",
        lambda repo_url, labels=None: [
            {"number": 7, "title": "Fixed already", "labels": [{"name": "bug"}, {"name": "status:shipped"}]},
            {"number": 8, "title": "Still broken", "labels": [{"name": "bug"}]},
        ],
    )

    bugs = mem.known_bugs()

    assert [b["external_id"] for b in bugs] == ["8"]


def test_known_bugs_returns_empty_without_a_github_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)

    assert mem.known_bugs() == []


def test_known_bugs_returns_empty_when_github_call_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    def _raise(*args, **kwargs):
        raise github_issues.GitHubIssuesError("boom")

    monkeypatch.setattr(github_issues, "list_open_issues", _raise)

    assert mem.known_bugs() == []


# ── record_known_bug() always creates a GitHub issue ─────────────────────


def test_record_known_bug_creates_a_github_issue(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    created = {}

    def fake_create(repo_url, title, body, labels=None):
        created["title"] = title
        created["labels"] = labels
        return {"number": 99}

    monkeypatch.setattr(github_issues, "create_issue", fake_create)

    mem.record_known_bug("run1", "high", "Checkout crashes", "null-check the cart")

    assert created["title"] == "Checkout crashes"
    assert created["labels"] == ["bug", "agentra"]


def test_record_known_bug_with_a_title_uses_it_as_the_issue_title_and_keeps_diagnosis_in_the_body(tmp_path, monkeypatch):
    # Dashboard submissions collect a short title separately from the fuller
    # description -- the title becomes the issue's actual title (instead of
    # a possibly much longer diagnosis), diagnosis lands as a "Description:"
    # body line that known_bugs()/_issue_description parse back out.
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    created = {}
    monkeypatch.setattr(
        github_issues,
        "create_issue",
        lambda repo_url, title, body, labels=None: created.update(title=title, body=body) or {"number": 99},
    )

    mem.record_known_bug(
        "run1", "high", "Checkout crashes on an empty cart, seen on the mobile app only", "null-check the cart",
        title="Checkout crashes on mobile",
    )

    assert created["title"] == "Checkout crashes on mobile"
    assert "Description: Checkout crashes on an empty cart, seen on the mobile app only" in created["body"]


def test_record_known_bug_adds_need_human_and_blocking_agentra_labels(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    created = {}

    monkeypatch.setattr(
        github_issues, "create_issue", lambda repo_url, title, body, labels=None: created.update(labels=labels) or {"number": 99}
    )

    mem.record_known_bug(
        "run1", "high", "403 Write access to repository not granted", "grant App write access",
        needs_human=True, blocking_agentra=True,
    )

    assert created["labels"] == ["bug", "agentra", "need_human", "blocking_agentra"]


def test_record_known_bug_adds_labels_to_an_existing_duplicate_instead_of_a_fresh_issue(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(
        github_issues,
        "list_open_issues",
        lambda repo_url, labels=None: [{"number": 7, "title": "403 Write access to repository not granted", "body": "..."}],
    )
    monkeypatch.setattr(github_issues, "add_comment", lambda *a, **k: None)
    labeled = {}
    monkeypatch.setattr(
        github_issues, "add_labels", lambda repo_url, issue_number, labels: labeled.update(issue_number=issue_number, labels=labels)
    )
    monkeypatch.setattr(
        github_issues, "create_issue", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not file a duplicate"))
    )

    mem.record_known_bug(
        "run2", "high", "403 Write access to repository not granted", "grant App write access",
        needs_human=True, blocking_agentra=True,
    )

    assert labeled == {"issue_number": 7, "labels": ["need_human", "blocking_agentra"]}


def test_record_known_bug_is_a_noop_without_a_github_remote(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)

    def fail_create(*a, **k):
        raise AssertionError("should not attempt a GitHub call with no remote")

    monkeypatch.setattr(github_issues, "create_issue", fail_create)

    mem.record_known_bug("run1", "high", "Checkout crashes", "null-check the cart")  # must not raise


def test_record_known_bug_comments_on_a_similar_open_bug_instead_of_filing_a_duplicate(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(
        github_issues,
        "list_open_issues",
        lambda repo_url, labels=None: [
            {"number": 7, "title": "403 Write access to repository not granted", "body": "..."}
        ],
    )
    monkeypatch.setattr(
        github_issues, "create_issue", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not file a duplicate"))
    )
    comments = []
    monkeypatch.setattr(
        github_issues, "add_comment", lambda repo_url, issue_number, comment: comments.append((issue_number, comment))
    )

    mem.record_known_bug("run2", "high", "403 Write access to repository not granted", "investigate git auth")

    assert len(comments) == 1
    assert comments[0][0] == 7
    assert "run2" in comments[0][1]


def test_record_known_bug_files_a_fresh_issue_when_no_similar_bug_is_open(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(
        github_issues, "list_open_issues", lambda repo_url, labels=None: [{"number": 3, "title": "Totally unrelated bug", "body": ""}]
    )
    created = {}
    monkeypatch.setattr(
        github_issues, "create_issue", lambda repo_url, title, body, labels=None: created.update(title=title) or {"number": 8}
    )
    monkeypatch.setattr(github_issues, "add_comment", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not comment")))

    mem.record_known_bug("run1", "high", "Checkout crashes on empty cart", "null-check the cart")

    assert created["title"] == "Checkout crashes on empty cart"


def test_record_known_bug_logs_when_github_create_fails(tmp_path, monkeypatch, caplog):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    def _raise(*a, **k):
        raise github_issues.GitHubIssuesError("rate limited")

    monkeypatch.setattr(github_issues, "create_issue", _raise)

    mem.record_known_bug("run1", "high", "Checkout crashes", "null-check the cart")  # must not raise


# ── clear_known_bug() marks the GitHub issue shipped (stays open) when the id is numeric ────────


def test_clear_known_bug_marks_the_github_issue_shipped(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    marked = {}

    def fake_mark_shipped(repo_url, issue_number, comment=None):
        marked["issue_number"] = issue_number
        marked["comment"] = comment

    monkeypatch.setattr(github_issues, "mark_shipped", fake_mark_shipped)

    mem.clear_known_bug("42")

    assert marked["issue_number"] == 42


def test_clear_known_bug_passes_through_a_custom_resolution_note(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    marked = {}

    monkeypatch.setattr(
        github_issues, "mark_shipped", lambda repo_url, issue_number, comment=None: marked.update(comment=comment)
    )

    mem.clear_known_bug("42", "Resolved by agentra: shipped as 'Fix pagination' (commit abc1234)")

    assert marked["comment"] == "Resolved by agentra: shipped as 'Fix pagination' (commit abc1234)"


def test_clear_known_bug_with_non_numeric_id_does_not_call_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    def fail_mark_shipped(*a, **k):
        raise AssertionError("should not call GitHub for a non-numeric id")

    monkeypatch.setattr(github_issues, "mark_shipped", fail_mark_shipped)

    mem.clear_known_bug("run-abc")  # must not raise


# ── feature_queue() mirrors the same behavior with the "feature" label ──


def test_feature_queue_reads_from_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    captured = {}

    def fake_list(repo_url, labels=None):
        captured["labels"] = labels
        return [{"number": 5, "title": "Add dark mode"}]

    monkeypatch.setattr(github_issues, "list_open_issues", fake_list)

    queue = mem.feature_queue()

    assert captured["labels"] == ["feature", "agentra"]
    assert queue == [{"description": "Add dark mode", "source": "github", "external_id": "5", "html_url": None}]


def test_feature_queue_returns_empty_without_a_github_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)

    assert mem.feature_queue() == []


def test_feature_queue_excludes_a_feature_already_marked_shipped(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues,
        "list_open_issues",
        lambda repo_url, labels=None: [
            {"number": 5, "title": "Already shipped", "labels": [{"name": "feature"}, {"name": "status:shipped"}]},
            {"number": 6, "title": "Not started", "labels": [{"name": "feature"}]},
        ],
    )

    queue = mem.feature_queue()

    assert [f["external_id"] for f in queue] == ["6"]


def test_in_progress_features_reads_from_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    captured = {}

    def fake_list(repo_url, labels=None):
        captured["labels"] = labels
        return [
            {
                "number": 10,
                "title": "Big feature",
                "body": "Tracks a multi-part feature.",
                "html_url": "https://github.com/acme/app/issues/10",
                "sub_issues_total": 3,
                "sub_issues_completed": 1,
            }
        ]

    monkeypatch.setattr(github_issues, "list_in_progress_features", fake_list)

    result = mem.in_progress_features()

    assert captured["labels"] == ["feature", "agentra"]
    assert result == [
        {
            "description": "Big feature",
            "external_id": "10",
            "sub_issues_total": 3,
            "sub_issues_completed": 1,
            "html_url": "https://github.com/acme/app/issues/10",
        }
    ]


def test_in_progress_features_returns_empty_without_a_github_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)

    assert mem.in_progress_features() == []


def test_in_progress_features_filters_out_a_feature_with_nothing_shipped_yet(tmp_path, monkeypatch):
    # The real bug this fixes: a feature can have open sub-issues (a manual
    # breakdown/plan) with nothing actually shipped yet -- subIssuesSummary.total
    # > 0 alone can't tell that apart from genuine progress. sub_issues_completed
    # > 0 is the authoritative "has work actually begun" signal instead.
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues,
        "list_in_progress_features",
        lambda repo_url, labels=None: [
            {
                "number": 2, "title": "Planned but not started", "body": None,
                "html_url": "https://github.com/acme/app/issues/2", "sub_issues_total": 2, "sub_issues_completed": 0,
            }
        ],
    )

    assert mem.in_progress_features() == []


def test_record_feature_request_creates_a_github_issue(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    created = {}

    def fake_create(repo_url, title, body, labels=None):
        created["title"] = title
        created["labels"] = labels
        return {"number": 11}

    monkeypatch.setattr(github_issues, "create_issue", fake_create)

    mem.record_feature_request("Add keyboard shortcuts")

    assert created["title"] == "Add keyboard shortcuts"
    assert created["labels"] == ["feature", "agentra"]


def test_record_feature_request_with_a_title_uses_it_as_the_issue_title(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    created = {}
    monkeypatch.setattr(
        github_issues,
        "create_issue",
        lambda repo_url, title, body, labels=None: created.update(title=title, body=body) or {"number": 11},
    )

    mem.record_feature_request("Let power users jump between panels without the mouse", title="Keyboard shortcuts")

    assert created["title"] == "Keyboard shortcuts"
    assert "Description: Let power users jump between panels without the mouse" in created["body"]


def test_clear_feature_request_closes_the_github_issue(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    closed = {}

    monkeypatch.setattr(
        github_issues, "close_issue", lambda repo_url, issue_number, comment=None: closed.update(issue_number=issue_number)
    )

    mem.clear_feature_request("11")

    assert closed["issue_number"] == 11


# ── resume capability: durable branch pointer for interrupted implement_feature work ──


def test_record_in_progress_branch_delegates_to_github_issues(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    captured = {}
    monkeypatch.setattr(
        github_issues,
        "record_in_progress_branch",
        lambda repo_url, issue_number, branch, run_id=None: captured.update(issue_number=issue_number, branch=branch, run_id=run_id),
    )
    labeled = {}
    monkeypatch.setattr(
        github_issues, "add_labels", lambda repo_url, issue_number, labels: labeled.update(issue_number=issue_number, labels=labels)
    )

    mem.record_in_progress_branch(13, "dev/abc123-fix-sort-order", "run42")

    assert captured == {"issue_number": 13, "branch": "dev/abc123-fix-sort-order", "run_id": "run42"}
    assert labeled == {"issue_number": 13, "labels": ["status:in-progress"]}


def test_record_in_progress_branch_is_a_noop_without_a_github_remote(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues, "record_in_progress_branch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GitHub"))
    )

    mem.record_in_progress_branch(13, "dev/abc123")  # must not raise


def test_resume_branch_for_delegates_to_github_issues(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues,
        "get_in_progress_branch",
        lambda repo_url, issue_number: "dev/abc123-fix-sort-order" if issue_number == 13 else None,
    )

    assert mem.resume_branch_for("13") == "dev/abc123-fix-sort-order"
    assert mem.resume_branch_for("99") is None


def test_resume_branch_for_returns_none_for_a_non_numeric_id(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues, "get_in_progress_branch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GitHub"))
    )

    assert mem.resume_branch_for("not-a-number") is None


def test_resume_branch_for_returns_none_without_a_github_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)

    assert mem.resume_branch_for("13") is None


def test_resume_branch_for_returns_none_when_github_call_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues, "get_in_progress_branch", lambda *a, **k: (_ for _ in ()).throw(github_issues.GitHubIssuesError("boom"))
    )

    assert mem.resume_branch_for("13") is None


# ── closed_bugs(): resolved bugs, paired with shipped_features() for the ──
# dashboard's combined "release to production" view                       ──


def test_closed_bugs_reads_from_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    captured = {}
    monkeypatch.setattr(github_issues, "list_open_issues", lambda repo_url, labels=None: [])

    def fake_list(repo_url, labels=None, limit=30):
        captured["labels"] = labels
        return [
            {
                "number": 6, "title": "Fixed bug", "body": "details",
                "html_url": "https://github.com/acme/app/issues/6", "closed_at": "2026-08-13T00:00:00Z",
            }
        ]

    monkeypatch.setattr(github_issues, "list_closed_issues", fake_list)

    bugs = mem.closed_bugs()

    assert captured["labels"] == ["bug", "agentra"]
    assert bugs == [
        {
            "run_id": "6",
            "severity": "medium",
            "diagnosis": "Fixed bug",
            "proposed_fix": "details",
            "source": "github",
            "external_id": "6",
            "html_url": "https://github.com/acme/app/issues/6",
            "closed_at": "2026-08-13T00:00:00Z",
            "status_done": False,
        }
    ]


def test_closed_bugs_returns_empty_without_a_github_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)

    assert mem.closed_bugs() == []


def test_closed_bugs_returns_empty_when_github_call_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues, "list_closed_issues", lambda *a, **k: (_ for _ in ()).throw(github_issues.GitHubIssuesError("boom"))
    )

    assert mem.closed_bugs() == []


# ── record_commit(): links a commit on the tracking issue ─────────────────


def test_record_commit_delegates_to_github_issues(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    captured = {}
    monkeypatch.setattr(
        github_issues,
        "record_commit",
        lambda repo_url, issue_number, commit_sha: captured.update(issue_number=issue_number, commit_sha=commit_sha),
    )

    mem.record_commit(13, "abc1234")

    assert captured == {"issue_number": 13, "commit_sha": "abc1234"}


def test_record_commit_is_a_noop_without_a_github_remote(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues, "record_commit", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GitHub"))
    )

    mem.record_commit(13, "abc1234")  # must not raise


# ── mark_status_done() / resume_run_id_for() ───────────────────────────────


def test_mark_status_done_labels_and_closes_the_issue(tmp_path, monkeypatch):
    # status:done and "closed" happen together under the current model -- an
    # issue stays open through the shipped stage (mark_shipped), and only
    # production promotion (this method) finally closes it.
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    labeled = {}
    closed = {}
    monkeypatch.setattr(
        github_issues,
        "add_labels",
        lambda repo_url, issue_number, labels: labeled.update(issue_number=issue_number, labels=labels),
    )
    monkeypatch.setattr(
        github_issues,
        "close_issue",
        lambda repo_url, issue_number, comment=None: closed.update(issue_number=issue_number, comment=comment),
    )

    mem.mark_status_done(13)

    assert labeled == {"issue_number": 13, "labels": ["status:done"]}
    assert closed["issue_number"] == 13


def test_mark_status_done_is_a_noop_without_a_github_remote(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)
    monkeypatch.setattr(github_issues, "add_labels", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GitHub")))
    monkeypatch.setattr(github_issues, "close_issue", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GitHub")))

    mem.mark_status_done(13)  # must not raise


def test_resume_run_id_for_delegates_to_github_issues(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues,
        "get_in_progress_run_id",
        lambda repo_url, issue_number: "run42" if issue_number == 13 else None,
    )

    assert mem.resume_run_id_for("13") == "run42"
    assert mem.resume_run_id_for("99") is None


def test_resume_run_id_for_returns_none_for_a_non_numeric_id(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues, "get_in_progress_run_id", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GitHub"))
    )

    assert mem.resume_run_id_for("not-a-number") is None

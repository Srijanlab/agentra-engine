"""Regression tests for Memory.shipped_features()/record_shipped(): a
shipped feature is an open GitHub 'feature'-labeled issue stamped
"status:shipped" -- it only actually closes once promoted to production
(mark_status_done). record_shipped() either marks-shipped the
feature_queue issue it resolves (resolves_id) or opens-and-marks-shipped a
fresh one, stamping run_id/commit_sha into the issue body so
shipped_features() can read them straight back out. Same pattern as
test_memory_github_backlog.py: real local git repos on disk, github_issues'
HTTP calls monkeypatched.
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


def test_shipped_features_reads_from_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(github_issues, "list_open_issues", lambda repo_url, labels=None: [])
    monkeypatch.setattr(
        github_issues,
        "list_closed_issues",
        lambda repo_url, labels=None, limit=30: [
            {
                "number": 12,
                "title": "Dark mode",
                "body": "Autonomously shipped by agentra.\n\n---\nShipped-Run-ID: run42\nShipped-Commit: abc1234\n",
                "closed_at": "2026-08-10T12:00:00Z",
            }
        ],
    )

    assert mem.shipped_features() == [
        {
            "feature": "Dark mode",
            "commit_sha": "abc1234",
            "run_id": "run42",
            "session_id": None,
            "ts": "2026-08-10T12:00:00Z",
            "updated_at": None,
            "external_id": "12",
            "html_url": None,
            "status_done": False,
        }
    ]


def test_shipped_features_includes_open_issues_still_awaiting_promotion(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues,
        "list_open_issues",
        lambda repo_url, labels=None: [
            {
                "number": 12,
                "title": "Dark mode",
                "body": "---\nShipped-Run-ID: run42\nShipped-Commit: abc1234\n",
                "labels": ["feature", "agentra", "status:shipped"],
            }
        ],
    )
    monkeypatch.setattr(github_issues, "list_closed_issues", lambda repo_url, labels=None, limit=30: [])

    assert mem.shipped_features() == [
        {
            "feature": "Dark mode",
            "commit_sha": "abc1234",
            "run_id": "run42",
            "session_id": None,
            "ts": None,
            "updated_at": None,
            "external_id": "12",
            "html_url": None,
            "status_done": False,
        }
    ]


def test_shipped_features_tolerates_a_body_with_no_structured_fields(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(github_issues, "list_open_issues", lambda repo_url, labels=None: [])
    monkeypatch.setattr(
        github_issues,
        "list_closed_issues",
        lambda repo_url, labels=None, limit=30: [
            {"number": 3, "title": "Old feature", "body": None, "closed_at": "2026-01-01T00:00:00Z"}
        ],
    )

    assert mem.shipped_features() == [
        {
            "feature": "Old feature",
            "commit_sha": None,
            "run_id": None,
            "session_id": None,
            "ts": "2026-01-01T00:00:00Z",
            "updated_at": None,
            "external_id": "3",
            "html_url": None,
            "status_done": False,
        }
    ]


def test_shipped_features_returns_empty_without_a_github_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)

    assert mem.shipped_features() == []


def test_shipped_features_reads_session_id_from_github(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(github_issues, "list_open_issues", lambda repo_url, labels=None: [])
    monkeypatch.setattr(
        github_issues,
        "list_closed_issues",
        lambda repo_url, labels=None, limit=30: [
            {
                "number": 12,
                "title": "Dark mode",
                "body": "Autonomously shipped by agentra.\n\n---\nShipped-Run-ID: run42\nShipped-Session-ID: sess-abc\n",
                "closed_at": "2026-08-10T12:00:00Z",
                "updated_at": "2026-08-10T12:00:00Z",
            }
        ],
    )

    features = mem.shipped_features()

    assert features[0]["session_id"] == "sess-abc"
    assert features[0]["updated_at"] == "2026-08-10T12:00:00Z"


def test_record_shipped_creates_and_marks_shipped_a_fresh_issue_for_a_self_initiated_feature(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(github_issues, "list_open_issues", lambda repo_url, labels=None: [])
    created = {}
    marked = {}

    def _create_issue(repo_url, title, body, labels=None):
        created.update(repo_url=repo_url, title=title, body=body, labels=labels)
        return {"number": 99, "title": title, "body": body}

    def _mark_shipped(repo_url, issue_number, comment=None, body_suffix=None):
        marked.update(repo_url=repo_url, issue_number=issue_number, comment=comment, body_suffix=body_suffix)

    monkeypatch.setattr(github_issues, "create_issue", _create_issue)
    monkeypatch.setattr(github_issues, "mark_shipped", _mark_shipped)
    monkeypatch.setattr(
        github_issues, "close_issue", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not close, only mark shipped"))
    )

    result = mem.record_shipped("Dark mode", commit_sha="abc1234", run_id="run42")

    assert created["title"] == "Dark mode"
    assert created["labels"] == ["feature", "agentra"]
    assert marked["issue_number"] == 99
    assert "run42" in marked["comment"]
    assert "abc1234" in marked["comment"]
    assert "Shipped-Run-ID: run42" in marked["body_suffix"]
    assert "Shipped-Commit: abc1234" in marked["body_suffix"]
    assert "Shipped-Session-ID:" not in marked["body_suffix"]  # not passed for this call
    assert result == {"issue_number": 99, "board_issue_number": 99}


def test_record_shipped_stamps_session_id_when_given(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(github_issues, "list_open_issues", lambda repo_url, labels=None: [])
    monkeypatch.setattr(github_issues, "create_issue", lambda repo_url, title, body, labels=None: {"number": 99, "title": title, "body": body})
    marked = {}
    monkeypatch.setattr(
        github_issues,
        "mark_shipped",
        lambda repo_url, issue_number, comment=None, body_suffix=None: marked.update(body_suffix=body_suffix),
    )

    mem.record_shipped("Dark mode", commit_sha="abc1234", run_id="run42", session_id="sess-abc")

    assert "Shipped-Session-ID: sess-abc" in marked["body_suffix"]


def test_record_shipped_marks_shipped_the_originating_feature_queue_issue_instead_of_creating_a_new_one(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(
        github_issues, "create_issue", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not create a new issue"))
    )
    marked = {}
    monkeypatch.setattr(
        github_issues,
        "mark_shipped",
        lambda repo_url, issue_number, comment=None, body_suffix=None: marked.update(
            issue_number=issue_number, comment=comment, body_suffix=body_suffix
        ),
    )

    result = mem.record_shipped("Approvals queue UI", commit_sha="def5678", run_id="run7", resolves_id="42")

    assert marked["issue_number"] == 42
    assert "Shipped-Run-ID: run7" in marked["body_suffix"]
    assert result == {"issue_number": 42, "board_issue_number": 42}


def test_record_shipped_with_known_bug_issue_reuses_it_without_a_second_shipped_comment(tmp_path, monkeypatch):
    """Regression test for issue #33: a known-bug resolution used to have no
    way to tell record_shipped which issue it belongs to (resolves_id is only
    forwarded for resolves_origin=="feature_queue"), so its fallback fuzzy
    similarity check had to guess -- and sometimes missed, creating an
    orphaned duplicate instead of reusing the real tracking issue (#21).
    known_bug_issue fixes this by passing the known issue number through
    directly. mark_shipped must NOT be called here -- the caller's own
    clear_known_bug() (a separate call, not exercised by this test) already
    stamps status:shipped and posts the resolution comment on that same
    issue; calling mark_shipped again here would double the comment."""
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(
        github_issues, "create_issue", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not create a new issue"))
    )
    monkeypatch.setattr(
        github_issues, "mark_shipped", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not double-post a shipped comment"))
    )

    result = mem.record_shipped("Standup dedup fix", commit_sha="abc1234", run_id="run21", known_bug_issue="21")

    assert result == {"issue_number": 21, "board_issue_number": 21}


def test_record_shipped_with_sub_feature_of_creates_a_linked_sub_issue(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    sub_issue_calls = {}

    def _create_sub_issue(repo_url, parent_issue_number, title, body, labels=None):
        sub_issue_calls.update(parent_issue_number=parent_issue_number, title=title, labels=labels)
        return {"number": 55, "title": title}

    closed = []
    marked = []
    monkeypatch.setattr(github_issues, "create_sub_issue", _create_sub_issue)
    monkeypatch.setattr(github_issues, "close_issue", lambda repo_url, issue_number, **k: closed.append(issue_number))
    monkeypatch.setattr(
        github_issues,
        "mark_shipped",
        lambda repo_url, issue_number, comment=None, body_suffix=None: marked.append(issue_number),
    )

    result = mem.record_shipped("Part two", commit_sha="cafe123", run_id="run9", sub_feature_of="10")

    assert sub_issue_calls == {"parent_issue_number": 10, "title": "Part two", "labels": ["story", "agentra"]}
    # The sub-issue closes immediately; the PARENT (#10) gets marked shipped
    # instead -- more_parts_expected defaults False, so this call completed
    # the whole feature.
    assert closed == [55]
    assert marked == [10]
    assert result == {"issue_number": 55, "board_issue_number": 10}


def test_record_shipped_sub_feature_of_with_more_parts_expected_leaves_the_parent_open_and_unmarked(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(github_issues, "create_sub_issue", lambda *a, **k: {"number": 55, "title": "Part two"})
    closed = []
    marked = []
    monkeypatch.setattr(github_issues, "close_issue", lambda repo_url, issue_number, **k: closed.append(issue_number))
    monkeypatch.setattr(github_issues, "mark_shipped", lambda repo_url, issue_number, **k: marked.append(issue_number))

    mem.record_shipped("Part two", sub_feature_of="10", more_parts_expected=True)

    assert closed == [55]  # only the sub-issue -- the parent (#10) stays open
    assert marked == []  # not yet fully shipped -- no status:shipped on the parent


def test_record_shipped_sub_feature_of_without_more_parts_marks_the_parent_shipped_too(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(github_issues, "create_sub_issue", lambda *a, **k: {"number": 55, "title": "Final part"})
    closed = []
    marked = []
    monkeypatch.setattr(github_issues, "close_issue", lambda repo_url, issue_number, **k: closed.append(issue_number))
    monkeypatch.setattr(
        github_issues,
        "mark_shipped",
        lambda repo_url, issue_number, comment=None, body_suffix=None: marked.append((issue_number, comment)),
    )

    mem.record_shipped("Final part", run_id="run9", sub_feature_of="10", more_parts_expected=False)

    # Sub-issue closes immediately; the parent gets marked shipped (stays
    # open) -- the whole feature is done, but only production promotion
    # closes it.
    assert closed == [55]
    assert marked == [(10, "All parts shipped (run run9).")]


def test_record_shipped_starts_a_multi_part_feature_with_a_fresh_open_parent(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    created_issues = []
    monkeypatch.setattr(
        github_issues,
        "create_issue",
        lambda repo_url, title, body, labels=None: created_issues.append(title) or {"number": 20, "title": title},
    )
    monkeypatch.setattr(github_issues, "create_sub_issue", lambda repo_url, parent, title, body, labels=None: {"number": 21, "title": title})
    closed = []
    marked = []
    monkeypatch.setattr(github_issues, "close_issue", lambda repo_url, issue_number, **k: closed.append(issue_number))
    monkeypatch.setattr(github_issues, "mark_shipped", lambda repo_url, issue_number, **k: marked.append(issue_number))

    result = mem.record_shipped("Big new feature", run_id="run1", more_parts_expected=True)

    assert created_issues == ["Big new feature"]  # the fresh parent, never closed
    assert closed == [21]  # only the first part's sub-issue
    assert marked == []  # parent not fully shipped yet
    assert result == {"issue_number": 21, "board_issue_number": 20}


def test_record_shipped_starts_a_multi_part_feature_using_the_feature_queue_issue_as_parent(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(
        github_issues, "create_issue", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must reuse the feature_queue issue"))
    )
    sub_issue_calls = {}
    monkeypatch.setattr(
        github_issues,
        "create_sub_issue",
        lambda repo_url, parent, title, body, labels=None: sub_issue_calls.update(parent=parent) or {"number": 21, "title": title},
    )
    closed = []
    monkeypatch.setattr(github_issues, "close_issue", lambda repo_url, issue_number, **k: closed.append(issue_number))
    monkeypatch.setattr(github_issues, "mark_shipped", lambda repo_url, issue_number, **k: (_ for _ in ()).throw(AssertionError("parent not fully shipped yet")))

    result = mem.record_shipped("Big backlog feature", run_id="run1", resolves_id="7", more_parts_expected=True)

    assert sub_issue_calls == {"parent": 7}
    assert closed == [21]  # the sub-issue only -- issue #7 (the feature_queue entry) stays open
    assert result == {"issue_number": 21, "board_issue_number": 7}


def test_record_shipped_marks_a_similar_open_bug_shipped_instead_of_orphaning_a_fresh_issue(tmp_path, monkeypatch):
    # The real bug this fixes: a known-bug fix's caller is supposed to pass
    # resolves_id/resolves_origin, but confirmed live (issues #13/#16,
    # #1/#19, #6/#15) it doesn't always -- leaving the original bug open
    # forever while an orphaned "shipped" issue captures the fix.
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(
        github_issues,
        "list_open_issues",
        lambda repo_url, labels=None: (
            [{"number": 13, "title": "Runs within a loop are listed oldest-first, not newest-first", "body": ""}]
            if labels == ["bug", "agentra"]
            else []
        ),
    )
    monkeypatch.setattr(
        github_issues, "create_issue", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not orphan a fresh issue"))
    )
    marked = {}
    monkeypatch.setattr(
        github_issues,
        "mark_shipped",
        lambda repo_url, issue_number, comment=None, body_suffix=None: marked.update(issue_number=issue_number),
    )

    result = mem.record_shipped("Runs within a loop are listed oldest-first, not newest-first -- fixed", run_id="run1")

    assert marked["issue_number"] == 13
    assert result == {"issue_number": 13, "board_issue_number": 13}


def test_record_shipped_still_creates_a_fresh_issue_when_nothing_similar_is_open(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)

    monkeypatch.setattr(
        github_issues, "list_open_issues", lambda repo_url, labels=None: [{"number": 3, "title": "Totally unrelated", "body": ""}]
    )
    created = {}
    monkeypatch.setattr(
        github_issues, "create_issue", lambda repo_url, title, body, labels=None: created.update(title=title) or {"number": 99}
    )
    monkeypatch.setattr(github_issues, "mark_shipped", lambda *a, **k: None)

    result = mem.record_shipped("Brand new feature nobody asked for yet", run_id="run1")

    assert created["title"] == "Brand new feature nobody asked for yet"
    assert result == {"issue_number": 99, "board_issue_number": 99}


def test_record_shipped_is_a_noop_without_a_github_remote(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo", remote=None)
    mem = Memory(repo)

    called = []
    monkeypatch.setattr(github_issues, "create_issue", lambda *a, **k: called.append("create"))

    mem.record_shipped("Dark mode")

    assert called == []


# ── pending_promotion_features(): shipped but not yet in released_features() ──


def _shipped_issue(number: int, title: str, session_id: str | None = None, updated_at: str | None = None) -> dict:
    body = "---\nShipped-Run-ID: run1\n"
    if session_id:
        body += f"Shipped-Session-ID: {session_id}\n"
    return {
        "number": number, "title": title, "body": body,
        "labels": ["feature", "agentra", "status:shipped"], "updated_at": updated_at,
    }


def test_pending_promotion_features_excludes_already_released(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(
        github_issues,
        "list_open_issues",
        lambda repo_url, labels=None: [
            _shipped_issue(1, "Ready to ship", session_id="sess-1"),
            _shipped_issue(2, "Already released"),
        ],
    )
    monkeypatch.setattr(github_issues, "list_closed_issues", lambda repo_url, labels=None, limit=30: [])
    mem.record_released("Already released", release_run_id="prior-run")

    pending = mem.pending_promotion_features()

    assert [f["feature"] for f in pending] == ["Ready to ship"]
    assert pending[0]["session_id"] == "sess-1"


def test_pending_promotion_features_returns_empty_when_nothing_shipped(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(github_issues, "list_open_issues", lambda repo_url, labels=None: [])
    monkeypatch.setattr(github_issues, "list_closed_issues", lambda repo_url, labels=None, limit=30: [])

    assert mem.pending_promotion_features() == []

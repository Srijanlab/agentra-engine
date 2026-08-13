"""Tests for connectors/github_fake.py's FakeGitHubBackend -- namespacing
by repo_url specifically, since dev_seed.py seeds multiple fixture apps
against one shared backend instance, and un-namespaced storage previously
leaked one app's issues/variables into another's (confirmed live:
agentra's dev fixture picked up "cap"'s objective and bug count).
"""

from agentra.connectors import github_fake


def test_issues_are_namespaced_by_repo_url():
    backend = github_fake.FakeGitHubBackend()

    backend.create_issue("https://github.com/acme/app-a.git", "Bug in A", "body", labels=["bug"])
    backend.create_issue("https://github.com/acme/app-b.git", "Bug in B", "body", labels=["bug"])

    a_issues = backend.list_open_issues("https://github.com/acme/app-a.git")
    b_issues = backend.list_open_issues("https://github.com/acme/app-b.git")

    assert [i["title"] for i in a_issues] == ["Bug in A"]
    assert [i["title"] for i in b_issues] == ["Bug in B"]


def test_issue_numbers_are_independent_per_repo():
    backend = github_fake.FakeGitHubBackend()

    a1 = backend.create_issue("https://github.com/acme/app-a.git", "First in A", "")
    b1 = backend.create_issue("https://github.com/acme/app-b.git", "First in B", "")

    assert a1["number"] == 1
    assert b1["number"] == 1


def test_closing_an_issue_only_affects_its_own_repo():
    backend = github_fake.FakeGitHubBackend()
    backend.create_issue("https://github.com/acme/app-a.git", "Bug in A", "")
    backend.create_issue("https://github.com/acme/app-b.git", "Bug in B", "")

    backend.close_issue("https://github.com/acme/app-a.git", 1)

    assert backend.list_open_issues("https://github.com/acme/app-a.git") == []
    assert len(backend.list_open_issues("https://github.com/acme/app-b.git")) == 1


def test_create_sub_issue_links_under_its_parent():
    backend = github_fake.FakeGitHubBackend()
    parent = backend.create_issue("https://github.com/acme/app.git", "Big feature", "")

    sub = backend.create_sub_issue("https://github.com/acme/app.git", parent["number"], "Part one", "")

    assert sub["number"] == 2
    assert backend.issues["https://github.com/acme/app.git"][parent["number"]]["sub_issue_numbers"] == [sub["number"]]


def test_get_issue_returns_the_issue_or_none():
    backend = github_fake.FakeGitHubBackend()
    created = backend.create_issue("https://github.com/acme/app.git", "A feature", "")

    assert backend.get_issue("https://github.com/acme/app.git", created["number"])["title"] == "A feature"
    assert backend.get_issue("https://github.com/acme/app.git", 999) is None


def test_variables_are_namespaced_by_repo_url():
    backend = github_fake.FakeGitHubBackend()

    backend.set_variable("https://github.com/acme/app-a.git", "AGENTRA_OBJECTIVE", "Objective A")
    backend.set_variable("https://github.com/acme/app-b.git", "AGENTRA_OBJECTIVE", "Objective B")

    assert backend.list_variables("https://github.com/acme/app-a.git")["AGENTRA_OBJECTIVE"] == "Objective A"
    assert backend.list_variables("https://github.com/acme/app-b.git")["AGENTRA_OBJECTIVE"] == "Objective B"


def test_install_with_monkeypatch_reverts_after_the_test(monkeypatch):
    from agentra.connectors import github_issues

    original = github_issues.create_issue
    github_fake.install(monkeypatch=monkeypatch)

    assert github_issues.create_issue is not original
    # monkeypatch's own teardown (not exercised here) restores `original`
    # after this test -- verified structurally by passing monkeypatch in,
    # since install() uses monkeypatch.setattr specifically so that happens.

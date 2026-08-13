"""Tests for connectors/github_issues.py -- Phase 1 of the known_bugs/
feature_queue -> GitHub Issues migration. No real GitHub API calls: httpx
is monkeypatched directly (this repo has no respx/httpx-mock dependency),
and github_app's token minting is stubbed so these tests don't need a real
GITHUB_APP_ID/GITHUB_APP_PRIVATE_KEY.
"""

from unittest.mock import MagicMock

import pytest

from agentra.connectors import github_app, github_issues


@pytest.fixture(autouse=True)
def _stub_token(monkeypatch):
    monkeypatch.setattr(github_app, "get_installation_token", lambda repo_url: "fake-installation-token")
    monkeypatch.setattr(github_issues, "get_installation_token", lambda repo_url: "fake-installation-token")


def _fake_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.side_effect = None
    return resp


def test_create_issue_posts_to_the_right_repo_with_labels(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _fake_response({"number": 42, "html_url": "https://github.com/acme/app/issues/42"})

    monkeypatch.setattr(github_issues.httpx, "post", fake_post)

    result = github_issues.create_issue(
        "https://github.com/acme/app.git", "Bug: pagination off by one", "Details here", labels=["bug"]
    )

    assert captured["url"] == "https://api.github.com/repos/acme/app/issues"
    assert captured["headers"]["Authorization"] == "token fake-installation-token"
    assert captured["json"] == {"title": "Bug: pagination off by one", "body": "Details here", "labels": ["bug"]}
    assert result["number"] == 42


def test_create_issue_rejects_non_github_url():
    with pytest.raises(github_issues.GitHubIssuesError):
        github_issues.create_issue("git@gitlab.com:acme/app.git", "title", "body")


def test_list_open_issues_filters_out_pull_requests(monkeypatch):
    def fake_get(url, headers, params, timeout):
        assert params["state"] == "open"
        return _fake_response(
            [
                {"number": 1, "title": "A real bug"},
                {"number": 2, "title": "A PR", "pull_request": {"url": "..."}},
            ]
        )

    monkeypatch.setattr(github_issues.httpx, "get", fake_get)

    issues = github_issues.list_open_issues("https://github.com/acme/app.git")

    assert [i["number"] for i in issues] == [1]


def test_list_open_issues_passes_label_filter(monkeypatch):
    captured = {}

    def fake_get(url, headers, params, timeout):
        captured["params"] = params
        return _fake_response([])

    monkeypatch.setattr(github_issues.httpx, "get", fake_get)

    github_issues.list_open_issues("https://github.com/acme/app.git", labels=["bug", "agentra"])

    assert captured["params"]["labels"] == "bug,agentra"


def test_close_issue_without_comment_only_patches_state(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(("post", args, kwargs))
        raise AssertionError("should not comment when comment=None")

    def fake_patch(url, headers, json, timeout):
        calls.append(("patch", url, json))
        return _fake_response({})

    monkeypatch.setattr(github_issues.httpx, "post", fake_post)
    monkeypatch.setattr(github_issues.httpx, "patch", fake_patch)

    github_issues.close_issue("https://github.com/acme/app.git", 42)

    assert calls == [("patch", "https://api.github.com/repos/acme/app/issues/42", {"state": "closed"})]


def test_close_issue_with_comment_posts_comment_then_closes(monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(("post", url, json))
        return _fake_response({})

    def fake_patch(url, headers, json, timeout):
        calls.append(("patch", url, json))
        return _fake_response({})

    monkeypatch.setattr(github_issues.httpx, "post", fake_post)
    monkeypatch.setattr(github_issues.httpx, "patch", fake_patch)

    github_issues.close_issue("https://github.com/acme/app.git", 42, comment="Fixed in abc123")

    assert calls == [
        ("post", "https://api.github.com/repos/acme/app/issues/42/comments", {"body": "Fixed in abc123"}),
        ("patch", "https://api.github.com/repos/acme/app/issues/42", {"state": "closed"}),
    ]


def test_close_issue_rejects_non_github_url():
    with pytest.raises(github_issues.GitHubIssuesError):
        github_issues.close_issue("git@gitlab.com:acme/app.git", 1)

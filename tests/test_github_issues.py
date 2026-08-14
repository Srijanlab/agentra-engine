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


def test_get_issue_returns_the_issue_json(monkeypatch):
    def fake_get(url, headers, timeout):
        assert url == "https://api.github.com/repos/acme/app/issues/7"
        return _fake_response({"number": 7, "title": "Parent feature"})

    monkeypatch.setattr(github_issues.httpx, "get", fake_get)

    result = github_issues.get_issue("https://github.com/acme/app.git", 7)

    assert result == {"number": 7, "title": "Parent feature"}


def test_get_issue_returns_none_on_404(monkeypatch):
    def fake_get(url, headers, timeout):
        return _fake_response({}, status_code=404)

    monkeypatch.setattr(github_issues.httpx, "get", fake_get)

    assert github_issues.get_issue("https://github.com/acme/app.git", 999) is None


def test_get_issue_rejects_non_github_url():
    with pytest.raises(github_issues.GitHubIssuesError):
        github_issues.get_issue("git@gitlab.com:acme/app.git", 1)


def test_add_comment_posts_without_changing_state(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update(url=url, json=json)
        return _fake_response({})

    def fail_patch(*a, **k):
        raise AssertionError("add_comment must not change issue state")

    monkeypatch.setattr(github_issues.httpx, "post", fake_post)
    monkeypatch.setattr(github_issues.httpx, "patch", fail_patch)

    github_issues.add_comment("https://github.com/acme/app.git", 7, "Still occurring.")

    assert captured["url"] == "https://api.github.com/repos/acme/app/issues/7/comments"
    assert captured["json"] == {"body": "Still occurring."}


def test_add_comment_rejects_non_github_url():
    with pytest.raises(github_issues.GitHubIssuesError):
        github_issues.add_comment("git@gitlab.com:acme/app.git", 1, "x")


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


def test_list_in_progress_features_filters_to_issues_with_sub_issues(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update(url=url, variables=json["variables"])
        resp = MagicMock()
        resp.raise_for_status.side_effect = None
        resp.json.return_value = {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": [
                            {
                                "number": 10,
                                "title": "Big feature",
                                "body": "Tracks a multi-part feature.",
                                "url": "https://github.com/acme/app/issues/10",
                                "subIssuesSummary": {"total": 3, "completed": 1},
                            },
                            {
                                "number": 11,
                                "title": "Not yet started",
                                "body": None,
                                "url": "https://github.com/acme/app/issues/11",
                                "subIssuesSummary": {"total": 0, "completed": 0},
                            },
                        ]
                    }
                }
            }
        }
        return resp

    monkeypatch.setattr(github_issues.httpx, "post", fake_post)

    result = github_issues.list_in_progress_features("https://github.com/acme/app.git", labels=["feature"])

    assert captured["url"] == "https://api.github.com/graphql"
    assert captured["variables"] == {"owner": "acme", "name": "app", "labels": ["feature"]}
    assert result == [
        {
            "number": 10,
            "title": "Big feature",
            "body": "Tracks a multi-part feature.",
            "html_url": "https://github.com/acme/app/issues/10",
            "sub_issues_total": 3,
            "sub_issues_completed": 1,
        }
    ]


def test_ensure_labels_creates_only_the_missing_ones(monkeypatch):
    created = []

    def fake_get(url, headers, params, timeout):
        assert url == "https://api.github.com/repos/acme/app/labels"
        return _fake_response([{"name": "bug"}, {"name": "story"}, {"name": "unrelated-label"}])

    def fake_post(url, headers, json, timeout):
        created.append(json["name"])
        return _fake_response({"name": json["name"]}, status_code=201)

    monkeypatch.setattr(github_issues.httpx, "get", fake_get)
    monkeypatch.setattr(github_issues.httpx, "post", fake_post)

    github_issues.ensure_labels("https://github.com/acme/app.git")

    assert sorted(created) == ["blocking_agentra", "feature", "need_human"]


def test_ensure_labels_tolerates_a_concurrent_create(monkeypatch):
    def fake_get(url, headers, params, timeout):
        return _fake_response([])

    def fake_post(url, headers, json, timeout):
        return _fake_response({"message": "already_exists"}, status_code=422)

    monkeypatch.setattr(github_issues.httpx, "get", fake_get)
    monkeypatch.setattr(github_issues.httpx, "post", fake_post)

    github_issues.ensure_labels("https://github.com/acme/app.git")  # must not raise


def test_add_labels_posts_the_given_labels(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update(url=url, json=json)
        return _fake_response({})

    monkeypatch.setattr(github_issues.httpx, "post", fake_post)

    github_issues.add_labels("https://github.com/acme/app.git", 7, ["need_human", "blocking_agentra"])

    assert captured["url"] == "https://api.github.com/repos/acme/app/issues/7/labels"
    assert captured["json"] == {"labels": ["need_human", "blocking_agentra"]}


def test_add_labels_rejects_non_github_url():
    with pytest.raises(github_issues.GitHubIssuesError):
        github_issues.add_labels("git@gitlab.com:acme/app.git", 1, ["bug"])


def test_create_sub_issue_creates_and_links_under_parent(monkeypatch):
    posts = []

    def fake_post(url, headers, json, timeout):
        posts.append((url, json))
        if url == "https://api.github.com/repos/acme/app/issues":
            return _fake_response(
                {"number": 43, "node_id": "SUB_NODE", "html_url": "https://github.com/acme/app/issues/43"}
            )
        if url == "https://api.github.com/graphql":
            assert json["variables"] == {"issueId": "PARENT_NODE", "subIssueId": "SUB_NODE"}
            return _fake_response({"data": {"addSubIssue": {"subIssue": {"id": "SUB_NODE"}}}})
        raise AssertionError(f"unexpected POST to {url}")

    def fake_get(url, headers, timeout):
        assert url == "https://api.github.com/repos/acme/app/issues/10"
        return _fake_response({"node_id": "PARENT_NODE"})

    monkeypatch.setattr(github_issues.httpx, "post", fake_post)
    monkeypatch.setattr(github_issues.httpx, "get", fake_get)

    result = github_issues.create_sub_issue("https://github.com/acme/app.git", 10, "Sub task", "details")

    assert result["number"] == 43
    assert len(posts) == 2


def test_create_sub_issue_still_returns_the_issue_if_linking_fails(monkeypatch):
    def fake_post(url, headers, json, timeout):
        if url == "https://api.github.com/repos/acme/app/issues":
            return _fake_response({"number": 43, "node_id": "SUB_NODE"})
        raise AssertionError(f"unexpected POST to {url}")

    def fake_get(url, headers, timeout):
        raise RuntimeError("network down")

    monkeypatch.setattr(github_issues.httpx, "post", fake_post)
    monkeypatch.setattr(github_issues.httpx, "get", fake_get)

    result = github_issues.create_sub_issue("https://github.com/acme/app.git", 10, "Sub task", "details")

    assert result["number"] == 43


def test_create_sub_issue_still_returns_the_issue_on_a_graphql_error(monkeypatch):
    def fake_post(url, headers, json, timeout):
        if url == "https://api.github.com/repos/acme/app/issues":
            return _fake_response({"number": 43, "node_id": "SUB_NODE"})
        if url == "https://api.github.com/graphql":
            return _fake_response({"errors": [{"message": "not accessible"}]})
        raise AssertionError(f"unexpected POST to {url}")

    def fake_get(url, headers, timeout):
        return _fake_response({"node_id": "PARENT_NODE"})

    monkeypatch.setattr(github_issues.httpx, "post", fake_post)
    monkeypatch.setattr(github_issues.httpx, "get", fake_get)

    result = github_issues.create_sub_issue("https://github.com/acme/app.git", 10, "Sub task", "details")

    assert result["number"] == 43


def test_list_comments_returns_the_comments_json(monkeypatch):
    def fake_get(url, headers, timeout):
        assert url == "https://api.github.com/repos/acme/app/issues/7/comments"
        return _fake_response([{"body": "first"}, {"body": "second"}])

    monkeypatch.setattr(github_issues.httpx, "get", fake_get)

    result = github_issues.list_comments("https://github.com/acme/app.git", 7)

    assert result == [{"body": "first"}, {"body": "second"}]


def test_record_in_progress_branch_posts_the_marker_as_a_comment(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update(url=url, json=json)
        return _fake_response({})

    monkeypatch.setattr(github_issues.httpx, "post", fake_post)

    github_issues.record_in_progress_branch("https://github.com/acme/app.git", 13, "dev/abc123-fix-sort-order")

    assert captured["url"] == "https://api.github.com/repos/acme/app/issues/13/comments"
    assert captured["json"] == {"body": "In-Progress-Branch: dev/abc123-fix-sort-order"}


def test_get_in_progress_branch_returns_the_most_recent_marker(monkeypatch):
    def fake_get(url, headers, timeout):
        return _fake_response(
            [
                {"body": "Still occurring (run r1)."},
                {"body": "In-Progress-Branch: dev/first-attempt"},
                {"body": "just chatting"},
                {"body": "In-Progress-Branch: dev/second-attempt"},
            ]
        )

    monkeypatch.setattr(github_issues.httpx, "get", fake_get)

    result = github_issues.get_in_progress_branch("https://github.com/acme/app.git", 13)

    assert result == "dev/second-attempt"


def test_get_in_progress_branch_returns_none_without_a_marker(monkeypatch):
    def fake_get(url, headers, timeout):
        return _fake_response([{"body": "unrelated comment"}])

    monkeypatch.setattr(github_issues.httpx, "get", fake_get)

    assert github_issues.get_in_progress_branch("https://github.com/acme/app.git", 13) is None

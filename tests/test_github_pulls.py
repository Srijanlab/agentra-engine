"""Tests for connectors/github_pulls.py -- the Pull Requests API helper backing
the "external" deploy_strategy's promotion (agents/deployment.py's
_promote_prod_external). No real GitHub API calls: httpx is monkeypatched
directly (same convention as test_github_issues.py), and github_app's token
minting is stubbed.
"""

from unittest.mock import MagicMock

import pytest

from agentra.connectors import github_app, github_pulls


@pytest.fixture(autouse=True)
def _stub_token(monkeypatch):
    monkeypatch.setattr(github_app, "get_installation_token", lambda repo_url: "fake-installation-token")
    monkeypatch.setattr(github_pulls, "get_installation_token", lambda repo_url: "fake-installation-token")


def _fake_response(json_data, status_code=200, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = text
    resp.raise_for_status.side_effect = None
    return resp


def test_no_open_pr_creates_then_merges(monkeypatch):
    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append(("get", url, params))
        return _fake_response([])  # no open PR yet

    def fake_post(url, headers, json, timeout):
        calls.append(("post", url, json))
        return _fake_response({"number": 7, "html_url": "https://github.com/acme/loop/pull/7"})

    def fake_put(url, headers, json, timeout):
        calls.append(("put", url, json))
        return _fake_response({"merged": True})

    monkeypatch.setattr(github_pulls.httpx, "get", fake_get)
    monkeypatch.setattr(github_pulls.httpx, "post", fake_post)
    monkeypatch.setattr(github_pulls.httpx, "put", fake_put)

    result = github_pulls.open_or_merge_promotion_pr(
        "https://github.com/acme/loop.git", "beta", "main", title="Promote beta to main"
    )

    assert "Merged PR #7" in result
    assert calls[0][0] == "get"
    assert calls[1] == ("post", "https://api.github.com/repos/acme/loop/pulls", {"title": "Promote beta to main", "head": "beta", "base": "main"})
    assert calls[2] == ("put", "https://api.github.com/repos/acme/loop/pulls/7/merge", {"merge_method": "merge"})


def test_an_existing_open_pr_is_merged_directly_without_creating_a_new_one(monkeypatch):
    def fake_get(url, headers, params, timeout):
        return _fake_response([{"number": 3, "html_url": "https://github.com/acme/loop/pull/3"}])

    def fail_post(*a, **k):
        raise AssertionError("must not create a new PR when one is already open")

    def fake_put(url, headers, json, timeout):
        return _fake_response({"merged": True})

    monkeypatch.setattr(github_pulls.httpx, "get", fake_get)
    monkeypatch.setattr(github_pulls.httpx, "post", fail_post)
    monkeypatch.setattr(github_pulls.httpx, "put", fake_put)

    result = github_pulls.open_or_merge_promotion_pr(
        "https://github.com/acme/loop.git", "beta", "main", title="Promote beta to main"
    )

    assert "Merged PR #3" in result


def test_nothing_to_promote_when_branches_are_already_equal(monkeypatch):
    def fake_get(url, headers, params, timeout):
        return _fake_response([])

    def fake_post(url, headers, json, timeout):
        return _fake_response({"errors": ["No commits between main and beta"]}, status_code=422, text="No commits between main and beta")

    def fail_put(*a, **k):
        raise AssertionError("must not attempt a merge when there's nothing to promote")

    monkeypatch.setattr(github_pulls.httpx, "get", fake_get)
    monkeypatch.setattr(github_pulls.httpx, "post", fake_post)
    monkeypatch.setattr(github_pulls.httpx, "put", fail_put)

    result = github_pulls.open_or_merge_promotion_pr(
        "https://github.com/acme/loop.git", "beta", "main", title="Promote beta to main"
    )

    assert "Nothing to promote" in result


def test_a_not_yet_mergeable_pr_is_reported_not_raised(monkeypatch):
    def fake_get(url, headers, params, timeout):
        return _fake_response([{"number": 7, "html_url": "https://github.com/acme/loop/pull/7"}])

    def fake_put(url, headers, json, timeout):
        return _fake_response({"message": "not mergeable"}, status_code=405)

    monkeypatch.setattr(github_pulls.httpx, "get", fake_get)
    monkeypatch.setattr(github_pulls.httpx, "put", fake_put)

    result = github_pulls.open_or_merge_promotion_pr(
        "https://github.com/acme/loop.git", "beta", "main", title="Promote beta to main"
    )

    assert "not mergeable yet" in result


def test_non_github_url_raises(monkeypatch):
    with pytest.raises(github_pulls.GitHubPullsError):
        github_pulls.open_or_merge_promotion_pr("git@bitbucket.org:acme/loop.git", "beta", "main", title="x")

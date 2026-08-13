"""Tests for connectors/github_variables.py -- the client for GitHub
Actions repository Variables that environments.py/memory.py's objective
sync build on top of. No real GitHub API calls: httpx is monkeypatched
directly, and token minting is stubbed.
"""

from unittest.mock import MagicMock

import pytest

from agentra.connectors import github_variables


@pytest.fixture(autouse=True)
def _stub_token(monkeypatch):
    monkeypatch.setattr(github_variables, "get_installation_token", lambda repo_url: "fake-installation-token")


def _fake_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.side_effect = None
    return resp


def test_list_variables_returns_name_to_value_mapping(monkeypatch):
    def fake_get(url, headers, params, timeout):
        assert url == "https://api.github.com/repos/acme/app/actions/variables"
        assert headers["Authorization"] == "token fake-installation-token"
        return _fake_response(
            {"variables": [{"name": "AGENTRA_SCHEDULE_HOURS", "value": "24.0"}, {"name": "AGENTRA_VERCEL", "value": "true"}]}
        )

    monkeypatch.setattr(github_variables.httpx, "get", fake_get)

    result = github_variables.list_variables("https://github.com/acme/app.git")

    assert result == {"AGENTRA_SCHEDULE_HOURS": "24.0", "AGENTRA_VERCEL": "true"}


def test_list_variables_paginates(monkeypatch):
    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append(params["page"])
        if params["page"] == 1:
            return _fake_response({"variables": [{"name": f"VAR_{i}", "value": str(i)} for i in range(100)]})
        return _fake_response({"variables": [{"name": "VAR_LAST", "value": "last"}]})

    monkeypatch.setattr(github_variables.httpx, "get", fake_get)

    result = github_variables.list_variables("https://github.com/acme/app.git")

    assert calls == [1, 2]
    assert len(result) == 101
    assert result["VAR_LAST"] == "last"


def test_list_variables_rejects_non_github_url():
    with pytest.raises(github_variables.GitHubVariablesError):
        github_variables.list_variables("git@gitlab.com:acme/app.git")


def test_set_variable_updates_existing_variable_via_patch(monkeypatch):
    captured = {}

    def fake_patch(url, headers, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _fake_response({})

    def fail_post(*a, **k):
        raise AssertionError("should not POST when PATCH succeeds")

    monkeypatch.setattr(github_variables.httpx, "patch", fake_patch)
    monkeypatch.setattr(github_variables.httpx, "post", fail_post)

    github_variables.set_variable("https://github.com/acme/app.git", "AGENTRA_SCHEDULE_HOURS", "12.0")

    assert captured["url"] == "https://api.github.com/repos/acme/app/actions/variables/AGENTRA_SCHEDULE_HOURS"
    assert captured["json"] == {"name": "AGENTRA_SCHEDULE_HOURS", "value": "12.0"}


def test_set_variable_creates_via_post_when_patch_404s(monkeypatch):
    captured = {}

    def fake_patch(url, headers, json, timeout):
        return _fake_response({}, status_code=404)

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _fake_response({}, status_code=201)

    monkeypatch.setattr(github_variables.httpx, "patch", fake_patch)
    monkeypatch.setattr(github_variables.httpx, "post", fake_post)

    github_variables.set_variable("https://github.com/acme/app.git", "AGENTRA_NEW_VAR", "hello")

    assert captured["url"] == "https://api.github.com/repos/acme/app/actions/variables"
    assert captured["json"] == {"name": "AGENTRA_NEW_VAR", "value": "hello"}


def test_set_variable_rejects_non_github_url():
    with pytest.raises(github_variables.GitHubVariablesError):
        github_variables.set_variable("git@gitlab.com:acme/app.git", "X", "1")

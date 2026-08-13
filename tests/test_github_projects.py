"""Tests for connectors/github_projects.py -- the GraphQL client and
provisioning logic behind "feature mapped to project, bug mapped to
issue" (see memory.py's record_feature_request/record_shipped). No real
GitHub API calls: httpx and token minting are stubbed, same pattern as
test_github_variables.py/test_memory_github_backlog.py.
"""

from unittest.mock import MagicMock

import pytest

from agentra.connectors import github_projects


@pytest.fixture(autouse=True)
def _stub_token(monkeypatch):
    monkeypatch.setattr(github_projects, "get_installation_token", lambda repo_url: "fake-installation-token")


# ── _graphql: the one HTTP choke point every function above it goes through ──


def test_graphql_posts_bearer_auth_and_returns_data(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json)
        resp = MagicMock()
        resp.raise_for_status.side_effect = None
        resp.json.return_value = {"data": {"hello": "world"}}
        return resp

    monkeypatch.setattr(github_projects.httpx, "post", fake_post)

    result = github_projects._graphql("https://github.com/acme/app.git", "query { x }", {"a": 1})

    assert result == {"hello": "world"}
    assert captured["url"] == "https://api.github.com/graphql"
    assert captured["headers"]["Authorization"] == "Bearer fake-installation-token"
    assert captured["json"] == {"query": "query { x }", "variables": {"a": 1}}


def test_graphql_raises_on_a_graphql_level_errors_array(monkeypatch):
    def fake_post(url, headers, json, timeout):
        resp = MagicMock()
        resp.raise_for_status.side_effect = None
        resp.json.return_value = {"errors": [{"message": "Resource not accessible by integration"}], "data": None}
        return resp

    monkeypatch.setattr(github_projects.httpx, "post", fake_post)

    with pytest.raises(github_projects.GitHubProjectsError):
        github_projects._graphql("https://github.com/acme/app.git", "query { x }", {})


# ── ensure_project: provision-once, cache-as-variables ────────────────────


def test_ensure_project_returns_none_without_a_github_remote():
    assert github_projects.ensure_project("git@gitlab.com:acme/app.git") is None


def test_ensure_project_returns_none_when_github_variables_unreachable(monkeypatch):
    monkeypatch.setattr(
        github_projects.github_variables, "list_variables", lambda repo_url: (_ for _ in ()).throw(RuntimeError("down"))
    )

    assert github_projects.ensure_project("https://github.com/acme/app.git") is None


def test_ensure_project_returns_cached_values_without_any_graphql_calls(monkeypatch):
    monkeypatch.setattr(
        github_projects.github_variables,
        "list_variables",
        lambda repo_url: {
            "AGENTRA_PROJECT_ID": "PVT_1",
            "AGENTRA_PROJECT_STATUS_FIELD_ID": "FIELD_1",
            "AGENTRA_PROJECT_STATUS_OPTION_TODO": "OPT_TODO",
            "AGENTRA_PROJECT_STATUS_OPTION_IN_PROGRESS": "OPT_PROG",
            "AGENTRA_PROJECT_STATUS_OPTION_DONE": "OPT_DONE",
        },
    )
    monkeypatch.setattr(
        github_projects, "_graphql", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GraphQL"))
    )

    result = github_projects.ensure_project("https://github.com/acme/app.git")

    assert result == {
        "project_id": "PVT_1",
        "status_field_id": "FIELD_1",
        "status_options": {"Todo": "OPT_TODO", "In Progress": "OPT_PROG", "Done": "OPT_DONE"},
    }


def test_ensure_project_provisions_a_fresh_project_and_caches_ids_as_variables(monkeypatch):
    monkeypatch.setattr(github_projects.github_variables, "list_variables", lambda repo_url: {})
    set_calls: dict[str, str] = {}
    monkeypatch.setattr(
        github_projects.github_variables,
        "set_variable",
        lambda repo_url, name, value: set_calls.__setitem__(name, value),
    )

    def fake_graphql(repo_url, query, variables):
        if "owner { id }" in query:
            return {"repository": {"id": "REPO_1", "owner": {"id": "OWNER_1"}}}
        if "createProjectV2(input:" in query:
            assert variables == {"ownerId": "OWNER_1", "title": github_projects._PROJECT_TITLE}
            return {
                "createProjectV2": {
                    "projectV2": {"id": "PVT_1", "number": 7, "url": "https://github.com/orgs/acme/projects/7"}
                }
            }
        if "linkProjectV2ToRepository" in query:
            assert variables == {"projectId": "PVT_1", "repositoryId": "REPO_1"}
            return {"linkProjectV2ToRepository": {"repository": {"id": "REPO_1"}}}
        if "createProjectV2Field" in query:
            return {
                "createProjectV2Field": {
                    "projectV2Field": {
                        "id": "FIELD_1",
                        "options": [
                            {"id": "OPT_TODO", "name": "Todo"},
                            {"id": "OPT_PROG", "name": "In Progress"},
                            {"id": "OPT_DONE", "name": "Done"},
                        ],
                    }
                }
            }
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(github_projects, "_graphql", fake_graphql)

    result = github_projects.ensure_project("https://github.com/acme/app.git")

    assert result == {
        "project_id": "PVT_1",
        "status_field_id": "FIELD_1",
        "status_options": {"Todo": "OPT_TODO", "In Progress": "OPT_PROG", "Done": "OPT_DONE"},
    }
    assert set_calls == {
        "AGENTRA_PROJECT_ID": "PVT_1",
        "AGENTRA_PROJECT_NUMBER": "7",
        "AGENTRA_PROJECT_URL": "https://github.com/orgs/acme/projects/7",
        "AGENTRA_PROJECT_STATUS_FIELD_ID": "FIELD_1",
        "AGENTRA_PROJECT_STATUS_OPTION_TODO": "OPT_TODO",
        "AGENTRA_PROJECT_STATUS_OPTION_IN_PROGRESS": "OPT_PROG",
        "AGENTRA_PROJECT_STATUS_OPTION_DONE": "OPT_DONE",
    }


def test_ensure_project_returns_none_on_graphql_error(monkeypatch):
    monkeypatch.setattr(github_projects.github_variables, "list_variables", lambda repo_url: {})
    monkeypatch.setattr(
        github_projects, "_graphql", lambda *a, **k: (_ for _ in ()).throw(github_projects.GitHubProjectsError("boom"))
    )

    assert github_projects.ensure_project("https://github.com/acme/app.git") is None


def test_ensure_project_ignores_a_failed_repository_link(monkeypatch):
    """linkProjectV2ToRepository is cosmetic (shows the board under the
    repo's Projects tab) -- a failure there must not sink provisioning."""
    monkeypatch.setattr(github_projects.github_variables, "list_variables", lambda repo_url: {})
    monkeypatch.setattr(github_projects.github_variables, "set_variable", lambda *a, **k: None)

    def fake_graphql(repo_url, query, variables):
        if "owner { id }" in query:
            return {"repository": {"id": "REPO_1", "owner": {"id": "OWNER_1"}}}
        if "createProjectV2(input:" in query:
            return {"createProjectV2": {"projectV2": {"id": "PVT_1", "number": 7, "url": "https://x/7"}}}
        if "linkProjectV2ToRepository" in query:
            raise github_projects.GitHubProjectsError("no repo link permission")
        if "createProjectV2Field" in query:
            return {
                "createProjectV2Field": {
                    "projectV2Field": {"id": "FIELD_1", "options": [{"id": "OPT_TODO", "name": "Todo"}]}
                }
            }
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(github_projects, "_graphql", fake_graphql)

    result = github_projects.ensure_project("https://github.com/acme/app.git")

    assert result["project_id"] == "PVT_1"


# ── add_item_to_project: idempotent add + status set ───────────────────────


def test_add_item_to_project_adds_the_issue_and_sets_its_status(monkeypatch):
    monkeypatch.setattr(
        github_projects,
        "ensure_project",
        lambda repo_url: {
            "project_id": "PVT_1",
            "status_field_id": "FIELD_1",
            "status_options": {"Todo": "OPT_TODO", "In Progress": "OPT_PROG", "Done": "OPT_DONE"},
        },
    )
    monkeypatch.setattr(github_projects, "_issue_node_id", lambda repo_url, issue_number: "ISSUE_NODE_1")

    calls = []

    def fake_graphql(repo_url, query, variables):
        calls.append((query, variables))
        if "addProjectV2ItemById" in query:
            return {"addProjectV2ItemById": {"item": {"id": "ITEM_1"}}}
        if "updateProjectV2ItemFieldValue" in query:
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_1"}}}
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(github_projects, "_graphql", fake_graphql)

    github_projects.add_item_to_project("https://github.com/acme/app.git", 42, status="Done")

    assert calls[0][1] == {"projectId": "PVT_1", "contentId": "ISSUE_NODE_1"}
    assert calls[1][1] == {"projectId": "PVT_1", "itemId": "ITEM_1", "fieldId": "FIELD_1", "optionId": "OPT_DONE"}


def test_add_item_to_project_noops_when_ensure_project_fails(monkeypatch):
    monkeypatch.setattr(github_projects, "ensure_project", lambda repo_url: None)
    monkeypatch.setattr(github_projects, "_graphql", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GraphQL")))

    github_projects.add_item_to_project("https://github.com/acme/app.git", 42)  # must not raise


def test_add_item_to_project_noops_on_an_unknown_status(monkeypatch):
    monkeypatch.setattr(
        github_projects, "ensure_project", lambda repo_url: {"project_id": "PVT_1", "status_field_id": "FIELD_1", "status_options": {"Todo": "OPT_TODO"}}
    )
    monkeypatch.setattr(github_projects, "_graphql", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GraphQL")))

    github_projects.add_item_to_project("https://github.com/acme/app.git", 42, status="Bogus")


def test_add_item_to_project_noops_when_the_issue_has_no_node_id(monkeypatch):
    monkeypatch.setattr(
        github_projects, "ensure_project", lambda repo_url: {"project_id": "PVT_1", "status_field_id": "FIELD_1", "status_options": {"Todo": "OPT_TODO"}}
    )
    monkeypatch.setattr(github_projects, "_issue_node_id", lambda repo_url, issue_number: None)
    monkeypatch.setattr(github_projects, "_graphql", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GraphQL")))

    github_projects.add_item_to_project("https://github.com/acme/app.git", 42, status="Todo")


def test_add_item_to_project_never_raises_on_an_unexpected_failure(monkeypatch):
    monkeypatch.setattr(github_projects, "ensure_project", lambda repo_url: (_ for _ in ()).throw(RuntimeError("boom")))

    github_projects.add_item_to_project("https://github.com/acme/app.git", 42)  # must not raise

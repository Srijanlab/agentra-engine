"""Tests for connectors/github_projects.py -- the GraphQL client and
provisioning logic behind "feature mapped to project, bug mapped to
issue" (see memory.py's record_feature_request/record_shipped). One
Project per FEATURE (titled after that feature), not per repo/app -- no
local cache or GitHub Variables involved, a feature issue's Project
association is asked from GitHub directly every time (issue.projectItems).
No real GitHub API calls: httpx and token minting are stubbed, same
pattern as test_github_variables.py/test_memory_github_backlog.py.
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


# ── ensure_feature_project: provision-once-per-feature, no local cache ─────


def _existing_project_response(project):
    return {"repository": {"issue": {"projectItems": {"nodes": [{"project": project}]}}}}


def _no_project_response():
    return {"repository": {"issue": {"projectItems": {"nodes": []}}}}


def test_ensure_feature_project_returns_none_without_a_github_remote():
    assert github_projects.ensure_feature_project("git@gitlab.com:acme/app.git", 10, "My feature") is None


def test_ensure_feature_project_returns_none_when_graphql_unreachable(monkeypatch):
    monkeypatch.setattr(
        github_projects, "_graphql", lambda *a, **k: (_ for _ in ()).throw(github_projects.GitHubProjectsError("down"))
    )

    assert github_projects.ensure_feature_project("https://github.com/acme/app.git", 10, "My feature") is None


def test_ensure_feature_project_returns_the_existing_project_without_creating_a_new_one(monkeypatch):
    def fake_graphql(repo_url, query, variables):
        if "projectItems" in query:
            assert variables == {"owner": "acme", "name": "app", "number": 10}
            return _existing_project_response(
                {
                    "id": "PVT_1",
                    "url": "https://github.com/orgs/acme/projects/9",
                    "fields": {
                        "nodes": [
                            {"id": "FIELD_1", "name": "Status", "options": [{"id": "OPT_TODO", "name": "Todo"}]},
                        ]
                    },
                }
            )
        raise AssertionError(f"must not call GraphQL again: {query}")

    monkeypatch.setattr(github_projects, "_graphql", fake_graphql)

    result = github_projects.ensure_feature_project("https://github.com/acme/app.git", 10, "My feature")

    assert result == {
        "project_id": "PVT_1",
        "url": "https://github.com/orgs/acme/projects/9",
        "status_field_id": "FIELD_1",
        "status_options": {"Todo": "OPT_TODO"},
    }


def test_ensure_feature_project_provisions_a_fresh_project_titled_after_the_feature(monkeypatch):
    """A freshly created ProjectV2 already comes with its own default
    Status field (Todo/In Progress/Done) -- confirmed live against a real
    org, createProjectV2Field's own attempt to add a second one fails
    outright ("Name has already been taken"). ensure_feature_project must
    read that existing field back, not try to create one."""

    def fake_graphql(repo_url, query, variables):
        if "projectItems" in query:
            return _no_project_response()
        if "owner { id }" in query:
            return {"repository": {"id": "REPO_1", "owner": {"id": "OWNER_1"}}}
        if "createProjectV2(input:" in query:
            assert variables == {"ownerId": "OWNER_1", "title": "My feature"}
            return {
                "createProjectV2": {
                    "projectV2": {"id": "PVT_1", "number": 7, "url": "https://github.com/orgs/acme/projects/7"}
                }
            }
        if "linkProjectV2ToRepository" in query:
            assert variables == {"projectId": "PVT_1", "repositoryId": "REPO_1"}
            return {"linkProjectV2ToRepository": {"repository": {"id": "REPO_1"}}}
        if "node(id: $projectId)" in query:
            assert variables == {"projectId": "PVT_1"}
            return {
                "node": {
                    "fields": {
                        "nodes": [
                            {"id": "PVTF_TITLE"},  # a ProjectV2FieldCommon field has no "options" key at all
                            {
                                "id": "FIELD_1",
                                "name": "Status",
                                "options": [
                                    {"id": "OPT_TODO", "name": "Todo"},
                                    {"id": "OPT_PROG", "name": "In Progress"},
                                    {"id": "OPT_DONE", "name": "Done"},
                                ],
                            },
                        ]
                    }
                }
            }
        if "createProjectV2Field" in query:
            raise AssertionError("must not try to create a Status field when one already exists")
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(github_projects, "_graphql", fake_graphql)

    result = github_projects.ensure_feature_project("https://github.com/acme/app.git", 10, "My feature")

    assert result == {
        "project_id": "PVT_1",
        "url": "https://github.com/orgs/acme/projects/7",
        "status_field_id": "FIELD_1",
        "status_options": {"Todo": "OPT_TODO", "In Progress": "OPT_PROG", "Done": "OPT_DONE"},
    }


def test_ensure_feature_project_falls_back_to_creating_a_status_field_if_none_exists(monkeypatch):
    def fake_graphql(repo_url, query, variables):
        if "projectItems" in query:
            return _no_project_response()
        if "owner { id }" in query:
            return {"repository": {"id": "REPO_1", "owner": {"id": "OWNER_1"}}}
        if "createProjectV2(input:" in query:
            return {"createProjectV2": {"projectV2": {"id": "PVT_1", "number": 7, "url": "https://x/7"}}}
        if "linkProjectV2ToRepository" in query:
            return {"linkProjectV2ToRepository": {"repository": {"id": "REPO_1"}}}
        if "node(id: $projectId)" in query:
            return {"node": {"fields": {"nodes": [{"id": "PVTF_TITLE"}]}}}  # no Status field present
        if "createProjectV2Field" in query:
            return {
                "createProjectV2Field": {
                    "projectV2Field": {"id": "FIELD_1", "options": [{"id": "OPT_TODO", "name": "Todo"}]}
                }
            }
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(github_projects, "_graphql", fake_graphql)

    result = github_projects.ensure_feature_project("https://github.com/acme/app.git", 10, "My feature")

    assert result["status_field_id"] == "FIELD_1"


def test_ensure_feature_project_returns_none_on_graphql_error_during_creation(monkeypatch):
    def fake_graphql(repo_url, query, variables):
        if "projectItems" in query:
            return _no_project_response()
        raise github_projects.GitHubProjectsError("boom")

    monkeypatch.setattr(github_projects, "_graphql", fake_graphql)

    assert github_projects.ensure_feature_project("https://github.com/acme/app.git", 10, "My feature") is None


def test_ensure_feature_project_ignores_a_failed_repository_link(monkeypatch):
    """linkProjectV2ToRepository is cosmetic (shows the board under the
    repo's Projects tab) -- a failure there must not sink provisioning."""

    def fake_graphql(repo_url, query, variables):
        if "projectItems" in query:
            return _no_project_response()
        if "owner { id }" in query:
            return {"repository": {"id": "REPO_1", "owner": {"id": "OWNER_1"}}}
        if "createProjectV2(input:" in query:
            return {"createProjectV2": {"projectV2": {"id": "PVT_1", "number": 7, "url": "https://x/7"}}}
        if "linkProjectV2ToRepository" in query:
            raise github_projects.GitHubProjectsError("no repo link permission")
        if "node(id: $projectId)" in query:
            return {"node": {"fields": {"nodes": [{"id": "PVTF_TITLE"}]}}}
        if "createProjectV2Field" in query:
            return {
                "createProjectV2Field": {
                    "projectV2Field": {"id": "FIELD_1", "options": [{"id": "OPT_TODO", "name": "Todo"}]}
                }
            }
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(github_projects, "_graphql", fake_graphql)

    result = github_projects.ensure_feature_project("https://github.com/acme/app.git", 10, "My feature")

    assert result["project_id"] == "PVT_1"


# ── add_item_to_feature_project: idempotent add + status set ───────────────


def test_add_item_to_feature_project_adds_the_feature_issue_itself_by_default(monkeypatch):
    monkeypatch.setattr(
        github_projects,
        "ensure_feature_project",
        lambda repo_url, feature_issue_number, title: {
            "project_id": "PVT_1",
            "status_field_id": "FIELD_1",
            "status_options": {"Todo": "OPT_TODO", "In Progress": "OPT_PROG", "Done": "OPT_DONE"},
        },
    )
    monkeypatch.setattr(github_projects, "_issue_node_id", lambda repo_url, issue_number: f"NODE_{issue_number}")

    calls = []

    def fake_graphql(repo_url, query, variables):
        calls.append((query, variables))
        if "addProjectV2ItemById" in query:
            return {"addProjectV2ItemById": {"item": {"id": "ITEM_1"}}}
        if "updateProjectV2ItemFieldValue" in query:
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_1"}}}
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(github_projects, "_graphql", fake_graphql)

    github_projects.add_item_to_feature_project("https://github.com/acme/app.git", 10, "My feature", status="Done")

    assert calls[0][1] == {"projectId": "PVT_1", "contentId": "NODE_10"}
    assert calls[1][1] == {"projectId": "PVT_1", "itemId": "ITEM_1", "fieldId": "FIELD_1", "optionId": "OPT_DONE"}


def test_add_item_to_feature_project_adds_a_sub_issue_onto_the_same_board(monkeypatch):
    monkeypatch.setattr(
        github_projects,
        "ensure_feature_project",
        lambda repo_url, feature_issue_number, title: {
            "project_id": "PVT_1",
            "status_field_id": "FIELD_1",
            "status_options": {"Todo": "OPT_TODO"},
        },
    )
    monkeypatch.setattr(github_projects, "_issue_node_id", lambda repo_url, issue_number: f"NODE_{issue_number}")

    calls = []

    def fake_graphql(repo_url, query, variables):
        calls.append((query, variables))
        if "addProjectV2ItemById" in query:
            return {"addProjectV2ItemById": {"item": {"id": "ITEM_SUB"}}}
        if "updateProjectV2ItemFieldValue" in query:
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_SUB"}}}
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(github_projects, "_graphql", fake_graphql)

    github_projects.add_item_to_feature_project(
        "https://github.com/acme/app.git", 10, "My feature", issue_number=11, status="Todo"
    )

    assert calls[0][1] == {"projectId": "PVT_1", "contentId": "NODE_11"}


def test_add_item_to_feature_project_noops_when_ensure_fails(monkeypatch):
    monkeypatch.setattr(github_projects, "ensure_feature_project", lambda repo_url, feature_issue_number, title: None)
    monkeypatch.setattr(github_projects, "_graphql", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GraphQL")))

    github_projects.add_item_to_feature_project("https://github.com/acme/app.git", 10, "My feature")  # must not raise


def test_add_item_to_feature_project_noops_on_an_unknown_status(monkeypatch):
    monkeypatch.setattr(
        github_projects,
        "ensure_feature_project",
        lambda repo_url, feature_issue_number, title: {"project_id": "PVT_1", "status_field_id": "FIELD_1", "status_options": {"Todo": "OPT_TODO"}},
    )
    monkeypatch.setattr(github_projects, "_graphql", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GraphQL")))

    github_projects.add_item_to_feature_project("https://github.com/acme/app.git", 10, "My feature", status="Bogus")


def test_add_item_to_feature_project_noops_when_the_issue_has_no_node_id(monkeypatch):
    monkeypatch.setattr(
        github_projects,
        "ensure_feature_project",
        lambda repo_url, feature_issue_number, title: {"project_id": "PVT_1", "status_field_id": "FIELD_1", "status_options": {"Todo": "OPT_TODO"}},
    )
    monkeypatch.setattr(github_projects, "_issue_node_id", lambda repo_url, issue_number: None)
    monkeypatch.setattr(github_projects, "_graphql", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GraphQL")))

    github_projects.add_item_to_feature_project("https://github.com/acme/app.git", 10, "My feature", status="Todo")


def test_add_item_to_feature_project_never_raises_on_an_unexpected_failure(monkeypatch):
    monkeypatch.setattr(
        github_projects, "ensure_feature_project", lambda repo_url, feature_issue_number, title: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    github_projects.add_item_to_feature_project("https://github.com/acme/app.git", 10, "My feature")  # must not raise

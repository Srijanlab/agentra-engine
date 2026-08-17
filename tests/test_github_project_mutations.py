"""Tests for connectors/github_project_mutations.py -- add_item_to_feature_project,
the one write path onto a feature's Project board (see memory.py's
record_shipped path). Split out of github_projects.py's read/provisioning
layer; see test_github_projects.py for ensure_feature_project etc.
"""

from agentra.connectors import github_project_mutations


def test_add_item_to_feature_project_adds_the_feature_issue_itself_by_default(monkeypatch):
    monkeypatch.setattr(
        github_project_mutations,
        "ensure_feature_project",
        lambda repo_url, feature_issue_number, title: {
            "project_id": "PVT_1",
            "status_field_id": "FIELD_1",
            "status_options": {"Todo": "OPT_TODO", "In Progress": "OPT_PROG", "Done": "OPT_DONE"},
        },
    )
    monkeypatch.setattr(github_project_mutations, "_issue_node_id", lambda repo_url, issue_number: f"NODE_{issue_number}")

    calls = []

    def fake_graphql(repo_url, query, variables):
        calls.append((query, variables))
        if "addProjectV2ItemById" in query:
            return {"addProjectV2ItemById": {"item": {"id": "ITEM_1"}}}
        if "updateProjectV2ItemFieldValue" in query:
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_1"}}}
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(github_project_mutations, "_graphql", fake_graphql)

    github_project_mutations.add_item_to_feature_project("https://github.com/acme/app.git", 10, "My feature", status="Done")

    assert calls[0][1] == {"projectId": "PVT_1", "contentId": "NODE_10"}
    assert calls[1][1] == {"projectId": "PVT_1", "itemId": "ITEM_1", "fieldId": "FIELD_1", "optionId": "OPT_DONE"}


def test_add_item_to_feature_project_adds_a_sub_issue_onto_the_same_board(monkeypatch):
    monkeypatch.setattr(
        github_project_mutations,
        "ensure_feature_project",
        lambda repo_url, feature_issue_number, title: {
            "project_id": "PVT_1",
            "status_field_id": "FIELD_1",
            "status_options": {"Todo": "OPT_TODO"},
        },
    )
    monkeypatch.setattr(github_project_mutations, "_issue_node_id", lambda repo_url, issue_number: f"NODE_{issue_number}")

    calls = []

    def fake_graphql(repo_url, query, variables):
        calls.append((query, variables))
        if "addProjectV2ItemById" in query:
            return {"addProjectV2ItemById": {"item": {"id": "ITEM_SUB"}}}
        if "updateProjectV2ItemFieldValue" in query:
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_SUB"}}}
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(github_project_mutations, "_graphql", fake_graphql)

    github_project_mutations.add_item_to_feature_project(
        "https://github.com/acme/app.git", 10, "My feature", issue_number=11, status="Todo"
    )

    assert calls[0][1] == {"projectId": "PVT_1", "contentId": "NODE_11"}


def test_add_item_to_feature_project_noops_when_ensure_fails(monkeypatch):
    monkeypatch.setattr(github_project_mutations, "ensure_feature_project", lambda repo_url, feature_issue_number, title: None)
    monkeypatch.setattr(github_project_mutations, "_graphql", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GraphQL")))

    github_project_mutations.add_item_to_feature_project("https://github.com/acme/app.git", 10, "My feature")  # must not raise


def test_add_item_to_feature_project_noops_on_an_unknown_status(monkeypatch):
    monkeypatch.setattr(
        github_project_mutations,
        "ensure_feature_project",
        lambda repo_url, feature_issue_number, title: {"project_id": "PVT_1", "status_field_id": "FIELD_1", "status_options": {"Todo": "OPT_TODO"}},
    )
    monkeypatch.setattr(github_project_mutations, "_graphql", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GraphQL")))

    github_project_mutations.add_item_to_feature_project("https://github.com/acme/app.git", 10, "My feature", status="Bogus")


def test_add_item_to_feature_project_noops_when_the_issue_has_no_node_id(monkeypatch):
    monkeypatch.setattr(
        github_project_mutations,
        "ensure_feature_project",
        lambda repo_url, feature_issue_number, title: {"project_id": "PVT_1", "status_field_id": "FIELD_1", "status_options": {"Todo": "OPT_TODO"}},
    )
    monkeypatch.setattr(github_project_mutations, "_issue_node_id", lambda repo_url, issue_number: None)
    monkeypatch.setattr(github_project_mutations, "_graphql", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call GraphQL")))

    github_project_mutations.add_item_to_feature_project("https://github.com/acme/app.git", 10, "My feature", status="Todo")


def test_add_item_to_feature_project_never_raises_on_an_unexpected_failure(monkeypatch):
    monkeypatch.setattr(
        github_project_mutations, "ensure_feature_project", lambda repo_url, feature_issue_number, title: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    github_project_mutations.add_item_to_feature_project("https://github.com/acme/app.git", 10, "My feature")  # must not raise

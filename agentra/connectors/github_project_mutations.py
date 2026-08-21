"""connectors/github_project_mutations.py — write operations for GitHub Projects v2."""

from __future__ import annotations

import logging

from agentra.connectors.github_projects import (
    _issue_node_id,
    _graphql,
    ensure_feature_project,
)

logger = logging.getLogger(__name__)


def add_item_to_feature_project(
    repo_url: str,
    feature_issue_number: int,
    title: str,
    issue_number: int | None = None,
    status: str = "Todo",
) -> None:
    """Adds/moves an item onto feature_issue_number's Project (provisioning it first via ensure_feature_project if needed) and sets its Status."""
    target_issue_number = feature_issue_number if issue_number is None else issue_number
    try:
        project = ensure_feature_project(repo_url, feature_issue_number, title)
        if project is None:
            return
        option_id = project["status_options"].get(status)
        if option_id is None:
            logger.error("add_item_to_feature_project: unknown status %r for %s", status, repo_url)
            return

        content_id = _issue_node_id(repo_url, target_issue_number)
        if content_id is None:
            logger.error("add_item_to_feature_project: issue #%s not found on %s", target_issue_number, repo_url)
            return

        data = _graphql(
            repo_url,
            """
            mutation($projectId: ID!, $contentId: ID!) {
              addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
                item { id }
              }
            }
            """,
            {"projectId": project["project_id"], "contentId": content_id},
        )
        item_id = data["addProjectV2ItemById"]["item"]["id"]

        _graphql(
            repo_url,
            """
            mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
              updateProjectV2ItemFieldValue(input: {
                projectId: $projectId,
                itemId: $itemId,
                fieldId: $fieldId,
                value: { singleSelectOptionId: $optionId }
              }) {
                projectV2Item { id }
              }
            }
            """,
            {
                "projectId": project["project_id"],
                "itemId": item_id,
                "fieldId": project["status_field_id"],
                "optionId": option_id,
            },
        )
    except Exception:
        logger.error(
            "add_item_to_feature_project: failed for issue #%s on %s", target_issue_number, repo_url, exc_info=True
        )

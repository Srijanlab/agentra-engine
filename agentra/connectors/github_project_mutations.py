"""connectors/github_project_mutations.py — write operations for GitHub Projects v2.

Separated from github_projects.py's read layer so mutations are isolated:
add_item_to_feature_project is the one public write path callers use
(memory.py's record_shipped path). Internal helpers (_create_project,
_create_status_field, _find_status_field, _issue_node_id, _existing_feature_project,
ensure_feature_project) live in github_projects.py since they serve both read
and write paths.
"""

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
    """Adds/moves an item onto feature_issue_number's Project (provisioning
    it first via ensure_feature_project if needed) and sets its Status.

    issue_number defaults to feature_issue_number itself; pass a different
    one to add/move one of its sub-issues instead. addProjectV2ItemById is
    idempotent on content (adding an already-added issue returns the
    existing item), so this is also how an item's card moves to a new
    status without needing to track item ids anywhere.

    Best-effort like everything else in github_projects.py: never raises,
    so a Project sync failure never affects the Issue itself."""
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

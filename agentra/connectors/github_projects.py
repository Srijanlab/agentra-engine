"""GitHub Projects v2 (GraphQL) — read, provisioning, and write operations.

One Project PER FEATURE, not per app/repo — titled after the feature itself,
not a generic per-repo name. A feature's issue is the seed item of its
Project; any sub-issues it spawns are added to the same board via
add_item_to_feature_project.

No local cache: a feature issue's Project association is queried from GitHub
directly every time (issue.projectItems), so there's no side table to drift
out of sync. ensure_feature_project() is idempotent as a result: calling it
twice finds what the first call created.

Requires the GitHub App to have the "Projects" repository permission
(read/write) granted. Confirmed live: only works for org-owned repos —
GitHub rejects createProjectV2 for personal accounts regardless of
permissions, a platform limitation. All public functions here catch their own
exceptions and return None/no-op rather than raising.
"""

from __future__ import annotations

import logging

import httpx

from agentra.connectors.github_app import GITHUB_API, get_installation_token, owner_repo_from_url

logger = logging.getLogger(__name__)

_GRAPHQL_URL = f"{GITHUB_API}/graphql"

_STATUS_FIELD_NAME = "Status"
_STATUS_OPTIONS = [
    ("Todo", "GRAY"),
    ("In Progress", "YELLOW"),
    ("Done", "GREEN"),
]


class GitHubProjectsError(Exception):
    """The repo isn't a github.com HTTPS URL, or the GraphQL call failed
    (4xx/5xx or a GraphQL-level errors array, e.g. missing Projects permission)."""


def _owner_repo_or_raise(repo_url: str) -> tuple[str, str]:
    owner_repo = owner_repo_from_url(repo_url)
    if owner_repo is None:
        raise GitHubProjectsError(f"not a github.com HTTPS URL: {repo_url!r}")
    owner, name = owner_repo.split("/", 1)
    return owner, name


def _graphql(repo_url: str, query: str, variables: dict) -> dict:
    token = get_installation_token(repo_url)
    resp = httpx.post(
        _GRAPHQL_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "variables": variables},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise GitHubProjectsError(f"GraphQL errors: {body['errors']}")
    return body["data"]


def _repository_and_owner_ids(repo_url: str) -> tuple[str, str]:
    owner, name = _owner_repo_or_raise(repo_url)
    data = _graphql(
        repo_url,
        "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id owner { id } } }",
        {"owner": owner, "name": name},
    )
    repo = data["repository"]
    return repo["id"], repo["owner"]["id"]


def _create_project(repo_url: str, owner_id: str, repository_id: str, title: str) -> dict:
    data = _graphql(
        repo_url,
        """
        mutation($ownerId: ID!, $title: String!) {
          createProjectV2(input: {ownerId: $ownerId, title: $title}) {
            projectV2 { id number url }
          }
        }
        """,
        {"ownerId": owner_id, "title": title},
    )
    project = data["createProjectV2"]["projectV2"]
    try:
        _graphql(
            repo_url,
            """
            mutation($projectId: ID!, $repositoryId: ID!) {
              linkProjectV2ToRepository(input: {projectId: $projectId, repositoryId: $repositoryId}) {
                repository { id }
              }
            }
            """,
            {"projectId": project["id"], "repositoryId": repository_id},
        )
    except Exception:
        # Cosmetic only (shows the Project under the repo's Projects tab) —
        # the board is fully functional without this link.
        logger.warning("_create_project: failed to link project to repository for %s", repo_url, exc_info=True)
    return project


def _find_status_field(repo_url: str, project_id: str) -> dict | None:
    """A freshly created ProjectV2 already has a default "Status" single-select
    field (Todo/In Progress/Done). createProjectV2Field fails with "Name has
    already been taken" if called again, so this reads it back instead.
    _create_status_field stays as a fallback for the (unobserved) case
    where a Project has no Status field at all."""
    data = _graphql(
        repo_url,
        """
        query($projectId: ID!) {
          node(id: $projectId) {
            ... on ProjectV2 {
              fields(first: 20) {
                nodes {
                  ... on ProjectV2SingleSelectField { id name options { id name } }
                }
              }
            }
          }
        }
        """,
        {"projectId": project_id},
    )
    for field in data["node"]["fields"]["nodes"]:
        if field.get("name") == _STATUS_FIELD_NAME:
            return {"field_id": field["id"], "options": {o["name"]: o["id"] for o in field["options"]}}
    return None


def _create_status_field(repo_url: str, project_id: str) -> dict:
    options = ", ".join(
        f'{{name: "{name}", color: {color}, description: ""}}' for name, color in _STATUS_OPTIONS
    )
    data = _graphql(
        repo_url,
        f"""
        mutation($projectId: ID!) {{
          createProjectV2Field(input: {{
            projectId: $projectId,
            dataType: SINGLE_SELECT,
            name: "{_STATUS_FIELD_NAME}",
            singleSelectOptions: [{options}]
          }}) {{
            projectV2Field {{
              ... on ProjectV2SingleSelectField {{ id options {{ id name }} }}
            }}
          }}
        }}
        """,
        {"projectId": project_id},
    )
    field = data["createProjectV2Field"]["projectV2Field"]
    return {"field_id": field["id"], "options": {o["name"]: o["id"] for o in field["options"]}}


def _issue_node_id(repo_url: str, issue_number: int) -> str | None:
    owner, name = _owner_repo_or_raise(repo_url)
    data = _graphql(
        repo_url,
        "query($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, name: $name) { issue(number: $number) { id } } }",
        {"owner": owner, "name": name, "number": issue_number},
    )
    issue = data["repository"]["issue"]
    return issue["id"] if issue else None


def _existing_feature_project(repo_url: str, feature_issue_number: int) -> dict | None:
    """A feature issue can only ever be the seed item of the one Project
    created for it — so its own projectItems is the live, authoritative
    answer to 'does this feature already have a board', no bookkeeping needed."""
    owner, name = _owner_repo_or_raise(repo_url)
    data = _graphql(
        repo_url,
        """
        query($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) {
            issue(number: $number) {
              projectItems(first: 5) {
                nodes {
                  project {
                    id
                    url
                    fields(first: 20) {
                      nodes {
                        ... on ProjectV2SingleSelectField { id name options { id name } }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """,
        {"owner": owner, "name": name, "number": feature_issue_number},
    )
    issue = data["repository"]["issue"]
    if issue is None:
        return None
    for item in issue["projectItems"]["nodes"]:
        project = item["project"]
        status_field = next(
            (f for f in project["fields"]["nodes"] if f.get("name") == _STATUS_FIELD_NAME), None
        )
        if status_field is None:
            continue
        return {
            "project_id": project["id"],
            "url": project["url"],
            "status_field_id": status_field["id"],
            "status_options": {o["name"]: o["id"] for o in status_field["options"]},
        }
    return None


def ensure_feature_project(repo_url: str, feature_issue_number: int, title: str) -> dict | None:
    """Idempotent: returns the existing Project for feature_issue_number if it
    already has one (via projectItems), provisioning a fresh one titled
    `title` otherwise. Does NOT add the feature issue itself to a newly
    created project — callers do that via add_item_to_feature_project, so
    there is exactly one code path for 'put an item on this board'.

    None if the repo has no github.com remote, or provisioning failed (logged)."""
    try:
        existing = _existing_feature_project(repo_url, feature_issue_number)
        if existing is not None:
            return existing

        repository_id, owner_id = _repository_and_owner_ids(repo_url)
        project = _create_project(repo_url, owner_id, repository_id, title=title)
        field = _find_status_field(repo_url, project["id"]) or _create_status_field(repo_url, project["id"])
        return {
            "project_id": project["id"],
            "url": project["url"],
            "status_field_id": field["field_id"],
            "status_options": field["options"],
        }
    except Exception:
        logger.error(
            "ensure_feature_project: failed to provision a Project for issue #%s on %s",
            feature_issue_number, repo_url, exc_info=True,
        )
        return None


def get_feature_project_url(repo_url: str, feature_issue_number: int) -> str | None:
    """Read-only: does feature_issue_number already have a Project board?
    Never provisions one (unlike ensure_feature_project) — for the
    dashboard, which shouldn't create GitHub infrastructure just by
    rendering a page."""
    try:
        project = _existing_feature_project(repo_url, feature_issue_number)
        return project["url"] if project else None
    except Exception:
        return None


def get_feature_status(repo_url: str, feature_issue_number: int) -> str | None:
    """The feature issue's own Project card Status value ("Todo"/"In
    Progress"/"Done"), or None. Distinct from _existing_feature_project's
    query: that one fetches the Status FIELD's available options (for
    writing); this one fetches the VALUE actually set on this issue's card.

    Used by memory.py's in_progress_features() to distinguish 'broken into
    parts, already underway' from 'broken into parts, nothing started yet' —
    something subIssuesSummary.total > 0 alone can't do."""
    try:
        owner, name = _owner_repo_or_raise(repo_url)
        data = _graphql(
            repo_url,
            """
            query($owner: String!, $name: String!, $number: Int!) {
              repository(owner: $owner, name: $name) {
                issue(number: $number) {
                  projectItems(first: 5) {
                    nodes {
                      fieldValueByName(name: "Status") {
                        ... on ProjectV2ItemFieldSingleSelectValue { name }
                      }
                    }
                  }
                }
              }
            }
            """,
            {"owner": owner, "name": name, "number": feature_issue_number},
        )
        issue = data["repository"]["issue"]
        if issue is None:
            return None
        for item in issue["projectItems"]["nodes"]:
            value = item.get("fieldValueByName")
            if value and value.get("name"):
                return value["name"]
        return None
    except Exception:
        logger.warning("get_feature_status: lookup failed for issue #%s on %s", feature_issue_number, repo_url, exc_info=True)
        return None


def add_item_to_feature_project(
    repo_url: str,
    feature_issue_number: int,
    title: str,
    issue_number: int | None = None,
    status: str = "Todo",
) -> None:
    """Backward-compatible re-export: the implementation lives in
    github_project_mutations.py. Imported lazily (not at module load time)
    since that module imports ensure_feature_project/_issue_node_id/_graphql
    from this one -- an eager import here would be circular."""
    from agentra.connectors.github_project_mutations import add_item_to_feature_project as _impl

    return _impl(repo_url, feature_issue_number, title, issue_number=issue_number, status=status)


__all__ = [
    "GitHubProjectsError",
    "ensure_feature_project",
    "get_feature_project_url",
    "get_feature_status",
    "add_item_to_feature_project",
]

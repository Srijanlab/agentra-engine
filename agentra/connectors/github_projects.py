"""GitHub Projects v2 (GraphQL) -- the board "feature mapped to project,
bug mapped to issue" refers to. Every feature already gets a real GitHub
Issue (memory.py's record_feature_request/record_shipped, label
"enhancement") -- this module layers a Project board on top of that same
Issue, not a second, independent store of what a feature is. known_bugs
stay Issues-only; nothing here ever touches them.

One Project PER FEATURE, not per app/repo -- titled after the feature
itself (e.g. "Promotion capability with human in loop for testing report
verification"), not a generic per-repo name. A feature's own issue is the
first item on its Project; any sub-issues it gets broken into (see
github_issues.create_sub_issue, GitHub's native parent/sub-issue
relationship) are added to that same board as further items, all tracked
through the one "Status" single-select field (Todo/In Progress/Done --
"just status", no Priority/Size/other fields) every fresh Project already
comes with by default.

No local cache and no GitHub Variables involved: a feature issue's
Project association is asked from GitHub directly every time
(issue.projectItems), since that's where the relationship actually lives
-- there's no side table here that could ever drift out of sync with it.
ensure_feature_project() is idempotent purely because of that: call it
twice for the same feature issue and the second call just finds what the
first one created.

Requires the GitHub App to have the "Projects" repository permission
granted (read/write) -- a manual step on the App's settings page, same
prerequisite github_issues.py's Issues permission needed. Confirmed live:
this only works for organization-owned repos -- GitHub rejects
createProjectV2 for a personal (non-org) account's repos regardless of
permissions granted, a platform limitation, not something fixable here.
Every public function in this module fails soft either way: catches its
own exceptions, logs, and returns None/no-ops rather than raising, so a
Project provisioning hiccup or a missing permission never blocks filing
or shipping a feature -- the Issue itself (the authoritative record) is
unaffected.
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
    """The repo isn't a github.com HTTPS URL, or the GraphQL call itself
    failed (a 4xx/5xx, or a GraphQL-level `errors` array -- e.g. the App
    lacks the Projects permission)."""


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
        # Cosmetic only (shows the Project under the repo's own Projects tab) --
        # the board is fully functional without this link.
        logger.warning("_create_project: failed to link project to repository for %s", repo_url, exc_info=True)
    return project


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


def _find_status_field(repo_url: str, project_id: str) -> dict | None:
    """A freshly created ProjectV2 already has its own default "Status"
    single-select field (Todo/In Progress/Done) -- confirmed live,
    createProjectV2Field's own attempt to add a second one fails outright
    ("Name has already been taken"). This reads that existing field back
    instead of creating one; _create_status_field stays as a fallback for
    the case (not observed, but not guaranteed either) where a Project
    somehow has no Status field at all."""
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
    created for it (ensure_feature_project always adds it there first) --
    so its own projectItems is the live, authoritative answer to "does
    this feature already have a board," no local bookkeeping required."""
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
    """Idempotent, no caching: returns feature_issue_number's existing
    Project if it already has one (found via its own projectItems),
    provisioning a fresh one -- titled `title` (the feature's own title,
    not a generic per-repo name) -- otherwise. Does NOT add
    feature_issue_number to a newly created project itself; callers do
    that via add_item_to_feature_project the same way any other item gets
    added, so there's exactly one code path for "put an item on this
    feature's board" rather than two. None if the repo has no github.com
    remote, or provisioning failed for any reason (logged, never raised)
    -- callers should treat that exactly like "no Project yet"."""
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


def add_item_to_feature_project(
    repo_url: str,
    feature_issue_number: int,
    title: str,
    issue_number: int | None = None,
    status: str = "Todo",
) -> None:
    """Adds/moves an item onto feature_issue_number's Project (provisioning
    it first via ensure_feature_project if needed) and sets its Status.
    `issue_number` defaults to feature_issue_number itself (the feature's
    own main issue); pass a different one to add/move one of its
    sub-issues onto the same board instead. addProjectV2ItemById is
    idempotent on content (adding an already-added issue again just
    returns the existing item), so this is also how an item's card moves
    to a new status without agentra having to remember its item id
    anywhere -- resolving it by issue number again is cheap and stateless.
    Best-effort like everything else in this module: never raises, so a
    Project sync failure never affects the Issue itself."""
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
            {"projectId": project["project_id"], "itemId": item_id, "fieldId": project["status_field_id"], "optionId": option_id},
        )
    except Exception:
        logger.error(
            "add_item_to_feature_project: failed for issue #%s on %s", target_issue_number, repo_url, exc_info=True
        )

"""GitHub Projects v2 (GraphQL) -- the board "feature mapped to project,
bug mapped to issue" refers to. Every feature already gets a real GitHub
Issue (memory.py's record_feature_request/record_shipped, label
"enhancement") -- this module layers a Project board on top of that same
Issue, not a second, independent store of what a feature is. known_bugs
stay Issues-only; nothing here ever touches them.

One Project per app, provisioned lazily via ensure_project() -- not at
onboarding, the first time add_item_to_project() actually has a feature to
put on it (memory.py's record_feature_request/record_shipped). An app
that never gets a feature never gets a board. Its single "Status"
single-select field (Todo/In Progress/Done -- "just status", no
Priority/Size/other fields) and other IDs are cached as GitHub Actions
Variables (AGENTRA_PROJECT_*) the same way environments.py/memory.py
cache their own config -- no local file, and ensure_project() re-provisions
nothing on repeat calls once those variables exist.

Requires the GitHub App to have the "Projects" repository permission
granted (read/write) -- a manual step on the App's settings page, same
prerequisite github_issues.py's Issues permission needed. Every public
function here fails soft: catches its own exceptions, logs, and returns
None/no-ops rather than raising, so a Project provisioning hiccup or a
missing permission never blocks registering an app or filing/shipping a
feature -- the Issue itself (the authoritative record) is unaffected
either way.
"""

from __future__ import annotations

import logging

import httpx

from agentra.connectors.github_app import GITHUB_API, get_installation_token, owner_repo_from_url
from agentra.connectors import github_variables

logger = logging.getLogger(__name__)

_GRAPHQL_URL = f"{GITHUB_API}/graphql"

_PROJECT_TITLE = "agentra features"
_STATUS_FIELD_NAME = "Status"
_STATUS_OPTIONS = [
    ("Todo", "GRAY"),
    ("In Progress", "YELLOW"),
    ("Done", "GREEN"),
]

_VAR_PROJECT_ID = "AGENTRA_PROJECT_ID"
_VAR_PROJECT_NUMBER = "AGENTRA_PROJECT_NUMBER"
_VAR_PROJECT_URL = "AGENTRA_PROJECT_URL"
_VAR_STATUS_FIELD_ID = "AGENTRA_PROJECT_STATUS_FIELD_ID"
_VAR_STATUS_OPTION_PREFIX = "AGENTRA_PROJECT_STATUS_OPTION_"


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


def _create_project(repo_url: str, owner_id: str, repository_id: str) -> dict:
    data = _graphql(
        repo_url,
        """
        mutation($ownerId: ID!, $title: String!) {
          createProjectV2(input: {ownerId: $ownerId, title: $title}) {
            projectV2 { id number url }
          }
        }
        """,
        {"ownerId": owner_id, "title": _PROJECT_TITLE},
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


def ensure_project(repo_url: str) -> dict | None:
    """Idempotent: returns the cached {"project_id", "status_field_id",
    "status_options": {"Todo": id, "In Progress": id, "Done": id}} from
    GitHub Variables if this repo already has a Project, provisioning a
    fresh one (with its single Status field) on first call otherwise.
    None if the repo has no github.com remote, or provisioning failed for
    any reason (logged, never raised) -- callers should treat that exactly
    like "no Project yet" and keep going, never block on it."""
    try:
        existing = github_variables.list_variables(repo_url)
    except Exception:
        logger.error("ensure_project: GitHub Variables unavailable for %s", repo_url, exc_info=True)
        return None

    if existing.get(_VAR_PROJECT_ID) and existing.get(_VAR_STATUS_FIELD_ID):
        status_options = {
            name: existing[f"{_VAR_STATUS_OPTION_PREFIX}{name.upper().replace(' ', '_')}"]
            for name, _ in _STATUS_OPTIONS
            if f"{_VAR_STATUS_OPTION_PREFIX}{name.upper().replace(' ', '_')}" in existing
        }
        if status_options:
            return {
                "project_id": existing[_VAR_PROJECT_ID],
                "status_field_id": existing[_VAR_STATUS_FIELD_ID],
                "status_options": status_options,
            }

    try:
        repository_id, owner_id = _repository_and_owner_ids(repo_url)
        project = _create_project(repo_url, owner_id, repository_id)
        field = _find_status_field(repo_url, project["id"]) or _create_status_field(repo_url, project["id"])

        github_variables.set_variable(repo_url, _VAR_PROJECT_ID, project["id"])
        github_variables.set_variable(repo_url, _VAR_PROJECT_NUMBER, str(project["number"]))
        github_variables.set_variable(repo_url, _VAR_PROJECT_URL, project["url"])
        github_variables.set_variable(repo_url, _VAR_STATUS_FIELD_ID, field["field_id"])
        for name, option_id in field["options"].items():
            github_variables.set_variable(repo_url, f"{_VAR_STATUS_OPTION_PREFIX}{name.upper().replace(' ', '_')}", option_id)

        return {"project_id": project["id"], "status_field_id": field["field_id"], "status_options": field["options"]}
    except Exception:
        logger.error("ensure_project: failed to provision a Project for %s", repo_url, exc_info=True)
        return None


def get_project_url(repo_url: str) -> str | None:
    """Read-only: the dashboard's "View Project board" link. Deliberately
    does NOT provision a Project if one doesn't exist yet (unlike
    ensure_project) -- this is called on every app-detail page load, and a
    page view should never have the side effect of creating GitHub
    infrastructure. None if there's no Project yet, no github.com remote,
    or GitHub Variables are unreachable."""
    try:
        return github_variables.list_variables(repo_url).get(_VAR_PROJECT_URL)
    except Exception:
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


def add_item_to_project(repo_url: str, issue_number: int, status: str = "Todo") -> None:
    """Adds `issue_number` to this repo's Project (provisioning one via
    ensure_project() first if needed) and sets its Status field -- called
    right after memory.py creates or closes a feature-labeled Issue, so
    "Todo" tracks record_feature_request and "Done" tracks record_shipped.
    addProjectV2ItemById is idempotent on content (adding an already-added
    issue again just returns the existing item), so this is also exactly
    how a shipped feature's card gets moved to Done without agentra having
    to remember its item id anywhere -- resolving it by issue number again
    is cheap and stateless. Best-effort like everything else in this
    module: never raises, so a Project sync failure never affects the
    Issue itself."""
    try:
        project = ensure_project(repo_url)
        if project is None:
            return
        option_id = project["status_options"].get(status)
        if option_id is None:
            logger.error("add_item_to_project: unknown status %r for %s", status, repo_url)
            return

        content_id = _issue_node_id(repo_url, issue_number)
        if content_id is None:
            logger.error("add_item_to_project: issue #%s not found on %s", issue_number, repo_url)
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
        logger.error("add_item_to_project: failed for issue #%s on %s", issue_number, repo_url, exc_info=True)

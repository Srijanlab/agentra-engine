"""In-memory fake for github_issues/github_variables/github_projects --
used by dev_seed.py (AGENTRA_DEV_MODE has no real GitHub App credentials,
but known_bugs/feature_queue/objective/environments/Projects are
GitHub-only now, with no local file fallback) and by tests that need a
real create -> list -> close round-trip without a live GitHub API call.

github_projects.py talks GraphQL, not the REST shape the rest of this
module fakes -- rather than faking individual GraphQL mutations, its two
public functions (ensure_feature_project/add_item_to_feature_project) are
faked directly at the same level github_issues.create_issue etc. already
are. Projects are keyed by "{repo_url}#{feature_issue_number}" -- one per
feature, not one per repo.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path


def _repo_https_url(repo_url: str) -> str:
    """Best-effort 'https://github.com/owner/repo' for building fake
    html_url/issue links in dev mode -- handles both HTTPS and git@ SSH
    forms. Falls back to the raw repo_url (still a usable, if odd-looking,
    link) for anything that doesn't match either shape."""
    m = re.match(r"^(?:https://github\.com/|git@github\.com:)([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url)
    return f"https://github.com/{m.group(1)}/{m.group(2)}" if m else repo_url


class FakeGitHubBackend:
    """Namespaced by repo_url -- a real GitHub API is one repo per call,
    and dev_seed.py seeds multiple fixture apps against one shared
    instance of this backend, so un-namespaced storage would leak one
    app's issues/variables into another's (confirmed live: agentra's
    fixture picked up "cap"'s objective and bug count before this).

    Optionally persists to `persist_path` as plain JSON -- dev.sh's
    documented workflow seeds fixture data in one short-lived process
    (`python3 -c "from agentra.dev_seed import seed; seed()"`) and then
    starts `agentra serve` as a separate process; an in-memory-only fake
    can't survive that process boundary, so dev mode needs this written
    to disk under AGENTRA_HOME specifically (never under a target repo's
    own .agentra/ -- that's the real "no local file" boundary this fake
    doesn't cross)."""

    _STATUS_OPTIONS = ["Todo", "In Progress", "Done"]

    def __init__(self, persist_path: Path | None = None) -> None:
        self.issues: dict[str, dict[int, dict]] = defaultdict(dict)
        self.variables: dict[str, dict[str, str]] = defaultdict(dict)
        self.projects: dict[str, dict] = {}
        self._next_issue_number: dict[str, int] = defaultdict(lambda: 1)
        self._persist_path = persist_path
        if persist_path and persist_path.exists():
            try:
                data = json.loads(persist_path.read_text())
                self.issues = defaultdict(dict, {k: {int(n): v for n, v in issues.items()} for k, issues in data.get("issues", {}).items()})
                self.variables = defaultdict(dict, data.get("variables", {}))
                self.projects = data.get("projects", {})
                self._next_issue_number = defaultdict(lambda: 1, data.get("next_issue_number", {}))
            except Exception:
                pass  # corrupt/partial fixture state -- start fresh rather than crash dev mode

    def _save(self) -> None:
        if not self._persist_path:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        self._persist_path.write_text(
            json.dumps(
                {
                    "issues": self.issues,
                    "variables": self.variables,
                    "projects": self.projects,
                    "next_issue_number": self._next_issue_number,
                },
                indent=2,
            )
        )

    def create_issue(self, repo_url: str, title: str, body: str, labels: list[str] | None = None) -> dict:
        number = self._next_issue_number[repo_url]
        self._next_issue_number[repo_url] += 1
        self.issues[repo_url][number] = {
            "number": number,
            "title": title,
            "body": body,
            "labels": labels or [],
            "state": "open",
            "html_url": f"{_repo_https_url(repo_url)}/issues/{number}",
        }
        self._save()
        return dict(self.issues[repo_url][number])

    def create_sub_issue(
        self, repo_url: str, parent_issue_number: int, title: str, body: str, labels: list[str] | None = None
    ) -> dict:
        sub_issue = self.create_issue(repo_url, title, body, labels=labels)
        if parent_issue_number in self.issues[repo_url]:
            self.issues[repo_url][parent_issue_number].setdefault("sub_issue_numbers", []).append(sub_issue["number"])
            self._save()
        return sub_issue

    def list_open_issues(self, repo_url: str, labels: list[str] | None = None) -> list[dict]:
        results = [i for i in self.issues[repo_url].values() if i["state"] == "open"]
        if labels:
            results = [i for i in results if any(label in i["labels"] for label in labels)]
        return [dict(i) for i in results]

    def list_closed_issues(self, repo_url: str, labels: list[str] | None = None, limit: int = 30) -> list[dict]:
        results = [i for i in self.issues[repo_url].values() if i["state"] == "closed"]
        if labels:
            results = [i for i in results if any(label in i["labels"] for label in labels)]
        results.sort(key=lambda i: i.get("closed_at") or "", reverse=True)
        return [dict(i) for i in results[:limit]]

    def close_issue(
        self, repo_url: str, issue_number: int, comment: str | None = None, body_suffix: str | None = None
    ) -> None:
        if issue_number in self.issues[repo_url]:
            issue = self.issues[repo_url][issue_number]
            issue["state"] = "closed"
            issue["closed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            if body_suffix:
                issue["body"] = (issue.get("body") or "").rstrip() + "\n\n" + body_suffix
            self._save()

    def list_variables(self, repo_url: str) -> dict[str, str]:
        return dict(self.variables[repo_url])

    def set_variable(self, repo_url: str, name: str, value: str) -> None:
        self.variables[repo_url][name] = value
        self._save()

    def ensure_feature_project(self, repo_url: str, feature_issue_number: int, title: str) -> dict | None:
        key = f"{repo_url}#{feature_issue_number}"
        project = self.projects.get(key)
        if project is None:
            project = {
                "project_id": f"PVT_fake_{key}",
                "url": f"{_repo_https_url(repo_url)}/projects/fake-{feature_issue_number}",
                "title": title,
                "status_field_id": "PVTSSF_fake_status",
                "status_options": {name: f"OPT_fake_{name.lower().replace(' ', '_')}" for name in self._STATUS_OPTIONS},
                "items": {},
            }
            self.projects[key] = project
            self._save()
        return {k: v for k, v in project.items() if k != "items"}

    def add_item_to_feature_project(
        self, repo_url: str, feature_issue_number: int, title: str, issue_number: int | None = None, status: str = "Todo"
    ) -> None:
        target_issue_number = feature_issue_number if issue_number is None else issue_number
        project = self.ensure_feature_project(repo_url, feature_issue_number, title)
        if project is None or status not in project["status_options"]:
            return
        key = f"{repo_url}#{feature_issue_number}"
        self.projects[key]["items"][str(target_issue_number)] = status
        self._save()


def install(backend: FakeGitHubBackend | None = None, monkeypatch=None, persist_path: Path | None = None) -> FakeGitHubBackend:
    """Patches the github_issues/github_variables modules in place with
    `backend`'s methods -- callers (memory.py, environments.py) always go
    through the module object at call time (`from agentra.connectors
    import github_issues; github_issues.create_issue(...)`), never bind a
    function reference at import time, so reassigning these attributes
    here is enough for every existing call site to pick it up.

    Pass `monkeypatch` (a pytest fixture) from tests so the patch reverts
    after the test -- dev_seed.py calls this with no monkeypatch, since
    its process never has real GitHub credentials anyway and the patch is
    meant to live for the whole `agentra dev` process. Without this, a
    test-only direct assignment would permanently mutate these modules
    for every test file that runs afterward in the same pytest process."""
    from agentra.connectors import github_issues, github_projects, github_variables

    backend = backend or FakeGitHubBackend(persist_path=persist_path)
    patches = [
        (github_issues, "create_issue", backend.create_issue),
        (github_issues, "create_sub_issue", backend.create_sub_issue),
        (github_issues, "list_open_issues", backend.list_open_issues),
        (github_issues, "list_closed_issues", backend.list_closed_issues),
        (github_issues, "close_issue", backend.close_issue),
        (github_variables, "list_variables", backend.list_variables),
        (github_variables, "set_variable", backend.set_variable),
        (github_projects, "ensure_feature_project", backend.ensure_feature_project),
        (github_projects, "add_item_to_feature_project", backend.add_item_to_feature_project),
    ]
    for module, attr, fn in patches:
        if monkeypatch is not None:
            monkeypatch.setattr(module, attr, fn)
        else:
            setattr(module, attr, fn)
    return backend

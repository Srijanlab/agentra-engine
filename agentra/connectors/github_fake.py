"""In-memory fake for github_issues/github_variables -- used by dev_seed.py..."""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path


def _repo_https_url(repo_url: str) -> str:
    """Best-effort 'https://github.com/owner/repo' for building fake html_url/issue links in dev mode -- handles both HTTPS and git@ SSH forms."""
    m = re.match(r"^(?:https://github\.com/|git@github\.com:)([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url)
    return f"https://github.com/{m.group(1)}/{m.group(2)}" if m else repo_url


class FakeGitHubBackend:
    """Namespaced by repo_url -- a real GitHub API is one repo per call, and dev_seed.py seeds multiple fixture apps against one shared instance of this backend, so un-namespaced storage would leak one app's issues/variables into another's (confirmed live: agentra's fixture picked up "cap"'s objective and bug count before this)."""

    def __init__(self, persist_path: Path | None = None) -> None:
        self.issues: dict[str, dict[int, dict]] = defaultdict(dict)
        self.variables: dict[str, dict[str, str]] = defaultdict(dict)
        self._next_issue_number: dict[str, int] = defaultdict(lambda: 1)
        self._persist_path = persist_path
        if persist_path and persist_path.exists():
            try:
                data = json.loads(persist_path.read_text())
                self.issues = defaultdict(dict, {k: {int(n): v for n, v in issues.items()} for k, issues in data.get("issues", {}).items()})
                self.variables = defaultdict(dict, data.get("variables", {}))
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
        self.add_issue_as_sub_issue(repo_url, parent_issue_number, sub_issue["number"])
        return sub_issue

    def add_issue_as_sub_issue(self, repo_url: str, parent_issue_number: int, sub_issue_number: int) -> None:
        if parent_issue_number in self.issues[repo_url]:
            self.issues[repo_url][parent_issue_number].setdefault("sub_issue_numbers", []).append(sub_issue_number)
            self._save()

    def get_issue(self, repo_url: str, issue_number: int) -> dict | None:
        issue = self.issues[repo_url].get(issue_number)
        return dict(issue) if issue else None

    def list_open_issues(self, repo_url: str, labels: list[str] | None = None) -> list[dict]:
        results = [i for i in self.issues[repo_url].values() if i["state"] == "open"]
        if labels:
            results = [i for i in results if any(label in i["labels"] for label in labels)]
        return [dict(i) for i in results]

    def list_closed_issues(self, repo_url: str, labels: list[str] | None = None, limit: int = 5) -> list[dict]:
        results = [i for i in self.issues[repo_url].values() if i["state"] == "closed"]
        if labels:
            results = [i for i in results if any(label in i["labels"] for label in labels)]
        results.sort(key=lambda i: i.get("closed_at") or "", reverse=True)
        return [dict(i) for i in results[:limit]]

    def list_in_progress_features(self, repo_url: str, labels: list[str] | None = None) -> list[dict]:
        results = [
            i
            for i in self.issues[repo_url].values()
            if i["state"] == "open" and i.get("sub_issue_numbers") and "status:shipped" not in i["labels"]
        ]
        if labels:
            results = [i for i in results if any(label in i["labels"] for label in labels)]
        return [
            {
                "number": i["number"],
                "title": i["title"],
                "body": i.get("body"),
                "html_url": i.get("html_url"),
                "sub_issues_total": len(i["sub_issue_numbers"]),
                "sub_issues_completed": sum(
                    1
                    for n in i["sub_issue_numbers"]
                    if self.issues[repo_url].get(n, {}).get("state") == "closed"
                ),
            }
            for i in results
        ]

    def add_comment(self, repo_url: str, issue_number: int, comment: str) -> None:
        if issue_number in self.issues[repo_url]:
            self.issues[repo_url][issue_number].setdefault("comments", []).append(comment)
            self._save()

    def list_comments(self, repo_url: str, issue_number: int) -> list[dict]:
        return [{"body": c} for c in self.issues[repo_url].get(issue_number, {}).get("comments", [])]

    _IN_PROGRESS_BRANCH_RE = re.compile(r"^In-Progress-Branch: (\S+)$", re.MULTILINE)
    _IN_PROGRESS_RUN_ID_RE = re.compile(r"^Run-ID: (\S+)$", re.MULTILINE)

    def record_in_progress_branch(self, repo_url: str, issue_number: int, branch: str, run_id: str | None = None) -> None:
        body = f"In-Progress-Branch: {branch}"
        if run_id:
            body += f"\nRun-ID: {run_id}"
        self.add_comment(repo_url, issue_number, body)

    def get_in_progress_branch(self, repo_url: str, issue_number: int) -> str | None:
        comments = self.issues[repo_url].get(issue_number, {}).get("comments", [])
        for comment in reversed(comments):
            match = self._IN_PROGRESS_BRANCH_RE.search(comment)
            if match:
                return match.group(1)
        return None

    def get_in_progress_run_id(self, repo_url: str, issue_number: int) -> str | None:
        comments = self.issues[repo_url].get(issue_number, {}).get("comments", [])
        for comment in reversed(comments):
            if not self._IN_PROGRESS_BRANCH_RE.search(comment):
                continue
            match = self._IN_PROGRESS_RUN_ID_RE.search(comment)
            return match.group(1) if match else None
        return None

    _IN_PROGRESS_SESSION_ID_RE = re.compile(r"^Session-ID: (\S+)$", re.MULTILINE)

    def get_in_progress_session_id(self, repo_url: str, issue_number: int) -> str | None:
        comments = self.issues[repo_url].get(issue_number, {}).get("comments", [])
        for comment in reversed(comments):
            if not self._IN_PROGRESS_BRANCH_RE.search(comment):
                continue
            match = self._IN_PROGRESS_SESSION_ID_RE.search(comment)
            return match.group(1) if match else None
        return None

    # Human-in-the-loop escalation (GitHub issue #34) -- same marker/regex
    _HUMAN_INPUT_MARKER = "Human-Input-Required (agentra):"
    _HUMAN_INPUT_APP_RE = re.compile(r"^App: (\S+)$", re.MULTILINE)
    _HUMAN_INPUT_RUN_ID_RE = re.compile(r"^Run-ID: (\S+)$", re.MULTILINE)
    _HUMAN_INPUT_BRANCH_RE = re.compile(r"^Branch: (\S+)$", re.MULTILINE)
    _HUMAN_INPUT_SESSION_ID_RE = re.compile(r"^Session-ID: (\S+)$", re.MULTILINE)
    _HUMAN_INPUT_TRACKING_ISSUE_RE = re.compile(r"^Tracking-Issue: (\d+)$", re.MULTILINE)
    _HUMAN_INPUT_QUESTION_RE = re.compile(r"^Question: (.*)$", re.MULTILINE)
    # "Spec (agentra):" duplicated as a literal (not _SPEC_MARKER) -- that
    _INTERNAL_COMMENT_PREFIXES = ("In-Progress-Branch:", "Spec (agentra):", "Commit:", _HUMAN_INPUT_MARKER, "Answered:")

    def record_human_input_context(
        self,
        repo_url: str,
        issue_number: int,
        *,
        app: str,
        run_id: str,
        question: str,
        branch: str | None = None,
        session_id: str | None = None,
        tracking_issue: int | None = None,
    ) -> None:
        question_line = " ".join(question.split())
        body = f"{self._HUMAN_INPUT_MARKER}\nApp: {app}\nRun-ID: {run_id}\n"
        if branch:
            body += f"Branch: {branch}\n"
        if session_id:
            body += f"Session-ID: {session_id}\n"
        if tracking_issue is not None:
            body += f"Tracking-Issue: {tracking_issue}\n"
        body += f"Question: {question_line}"
        self.add_comment(repo_url, issue_number, body)

    def get_human_input_context(self, repo_url: str, issue_number: int) -> dict | None:
        comments = self.issues[repo_url].get(issue_number, {}).get("comments", [])
        for comment in reversed(comments):
            if not comment.startswith(self._HUMAN_INPUT_MARKER):
                continue
            app_m = self._HUMAN_INPUT_APP_RE.search(comment)
            run_id_m = self._HUMAN_INPUT_RUN_ID_RE.search(comment)
            branch_m = self._HUMAN_INPUT_BRANCH_RE.search(comment)
            session_id_m = self._HUMAN_INPUT_SESSION_ID_RE.search(comment)
            tracking_issue_m = self._HUMAN_INPUT_TRACKING_ISSUE_RE.search(comment)
            question_m = self._HUMAN_INPUT_QUESTION_RE.search(comment)
            return {
                "app": app_m.group(1) if app_m else None,
                "run_id": run_id_m.group(1) if run_id_m else None,
                "branch": branch_m.group(1) if branch_m else None,
                "session_id": session_id_m.group(1) if session_id_m else None,
                "tracking_issue": int(tracking_issue_m.group(1)) if tracking_issue_m else None,
                "question": question_m.group(1) if question_m else None,
            }
        return None

    def record_human_answer(self, repo_url: str, issue_number: int, answer: str, resumed_run_key: str | None = None) -> None:
        body = f"Answered: {answer}"
        if resumed_run_key:
            body += f"\n\nResuming as run {resumed_run_key}."
        self.add_comment(repo_url, issue_number, body)

    def find_unanswered_human_input_comment(self, repo_url: str, issue_number: int) -> str | None:
        comments = self.issues[repo_url].get(issue_number, {}).get("comments", [])
        marker_seen = False
        for comment in comments:  # oldest-first
            if comment.startswith(self._HUMAN_INPUT_MARKER):
                marker_seen = True
                continue
            if not marker_seen:
                continue
            if any(comment.startswith(prefix) for prefix in self._INTERNAL_COMMENT_PREFIXES):
                continue
            if not comment.strip():
                continue
            return comment.strip()
        return None

    def remove_label(self, repo_url: str, issue_number: int, label: str) -> None:
        if issue_number in self.issues[repo_url]:
            existing = self.issues[repo_url][issue_number]["labels"]
            self.issues[repo_url][issue_number]["labels"] = [l for l in existing if l != label]
            self._save()

    def issue_html_url(self, repo_url: str, issue_number: int) -> str | None:
        issue = self.issues[repo_url].get(issue_number)
        if issue is not None:
            return issue.get("html_url")
        return f"{_repo_https_url(repo_url)}/issues/{issue_number}"

    def record_commit(self, repo_url: str, issue_number: int, commit_sha: str) -> None:
        self.add_comment(repo_url, issue_number, f"Commit: {commit_sha}")

    _SPEC_MARKER = "Spec (agentra):"

    def record_spec(self, repo_url: str, issue_number: int, spec: dict) -> None:
        self.add_comment(repo_url, issue_number, f"{self._SPEC_MARKER}\n\n```json\n{json.dumps(spec, indent=2)}\n```")

    def get_spec(self, repo_url: str, issue_number: int) -> dict | None:
        comments = self.issues[repo_url].get(issue_number, {}).get("comments", [])
        for comment in reversed(comments):
            if not comment.startswith(self._SPEC_MARKER):
                continue
            match = re.search(r"```json\s*\n(.*?)\n```", comment, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    continue
        return None

    def ensure_labels(self, repo_url: str) -> None:
        pass  # fake backend doesn't validate label existence at all -- nothing to ensure

    def add_labels(self, repo_url: str, issue_number: int, labels: list[str]) -> None:
        if issue_number in self.issues[repo_url]:
            existing = self.issues[repo_url][issue_number]["labels"]
            self.issues[repo_url][issue_number]["labels"] = existing + [l for l in labels if l not in existing]
            self._save()

    def close_issue(
        self, repo_url: str, issue_number: int, comment: str | None = None, body_suffix: str | None = None
    ) -> None:
        if issue_number in self.issues[repo_url]:
            if comment:
                self.add_comment(repo_url, issue_number, comment)
            issue = self.issues[repo_url][issue_number]
            issue["state"] = "closed"
            issue["closed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            if body_suffix:
                issue["body"] = (issue.get("body") or "").rstrip() + "\n\n" + body_suffix
            self._save()

    def mark_shipped(
        self, repo_url: str, issue_number: int, comment: str | None = None, body_suffix: str | None = None
    ) -> None:
        if issue_number not in self.issues[repo_url]:
            return
        if comment:
            self.add_comment(repo_url, issue_number, comment)
        if body_suffix:
            issue = self.issues[repo_url][issue_number]
            issue["body"] = (issue.get("body") or "").rstrip() + "\n\n" + body_suffix
            self._save()
        self.add_labels(repo_url, issue_number, ["status:shipped"])

    def mark_code_complete(
        self, repo_url: str, issue_number: int, comment: str | None = None, body_suffix: str | None = None
    ) -> None:
        if issue_number not in self.issues[repo_url]:
            return
        if comment:
            self.add_comment(repo_url, issue_number, comment)
        if body_suffix:
            issue = self.issues[repo_url][issue_number]
            issue["body"] = (issue.get("body") or "").rstrip() + "\n\n" + body_suffix
            self._save()
        self.add_labels(repo_url, issue_number, ["status:code_complete"])

    def mark_shipped_to_preprod(self, repo_url: str, issue_number: int, comment: str | None = None) -> None:
        if issue_number not in self.issues[repo_url]:
            return
        if comment:
            self.add_comment(repo_url, issue_number, comment)
        self.remove_label(repo_url, issue_number, "status:code_complete")
        self.add_labels(repo_url, issue_number, ["status:shipped"])

    def mark_tested(self, repo_url: str, issue_number: int, comment: str | None = None) -> None:
        if issue_number not in self.issues[repo_url]:
            return
        if comment:
            self.add_comment(repo_url, issue_number, comment)
        self.remove_label(repo_url, issue_number, "status:shipped")
        self.add_labels(repo_url, issue_number, ["status:tested"])

    def list_variables(self, repo_url: str) -> dict[str, str]:
        return dict(self.variables[repo_url])

    def set_variable(self, repo_url: str, name: str, value: str) -> None:
        self.variables[repo_url][name] = value
        self._save()


def install(backend: FakeGitHubBackend | None = None, monkeypatch=None, persist_path: Path | None = None) -> FakeGitHubBackend:
    """Patches the github_issues/github_variables modules in place with `backend`'s methods -- callers (memory.py, environments.py) always go through the module object at call time (`from agentra.connectors import github_issues; github_issues.create_issue(...)`), never bind a function reference at import time, so reassigning these attributes here is enough for every existing call site to pick it up."""
    from agentra.connectors import github_issues, github_variables

    backend = backend or FakeGitHubBackend(persist_path=persist_path)
    patches = [
        (github_issues, "create_issue", backend.create_issue),
        (github_issues, "create_sub_issue", backend.create_sub_issue),
        (github_issues, "add_issue_as_sub_issue", backend.add_issue_as_sub_issue),
        (github_issues, "get_issue", backend.get_issue),
        (github_issues, "list_open_issues", backend.list_open_issues),
        (github_issues, "list_closed_issues", backend.list_closed_issues),
        (github_issues, "list_in_progress_features", backend.list_in_progress_features),
        (github_issues, "close_issue", backend.close_issue),
        (github_issues, "mark_shipped", backend.mark_shipped),
        (github_issues, "mark_code_complete", backend.mark_code_complete),
        (github_issues, "mark_shipped_to_preprod", backend.mark_shipped_to_preprod),
        (github_issues, "mark_tested", backend.mark_tested),
        (github_issues, "add_comment", backend.add_comment),
        (github_issues, "list_comments", backend.list_comments),
        (github_issues, "record_in_progress_branch", backend.record_in_progress_branch),
        (github_issues, "get_in_progress_branch", backend.get_in_progress_branch),
        (github_issues, "get_in_progress_run_id", backend.get_in_progress_run_id),
        (github_issues, "get_in_progress_session_id", backend.get_in_progress_session_id),
        (github_issues, "record_spec", backend.record_spec),
        (github_issues, "get_spec", backend.get_spec),
        (github_issues, "record_commit", backend.record_commit),
        (github_issues, "add_labels", backend.add_labels),
        (github_issues, "ensure_labels", backend.ensure_labels),
        (github_issues, "remove_label", backend.remove_label),
        (github_issues, "issue_html_url", backend.issue_html_url),
        (github_issues, "record_human_input_context", backend.record_human_input_context),
        (github_issues, "get_human_input_context", backend.get_human_input_context),
        (github_issues, "record_human_answer", backend.record_human_answer),
        (github_issues, "find_unanswered_human_input_comment", backend.find_unanswered_human_input_comment),
        (github_variables, "list_variables", backend.list_variables),
        (github_variables, "set_variable", backend.set_variable),
    ]
    for module, attr, fn in patches:
        if monkeypatch is not None:
            monkeypatch.setattr(module, attr, fn)
        else:
            setattr(module, attr, fn)
    return backend

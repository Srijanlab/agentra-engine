"""memory/features.py — MemoryFeaturesMixin.

Covers the feature backlog lifecycle: queue, in-progress, shipped, released,
and the record_shipped branching logic (simple single-call, multi-part, sub-issue).
"""

from __future__ import annotations

import datetime as dt
import json
import logging

logger = logging.getLogger(__name__)

from agentra.memory.core import (
    _AGENTRA_LABEL,
    _FEATURE_LABEL,
    _STATUS_SHIPPED_LABEL,
    _STORY_LABEL,
    _github_feature_to_dict,
    _github_shipped_to_dict,
    _label_names,
)


class MemoryFeaturesMixin:
    """Mixin for Memory: feature queue, shipped/released records, and the full
    record_shipped branching logic. Assumes self._repo_url() and self._find_similar_open()
    are defined on the base class (the latter comes from MemoryIssuesMixin)."""

    def feature_queue(self) -> list[dict]:
        """Open 'feature'-labeled issues not yet shipped — excludes ones
        already stamped 'status:shipped' (implemented, awaiting promotion)."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("feature_queue: %s has no github.com remote -- no feature backlog is visible at all", self.repo)
            return []
        try:
            from agentra.connectors import github_issues

            issues = github_issues.list_open_issues(repo_url, labels=[_FEATURE_LABEL, _AGENTRA_LABEL])
            issues = [i for i in issues if _STATUS_SHIPPED_LABEL not in _label_names(i)]
            return [_github_feature_to_dict(i) for i in issues]
        except Exception:
            logger.error("feature_queue: GitHub Issues unavailable for %s -- feature backlog is unreadable until it recovers", repo_url, exc_info=True)
            return []

    def in_progress_features(self) -> list[dict]:
        """Open 'feature'-labeled issues that already have at least one
        COMPLETED sub-issue — meaning real work has started, not just that
        sub-issues exist (a feature can be broken into planned sub-issues
        with zero of them completed). Confirmed live: issue #2 sat with two
        open, unstarted sub-issues and still got surfaced here ahead of real bugs."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("in_progress_features: %s has no github.com remote -- no in-progress features are visible", self.repo)
            return []
        try:
            from agentra.connectors import github_issues

            issues = github_issues.list_in_progress_features(repo_url, labels=[_FEATURE_LABEL, _AGENTRA_LABEL])
            return [
                {
                    "description": i["title"],
                    "external_id": str(i["number"]),
                    "sub_issues_total": i["sub_issues_total"],
                    "sub_issues_completed": i["sub_issues_completed"],
                    "html_url": i.get("html_url"),
                }
                for i in issues
                if i["sub_issues_completed"] > 0
            ]
        except Exception:
            logger.error("in_progress_features: GitHub Issues unavailable for %s", repo_url, exc_info=True)
            return []

    def shipped_features(self) -> list[dict]:
        """Each entry: {feature, commit_sha, run_id, session_id, ts, updated_at, external_id, status_done}.
        A shipped feature is open+status:shipped (Ready to Review) or
        closed (Release to Production; status_done=True). Parsed back from
        the body stamp record_shipped writes — no local shipped.json."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("shipped_features: %s has no github.com remote -- shipped history is unreadable", self.repo)
            return []
        try:
            from agentra.connectors import github_issues

            open_shipped = [
                i for i in github_issues.list_open_issues(repo_url, labels=[_FEATURE_LABEL, _AGENTRA_LABEL])
                if _STATUS_SHIPPED_LABEL in _label_names(i)
            ]
            closed = github_issues.list_closed_issues(repo_url, labels=[_FEATURE_LABEL, _AGENTRA_LABEL])
            return [_github_shipped_to_dict(i) for i in open_shipped + closed]
        except Exception:
            logger.error("shipped_features: GitHub Issues unavailable for %s -- shipped history is unreadable until it recovers", repo_url, exc_info=True)
            return []

    def record_shipped(
        self,
        feature: str,
        commit_sha: str | None = None,
        run_id: str | None = None,
        resolves_id: str | None = None,
        sub_feature_of: str | None = None,
        more_parts_expected: bool = False,
        session_id: str | None = None,
        known_bug_issue: str | None = None,
    ) -> dict | None:
        """Records a shipped feature as an open 'feature'-labeled issue stamped
        'status:shipped' — the same ledger feature_queue() and shipped_features()
        read, so pending/shipped/released is just an issue's own state/labels.

        known_bug_issue: set when this call resolves a known bug (not a
        feature_queue item) whose caller is ALSO calling clear_known_bug() on
        the same issue number right after this returns -- that call already
        stamps status:shipped and posts its own resolution comment on the bug
        issue itself, so this just reuses that number as board_issue_number
        without posting a second, redundant "Shipped as ..." comment (unlike
        resolves_id below, which does post one, since nothing else does for
        the feature_queue case). Without this, a known-bug fix had no way to
        tell record_shipped which issue it belongs to, so the fallback path's
        similarity check (comparing the shipped feature's generated title
        against each open bug's original diagnosis text) had to guess --
        confirmed live to miss and create an orphaned duplicate (issue #33,
        which should have matched #21).

        Three paths:
        - sub_feature_of set: continues a multi-part feature (existing parent).
          Creates+closes a sub-issue for this part; marks the parent shipped
          when more_parts_expected=False.
        - sub_feature_of unset, more_parts_expected=True: starts a new multi-part
          feature. resolves_id becomes the parent (or a fresh parent is created).
        - Neither: simple single-call feature. resolves_id (if set) is marked
          shipped directly; otherwise runs a similarity check against open bugs/
          features to avoid creating a duplicate orphan issue (confirmed live —
          three times, issues #13/#16, #1/#19, #6/#15 — implement_feature didn't
          always pass resolves_id).

        session_id: the Claude session that built this feature, stamped as
        Shipped-Session-ID so a later promote can resume it instead of
        starting a disconnected fresh session.

        Returns {issue_number, board_issue_number} on success, None on failure."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("record_shipped: %s has no github.com remote -- shipped feature %r was NOT recorded anywhere", self.repo, feature)
            return None
        note = (
            f"Shipped as {feature!r}"
            + (f" (run {run_id})" if run_id else "")
            + (f" (commit {commit_sha})" if commit_sha else "")
            + "."
        )
        body_suffix = (
            "---\n"
            + (f"Shipped-Run-ID: {run_id}\n" if run_id else "")
            + (f"Shipped-Commit: {commit_sha}\n" if commit_sha else "")
            + (f"Shipped-Session-ID: {session_id}\n" if session_id else "")
        )
        try:
            from agentra.connectors import github_issues

            if sub_feature_of and sub_feature_of.isdigit():
                parent_number = int(sub_feature_of)
                issue = github_issues.create_sub_issue(repo_url, parent_number, feature, "Autonomously shipped by agentra.", labels=[_STORY_LABEL, _AGENTRA_LABEL])
                issue_number = issue["number"]
                github_issues.close_issue(repo_url, issue_number, comment=note, body_suffix=body_suffix)
                if not more_parts_expected:
                    github_issues.mark_shipped(repo_url, parent_number, comment=f"All parts shipped (run {run_id})." if run_id else "All parts shipped.")
                board_issue_number = parent_number

            elif more_parts_expected:
                if resolves_id and resolves_id.isdigit():
                    parent_number = int(resolves_id)
                else:
                    parent_issue = github_issues.create_issue(repo_url, feature, "Tracks a multi-part feature; stays open until every part has shipped.", labels=[_FEATURE_LABEL, _AGENTRA_LABEL])
                    parent_number = parent_issue["number"]
                issue = github_issues.create_sub_issue(repo_url, parent_number, feature, "Autonomously shipped by agentra.", labels=[_STORY_LABEL, _AGENTRA_LABEL])
                issue_number = issue["number"]
                github_issues.close_issue(repo_url, issue_number, comment=note, body_suffix=body_suffix)
                board_issue_number = parent_number

            elif resolves_id and resolves_id.isdigit():
                issue_number = int(resolves_id)
                github_issues.mark_shipped(repo_url, issue_number, comment=note, body_suffix=body_suffix)
                board_issue_number = issue_number

            elif known_bug_issue and known_bug_issue.isdigit():
                # No mark_shipped/comment here -- the caller's own clear_known_bug()
                # call on this same issue number handles that (status:shipped label +
                # its own resolution comment). This branch exists purely so the known
                # issue number is used directly instead of falling through to the
                # fuzzy-match safety net below, which is what actually created #33.
                issue_number = int(known_bug_issue)
                board_issue_number = issue_number

            else:
                # Safety net: implement_feature's caller is supposed to pass resolves_id
                # when this call resolves a known bug or feature-queue item, but confirmed
                # live it doesn't always. A known-bug fix NEVER reaches the resolves_id
                # path (brain.py only forwards it for resolves_origin=="feature_queue").
                # Without this check, the original backlog entry stays open forever while
                # an orphaned fresh issue carries the shipped record.
                duplicate_of = self._find_similar_open(feature, self.known_bugs(), "diagnosis") or self._find_similar_open(feature, self.feature_queue(), "description")
                if duplicate_of and duplicate_of.isdigit():
                    issue_number = int(duplicate_of)
                    github_issues.mark_shipped(repo_url, issue_number, comment=note, body_suffix=body_suffix)
                else:
                    issue = github_issues.create_issue(repo_url, feature, "Autonomously shipped by agentra.", labels=[_FEATURE_LABEL, _AGENTRA_LABEL])
                    issue_number = issue["number"]
                    github_issues.mark_shipped(repo_url, issue_number, comment=note, body_suffix=body_suffix)
                board_issue_number = issue_number

        except Exception:
            logger.error("record_shipped: failed to record shipped feature %r on %s", feature, repo_url, exc_info=True)
            return None

        return {"issue_number": issue_number, "board_issue_number": board_issue_number}

    def released_features(self) -> list[dict]:
        """Each entry: {feature, commit_sha, ts, release_run_id} — the production
        release ledger. Intentionally separate from shipped_features(): shipped
        means 'implemented and in pre-prod'; released means 'made it to prod'.
        Older released.json files that only contain a plain list[str] are normalized."""
        if not self.released_path.exists():
            return []
        raw = json.loads(self.released_path.read_text())
        if not isinstance(raw, list):
            return []
        return [
            {"feature": e, "commit_sha": None, "ts": None, "release_run_id": None}
            if isinstance(e, str)
            else e
            for e in raw
        ]

    def pending_promotion_features(self) -> list[dict]:
        """Shipped features not yet in released_features() -- implemented and
        sitting in pre-prod, awaiting a Promote to reach production."""
        released = {f["feature"] for f in self.released_features()}
        return [f for f in self.shipped_features() if f["feature"] not in released]

    def record_released(self, feature: str, release_run_id: str, commit_sha: str | None = None) -> None:
        released = self.released_features()
        if any(f["feature"] == feature for f in released):
            return
        released.append({
            "feature": feature,
            "commit_sha": commit_sha,
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "release_run_id": release_run_id,
        })
        self.released_path.write_text(json.dumps(released, indent=2))

    def record_feature_request(
        self,
        description: str,
        source: str = "customer",
        external_id: str | None = None,
        title: str | None = None,
        extra_labels: list[str] | None = None,
    ) -> dict | None:
        """`title` (from the dashboard's form) becomes the issue's actual title;
        `description` is recorded as a 'Description: ...' body line (same
        pattern as record_known_bug's title param).

        extra_labels is additive on top of the standard [_FEATURE_LABEL,
        _AGENTRA_LABEL] pair (e.g. discover_opportunities passes
        [core._DISCOVERY_LABEL] -- see that constant's docstring in
        memory/core.py) -- never _BUG_LABEL; a feature request always goes
        through this path, never record_known_bug's.

        Returns {"number": int, "html_url": str} for the created issue, or
        None if it could not be recorded anywhere (no repo_url, or the
        GitHub API call failed)."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("record_feature_request: %s has no github.com remote -- feature %r was NOT recorded anywhere", self.repo, description)
            return None
        try:
            from agentra.connectors import github_issues

            body = f"Description: {description}\n\nSource: {source}"
            if external_id:
                body += f"\n\nExternal-ID: {external_id}"
            labels = [_FEATURE_LABEL, _AGENTRA_LABEL, *(extra_labels or [])]
            issue = github_issues.create_issue(repo_url, title or description, body, labels=labels)
            return {"number": issue.get("number"), "html_url": issue.get("html_url")}
        except Exception:
            logger.error("record_feature_request: failed to create a GitHub issue on %s -- feature %r was NOT recorded anywhere", repo_url, description, exc_info=True)
            return None

    def clear_feature_request(self, external_id: str, resolution_note: str | None = None) -> None:
        if not external_id.isdigit():
            logger.warning("clear_feature_request: %r is not a GitHub issue number, nothing to close", external_id)
            return
        repo_url = self._repo_url()
        if not repo_url:
            return
        try:
            from agentra.connectors import github_issues

            github_issues.close_issue(repo_url, int(external_id), comment=resolution_note or "Resolved by agentra.")
        except Exception:
            logger.warning("clear_feature_request: failed to close GitHub issue #%s on %s", external_id, repo_url, exc_info=True)

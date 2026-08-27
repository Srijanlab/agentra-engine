"""memory/features.py — MemoryFeaturesMixin."""

from __future__ import annotations

import datetime as dt
import json
import logging

logger = logging.getLogger(__name__)

from agentra.memory.core import (
    _AGENTRA_LABEL,
    _BUG_LABEL,
    _FEATURE_LABEL,
    _NEED_HUMAN_LABEL,
    _STATUS_CODE_COMPLETE_LABEL,
    _STATUS_PROGRESS_LABELS,
    _STATUS_SHIPPED_LABEL,
    _STATUS_TESTED_LABEL,
    _STORY_LABEL,
    _github_bug_to_dict,
    _github_feature_to_dict,
    _github_shipped_to_dict,
    _label_names,
)


class MemoryFeaturesMixin:
    """Mixin for Memory: feature queue, shipped/released records, and the full record_shipped branching logic."""

    def _items_at_stage(self, status_label: str) -> list[dict]:
        """Open bug- or feature-labeled issues (either type -- the priority order in
        check_backlog cares about pipeline stage, not bug vs. feature) carrying the
        given status label. Each entry gets a 'kind' field ('bug' or 'feature')."""
        repo_url = self._repo_url()
        if not repo_url:
            return []
        try:
            from agentra.connectors import github_issues

            bugs = github_issues.list_open_issues(repo_url, labels=[_BUG_LABEL, _AGENTRA_LABEL])
            features = github_issues.list_open_issues(repo_url, labels=[_FEATURE_LABEL, _AGENTRA_LABEL])
            result = []
            for i in bugs:
                names = _label_names(i)
                if status_label in names and _NEED_HUMAN_LABEL not in names:
                    entry = _github_bug_to_dict(i)
                    entry["kind"] = "bug"
                    result.append(entry)
            for i in features:
                if status_label in _label_names(i):
                    entry = _github_feature_to_dict(i)
                    entry["kind"] = "feature"
                    result.append(entry)
            return result
        except Exception:
            logger.error("_items_at_stage(%r): GitHub Issues unavailable for %s", status_label, repo_url, exc_info=True)
            return []

    def code_complete_items(self) -> list[dict]:
        """Bugs/features stamped status:code_complete -- pushed, not yet merged to pre-prod."""
        return self._items_at_stage(_STATUS_CODE_COMPLETE_LABEL)

    def shipped_pending_test_items(self) -> list[dict]:
        """Bugs/features stamped status:shipped (merged to pre-prod, not yet live-verified) -- excludes status:tested, which is a later stage."""
        return self._items_at_stage(_STATUS_SHIPPED_LABEL)

    def feature_queue(self) -> list[dict]:
        """Open 'feature'-labeled issues not yet started on -- excludes ones
        already stamped with any forward-progress status label (code_complete
        and later; see _STATUS_PROGRESS_LABELS)."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("feature_queue: %s has no github.com remote -- no feature backlog is visible at all", self.repo)
            return []
        try:
            from agentra.connectors import github_issues

            issues = github_issues.list_open_issues(repo_url, labels=[_FEATURE_LABEL, _AGENTRA_LABEL])
            issues = [i for i in issues if not any(label in _label_names(i) for label in _STATUS_PROGRESS_LABELS)]
            return [_github_feature_to_dict(i) for i in issues]
        except Exception:
            logger.error("feature_queue: GitHub Issues unavailable for %s -- feature backlog is unreadable until it recovers", repo_url, exc_info=True)
            return []

    def in_progress_features(self) -> list[dict]:
        """Open 'feature'-labeled issues that already have at least one COMPLETED sub-issue — meaning real work has started, not just that sub-issues exist (a feature can be broken into planned sub-issues with zero of them completed)."""
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
        """Each entry: {feature, commit_sha, run_id, session_id, ts, updated_at, external_id, status_done}."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("shipped_features: %s has no github.com remote -- shipped history is unreadable", self.repo)
            return []
        try:
            from agentra.connectors import github_issues

            open_shipped = [
                i for i in github_issues.list_open_issues(repo_url, labels=[_FEATURE_LABEL, _AGENTRA_LABEL])
                if _STATUS_SHIPPED_LABEL in _label_names(i) or _STATUS_TESTED_LABEL in _label_names(i)
            ]
            closed = github_issues.list_closed_issues(repo_url, labels=[_FEATURE_LABEL, _AGENTRA_LABEL])
            return [_github_shipped_to_dict(i) for i in open_shipped + closed]
        except Exception:
            logger.error("shipped_features: GitHub Issues unavailable for %s -- shipped history is unreadable until it recovers", repo_url, exc_info=True)
            return []

    def record_code_complete(
        self,
        feature: str,
        commit_sha: str | None = None,
        run_id: str | None = None,
        resolves_id: str | None = None,
        sub_feature_of: str | None = None,
        more_parts_expected: bool = False,
        session_id: str | None = None,
        known_bug_issue: str | None = None,
        branch: str | None = None,
    ) -> dict | None:
        """Records code-complete work (implemented, committed, pushed -- not yet merged anywhere) as an open 'feature'-labeled issue stamped 'status:code_complete' — the same ledger feature_queue()/code_complete_features() etc. read, so each stage is just an issue's own state/labels."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("record_code_complete: %s has no github.com remote -- code-complete feature %r was NOT recorded anywhere", self.repo, feature)
            return None
        note = (
            f"Code complete: {feature!r}"
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
                issue = github_issues.create_sub_issue(repo_url, parent_number, feature, "Autonomously implemented by agentra.", labels=[_STORY_LABEL, _AGENTRA_LABEL])
                issue_number = issue["number"]
                github_issues.close_issue(repo_url, issue_number, comment=note, body_suffix=body_suffix)
                if not more_parts_expected:
                    github_issues.mark_code_complete(repo_url, parent_number, comment=f"All parts code complete (run {run_id})." if run_id else "All parts code complete.")
                board_issue_number = parent_number

            elif more_parts_expected:
                if resolves_id and resolves_id.isdigit():
                    parent_number = int(resolves_id)
                elif branch and (existing := github_issues.find_tracking_issue_for_branch(repo_url, branch, _AGENTRA_LABEL)) is not None:
                    # GitHub issue #97 (and #85/#89 before it): a resumed implement_feature call
                    # that doesn't pass resolves_id back used to always create a brand-new parent
                    # tracking issue here, orphaning the real one -- confirmed live, issue #92's
                    # actual fix landed under a new #97 instead of #92 itself. `branch` is a
                    # structural identity check (which issue's In-Progress-Branch marker already
                    # names this exact branch), not fuzzy text similarity.
                    parent_number = existing
                else:
                    parent_issue = github_issues.create_issue(repo_url, feature, "Tracks a multi-part feature; stays open until every part has shipped.", labels=[_FEATURE_LABEL, _AGENTRA_LABEL])
                    parent_number = parent_issue["number"]
                issue = github_issues.create_sub_issue(repo_url, parent_number, feature, "Autonomously implemented by agentra.", labels=[_STORY_LABEL, _AGENTRA_LABEL])
                issue_number = issue["number"]
                github_issues.close_issue(repo_url, issue_number, comment=note, body_suffix=body_suffix)
                board_issue_number = parent_number

            elif resolves_id and resolves_id.isdigit():
                issue_number = int(resolves_id)
                github_issues.mark_code_complete(repo_url, issue_number, comment=note, body_suffix=body_suffix)
                board_issue_number = issue_number

            elif known_bug_issue and known_bug_issue.isdigit():
                # No mark_code_complete/comment here -- the caller's own clear_known_bug()
                issue_number = int(known_bug_issue)
                board_issue_number = issue_number

            else:
                # Safety net: implement_feature's caller is supposed to pass resolves_id
                duplicate_of = self._find_similar_open(feature, self.known_bugs(), "diagnosis") or self._find_similar_open(feature, self.feature_queue(), "description")
                if duplicate_of and duplicate_of.isdigit():
                    issue_number = int(duplicate_of)
                    github_issues.mark_code_complete(repo_url, issue_number, comment=note, body_suffix=body_suffix)
                else:
                    issue = github_issues.create_issue(repo_url, feature, "Autonomously implemented by agentra.", labels=[_FEATURE_LABEL, _AGENTRA_LABEL])
                    issue_number = issue["number"]
                    github_issues.mark_code_complete(repo_url, issue_number, comment=note, body_suffix=body_suffix)
                board_issue_number = issue_number

        except Exception:
            logger.error("record_code_complete: failed to record code-complete feature %r on %s", feature, repo_url, exc_info=True)
            return None

        return {"issue_number": issue_number, "board_issue_number": board_issue_number}

    def record_shipped_to_preprod(self, issue_numbers: list[str], run_id: str | None = None) -> list[str]:
        """Transitions each code-complete issue (bug or feature, same label either way) to status:shipped -- called once deploy_pre_prod has actually merged their branch into pre-prod/beta. Returns the ones that succeeded."""
        repo_url = self._repo_url()
        if not repo_url:
            return []
        moved = []
        try:
            from agentra.connectors import github_issues

            for id_ in issue_numbers:
                if not id_.isdigit():
                    continue
                try:
                    github_issues.mark_shipped_to_preprod(
                        repo_url, int(id_),
                        comment=f"Merged to pre-prod (run {run_id})." if run_id else "Merged to pre-prod.",
                    )
                    moved.append(id_)
                except Exception:
                    logger.warning("record_shipped_to_preprod: failed for issue #%s on %s", id_, repo_url, exc_info=True)
        except Exception:
            logger.error("record_shipped_to_preprod: failed for %s", repo_url, exc_info=True)
        return moved

    def record_tested(self, issue_numbers: list[str], run_id: str | None = None) -> list[str]:
        """Transitions each shipped issue to status:tested -- called once verify_pre_prod has passed against the live pre-prod deployment. Returns the ones that succeeded."""
        repo_url = self._repo_url()
        if not repo_url:
            return []
        moved = []
        try:
            from agentra.connectors import github_issues

            for id_ in issue_numbers:
                if not id_.isdigit():
                    continue
                try:
                    github_issues.mark_tested(
                        repo_url, int(id_),
                        comment=f"Live-verified in pre-prod (run {run_id})." if run_id else "Live-verified in pre-prod.",
                    )
                    moved.append(id_)
                except Exception:
                    logger.warning("record_tested: failed for issue #%s on %s", id_, repo_url, exc_info=True)
        except Exception:
            logger.error("record_tested: failed for %s", repo_url, exc_info=True)
        return moved

    def released_features(self) -> list[dict]:
        """Each entry: {feature, commit_sha, ts, release_run_id} — the production release ledger."""
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
        """`title` (from the dashboard's form) becomes the issue's actual title; `description` is recorded as a 'Description: ...' body line (same pattern as record_known_bug's title param)."""
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

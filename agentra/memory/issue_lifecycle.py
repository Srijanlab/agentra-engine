"""memory/issue_lifecycle.py — MemoryIssueLifecycleMixin."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from agentra.memory.core import _STATUS_DONE_LABEL, _STATUS_IN_PROGRESS_LABEL, _NEED_HUMAN_LABEL


class MemoryIssueLifecycleMixin:
    """Mixin for Memory: everything tracked on an issue once it's already
    been filed and is being worked -- branch/spec/commit/run markers and the
    needs_human round trip. Assumes self._repo_url() is defined on the base class."""

    def record_human_input_context(
        self,
        issue_number: int,
        *,
        app: str,
        run_id: str,
        question: str,
        branch: str | None = None,
        session_id: str | None = None,
        tracking_issue: int | None = None,
    ) -> None:
        """Stamps resume-correlation data (app id, run id, branch, session_id, the original tracking issue, the original question) onto a needs_human issue that record_known_bug just filed/updated -- the same post-and-read-most-recent comment pattern as record_in_progress_branch/record_spec, so the needs_human GitHub issue stays the single source of truth for blocked-run state (per architecture review) rather than a new database collection."""
        repo_url = self._repo_url()
        if not repo_url:
            return
        try:
            from agentra.connectors import github_issues

            github_issues.record_human_input_context(
                repo_url, issue_number, app=app, run_id=run_id, branch=branch,
                session_id=session_id, question=question, tracking_issue=tracking_issue,
            )
        except Exception:
            logger.warning("record_human_input_context: failed for issue #%s on %s", issue_number, repo_url, exc_info=True)

    def get_human_input_context(self, issue_number: int) -> dict | None:
        """The most recently stamped resume-correlation data for a needs_human issue, or None."""
        repo_url = self._repo_url()
        if not repo_url:
            return None
        try:
            from agentra.connectors import github_issues

            return github_issues.get_human_input_context(repo_url, issue_number)
        except Exception:
            return None

    def record_human_answer(self, issue_number: int, answer: str, resumed_run_key: str | None = None) -> None:
        """Comments the human's answer onto the needs_human issue and removes the need_human label (GitHub issue is updated to reflect it was answered) -- called once a dashboard answer submission has been accepted and a resume dispatched."""
        repo_url = self._repo_url()
        if not repo_url:
            return
        try:
            from agentra.connectors import github_issues

            github_issues.record_human_answer(repo_url, issue_number, answer, resumed_run_key)
            github_issues.remove_label(repo_url, issue_number, _NEED_HUMAN_LABEL)
        except Exception:
            logger.warning("record_human_answer: failed for issue #%s on %s", issue_number, repo_url, exc_info=True)

    def human_input_pending(self, issue_number: int) -> bool:
        """True while a needs_human issue still carries the need_human label -- no answer
        has been recorded yet through any channel (dashboard, Slack, GitHub comment).
        record_human_answer removes that label, so a second answer arriving afterwards
        (e.g. a Slack reply landing just after a dashboard answer) sees False here."""
        repo_url = self._repo_url()
        if not repo_url:
            return True
        try:
            from agentra.connectors import github_issues

            issue = github_issues.get_issue(repo_url, issue_number)
            return issue is not None and _NEED_HUMAN_LABEL in (issue.get("labels") or [])
        except Exception:
            return True

    def escalate_existing_issue(self, issue_number: int, run_id: str, full_diagnosis: str) -> None:
        """Escalates directly on the issue already tracking this work (comments the blocking question, adds the needs_human label) instead of filing a separate needs_human issue -- work that already has a home (a feature/bug issue) should never spawn a duplicate for a blocking question about that same work (confirmed live: issues #79, #80, #81, same interrupted item, three separate escalation issues)."""
        repo_url = self._repo_url()
        if not repo_url:
            return
        try:
            from agentra.connectors import github_issues

            github_issues.escalate_existing_issue(repo_url, issue_number, run_id, full_diagnosis, [_NEED_HUMAN_LABEL])
        except Exception:
            logger.warning("escalate_existing_issue: failed for issue #%s on %s", issue_number, repo_url, exc_info=True)

    def issue_html_url(self, issue_number: int) -> str | None:
        """The browsable GitHub URL for an issue, or None if this repo has no github.com remote."""
        repo_url = self._repo_url()
        if not repo_url:
            return None
        try:
            from agentra.connectors import github_issues

            return github_issues.issue_html_url(repo_url, issue_number)
        except Exception:
            return None

    def find_unanswered_human_input_comment(self, issue_number: int) -> str | None:
        """The human's answer to a needs_human issue's blocking question, if one has been posted as a plain GitHub comment since the question was filed -- the polling-based half of the GitHub-issue-comment resume channel (see github_issue_lifecycle.py's find_unanswered_human_input_comment for the matching heuristic; there is no inbound webhook receiving these)."""
        repo_url = self._repo_url()
        if not repo_url:
            return None
        try:
            from agentra.connectors import github_issues

            return github_issues.find_unanswered_human_input_comment(repo_url, issue_number)
        except Exception:
            return None

    def record_in_progress_branch(
        self, issue_number: int, branch: str, run_id: str | None = None, session_id: str | None = None
    ) -> None:
        """Also adds status:in-progress label — real work has demonstrably started.
        Best-effort: failure just means a future cycle won't be offered a resume."""
        repo_url = self._repo_url()
        if not repo_url:
            return
        try:
            from agentra.connectors import github_issues

            github_issues.record_in_progress_branch(repo_url, issue_number, branch, run_id, session_id)
            github_issues.add_labels(repo_url, issue_number, [_STATUS_IN_PROGRESS_LABEL])
        except Exception:
            logger.warning("record_in_progress_branch: failed for issue #%s on %s", issue_number, repo_url, exc_info=True)

    def mark_status_done(self, issue_number: int) -> None:
        """Stamps status:done and closes the issue — called once something actually reaches production (server.py's _record_production_release)."""
        repo_url = self._repo_url()
        if not repo_url:
            return
        try:
            from agentra.connectors import github_issues

            github_issues.add_labels(repo_url, issue_number, [_STATUS_DONE_LABEL])
            github_issues.close_issue(repo_url, issue_number, comment="Released to production.")
        except Exception:
            logger.warning("mark_status_done: failed for issue #%s on %s", issue_number, repo_url, exc_info=True)

    def record_failure_on_issue(self, issue_number: int, run_id: str, step_name: str, text: str) -> None:
        """Like record_failure, but for a failure that happened while working an already-known tracking issue (resolves_id/sub_feature_of was set on the implement_feature call) -- posts the failure as a comment on THAT issue instead of filing a brand-new, disconnected "X failed during an autonomous cycle" bug report."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("record_failure_on_issue: %s has no github.com remote -- failure was NOT recorded anywhere", self.repo)
            return
        try:
            from agentra.connectors import github_issues

            github_issues.add_comment(
                repo_url, issue_number, f"{step_name} failed (run {run_id}) while working this issue:\n\n{text[:2000]}"
            )
        except Exception:
            logger.warning("record_failure_on_issue: failed to comment on issue #%s on %s", issue_number, repo_url, exc_info=True)

    def record_commit(self, issue_number: int, commit_sha: str) -> None:
        """Links commit_sha on this issue — a tracking issue's work can span
        more than one commit; this is the only place the full history is visible."""
        repo_url = self._repo_url()
        if not repo_url:
            return
        try:
            from agentra.connectors import github_issues

            github_issues.record_commit(repo_url, issue_number, commit_sha)
        except Exception:
            logger.warning("record_commit: failed for issue #%s on %s", issue_number, repo_url, exc_info=True)

    def resume_branch_for(self, external_id: str) -> str | None:
        """The branch an interrupted implement_feature call pushed work to, if any."""
        if not external_id.isdigit():
            return None
        repo_url = self._repo_url()
        if not repo_url:
            return None
        try:
            from agentra.connectors import github_issues

            return github_issues.get_in_progress_branch(repo_url, int(external_id))
        except Exception:
            return None

    def shipped_commit_for(self, external_id: str) -> str | None:
        """The most recent Shipped-Commit recorded on an issue body, if any."""
        if not str(external_id).isdigit():
            return None
        repo_url = self._repo_url()
        if not repo_url:
            return None
        try:
            from agentra.connectors import github_issues
            from agentra.memory.core import _SHIPPED_COMMIT_RE

            issue = github_issues.get_issue(repo_url, int(external_id)) or {}
            shas = _SHIPPED_COMMIT_RE.findall(issue.get("body") or "")
            return shas[-1].strip() if shas else None
        except Exception:
            return None

    def resume_run_id_for(self, external_id: str) -> str | None:
        """The run_id that pushed resume_branch_for's branch. Informational
        (look up that run's log for context on what was already tried)."""
        if not external_id.isdigit():
            return None
        repo_url = self._repo_url()
        if not repo_url:
            return None
        try:
            from agentra.connectors import github_issues

            return github_issues.get_in_progress_run_id(repo_url, int(external_id))
        except Exception:
            return None

    def run_ids_for(self, external_id: str) -> list[str]:
        """Every run_id that has ever worked this issue, newest-first -- the dashboard's
        Backlog/Ready to Review tabs show this per item (1:N), not just the single most-recent
        run resume_run_id_for returns."""
        if not external_id.isdigit():
            return []
        repo_url = self._repo_url()
        if not repo_url:
            return []
        try:
            from agentra.connectors import github_issues

            return github_issues.list_run_ids_for_issue(repo_url, int(external_id))
        except Exception:
            return []

    def resume_session_id_for(self, external_id: str) -> str | None:
        """The Claude session_id an interrupted issue's build was using, if..."""
        if not external_id.isdigit():
            return None
        repo_url = self._repo_url()
        if not repo_url:
            return None
        try:
            from agentra.connectors import github_issues

            return github_issues.get_in_progress_session_id(repo_url, int(external_id))
        except Exception:
            return None

    def record_spec(self, issue_number: int, spec: dict) -> None:
        """Persists the spec as a comment — best-effort: failure means a
        resumed cycle regenerates the spec instead of reusing it."""
        repo_url = self._repo_url()
        if not repo_url:
            return
        try:
            from agentra.connectors import github_issues

            github_issues.record_spec(repo_url, issue_number, spec)
        except Exception:
            logger.warning("record_spec: failed for issue #%s on %s", issue_number, repo_url, exc_info=True)

    def get_spec(self, issue_number: int) -> dict | None:
        """Most recently recorded spec for this issue, or None."""
        repo_url = self._repo_url()
        if not repo_url:
            return None
        try:
            from agentra.connectors import github_issues

            return github_issues.get_spec(repo_url, issue_number)
        except Exception:
            return None

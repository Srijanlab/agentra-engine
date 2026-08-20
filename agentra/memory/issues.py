"""memory/issues.py — MemoryIssuesMixin.

Covers known-bug and failure recording, blocking-bug detection, and the
in-progress/commit/spec tracking that backs an issue's lifecycle through
record_in_progress_branch → record_commit → record_spec → (mark_status_done).
"""

from __future__ import annotations

import difflib
import logging

logger = logging.getLogger(__name__)

from agentra.memory.core import (
    _AGENTRA_LABEL,
    _BLOCKING_AGENTRA_LABEL,
    _BUG_LABEL,
    _NEED_HUMAN_LABEL,
    _STATUS_DONE_LABEL,
    _STATUS_IN_PROGRESS_LABEL,
    _STATUS_SHIPPED_LABEL,
    _github_bug_to_dict,
    _github_closed_bug_to_dict,
    _label_names,
    cannot_be_fixed_by_agentra,
    is_login_required_failure,
    is_transient_failure,
)


class MemoryIssuesMixin:
    """Mixin for Memory: bug filing, lifecycle tracking (branch/spec/commit),
    and failure triage. Assumes self._repo_url() is defined on the base class."""

    _DUPLICATE_BUG_SIMILARITY_THRESHOLD = 0.6

    def known_bugs(self) -> list[dict]:
        """Open 'bug'-labeled issues still needing a fix — excludes ones
        already stamped 'status:shipped' by clear_known_bug (fixed,
        awaiting production promotion; see closed_bugs())."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("known_bugs: %s has no github.com remote -- no bug backlog is visible at all", self.repo)
            return []
        try:
            from agentra.connectors import github_issues

            issues = github_issues.list_open_issues(repo_url, labels=[_BUG_LABEL, _AGENTRA_LABEL])
            issues = [i for i in issues if _STATUS_SHIPPED_LABEL not in _label_names(i)]
            return [_github_bug_to_dict(i) for i in issues]
        except Exception:
            logger.error("known_bugs: GitHub Issues unavailable for %s -- bug backlog is unreadable until it recovers", repo_url, exc_info=True)
            return []

    def closed_bugs(self) -> list[dict]:
        """Resolved bugs — open+status:shipped (fixed, awaiting promotion)
        or closed (already promoted; status_done True)."""
        repo_url = self._repo_url()
        if not repo_url:
            return []
        try:
            from agentra.connectors import github_issues

            open_shipped = [
                i for i in github_issues.list_open_issues(repo_url, labels=[_BUG_LABEL, _AGENTRA_LABEL])
                if _STATUS_SHIPPED_LABEL in _label_names(i)
            ]
            closed = github_issues.list_closed_issues(repo_url, labels=[_BUG_LABEL, _AGENTRA_LABEL])
            return [_github_closed_bug_to_dict(i) for i in open_shipped + closed]
        except Exception:
            logger.error("closed_bugs: GitHub Issues unavailable for %s", repo_url, exc_info=True)
            return []

    def blocking_bugs(self) -> list[dict]:
        """Open bugs labeled 'blocking_agentra' — run_autonomous_cycle
        checks this before starting a cycle; non-empty means agentra
        should not attempt anything until a human resolves it."""
        return [b for b in self.known_bugs() if b.get("blocking_agentra")]

    def _find_similar_open(self, text: str, candidates: list[dict], field: str) -> str | None:
        """difflib similarity (not exact match) since the text is LLM-generated
        free text that varies call to call even when describing the same thing."""
        for candidate in candidates:
            existing = candidate.get(field) or ""
            if difflib.SequenceMatcher(None, text, existing).ratio() >= self._DUPLICATE_BUG_SIMILARITY_THRESHOLD:
                return candidate.get("external_id")
        return None

    def _find_similar_open_bug(self, diagnosis: str) -> str | None:
        """Best-effort duplicate suppression: record_failure() fires on every
        non-transient failure, and an unfixed real bug produces the same
        failure on every retry — without this, each cycle filed its own fresh
        issue for the identical problem. Confirmed live: 4 near-identical
        '403 Write access to repository not granted' bugs (#7-#10) from four
        consecutive cycles hitting the same broken code path."""
        return self._find_similar_open(diagnosis, self.known_bugs(), "diagnosis")

    def record_known_bug(
        self,
        run_id: str,
        severity: str,
        diagnosis: str,
        proposed_fix: str,
        source: str = "prod-monitoring",
        external_id: str | None = None,
        needs_human: bool = False,
        blocking_agentra: bool = False,
        title: str | None = None,
    ) -> int | None:
        """If a similar bug is already open, adds a comment instead of filing
        a duplicate — and applies needs_human/blocking_agentra labels to
        THAT issue (GitHub's add-labels endpoint is additive), since the
        original occurrence might not have been recognized as unfixable.

        Returns the issue number (the duplicate's, or the freshly created
        one's) so a HUMAN_INPUT_REQUIRED escalation can stamp resume
        correlation data onto it via record_human_input_context -- or None
        if GitHub is unreachable/has no remote, matching every other
        best-effort write in this module."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("record_known_bug: %s has no github.com remote -- bug %r was NOT recorded anywhere", self.repo, diagnosis)
            return None
        extra_labels = ([_NEED_HUMAN_LABEL] if needs_human else []) + ([_BLOCKING_AGENTRA_LABEL] if blocking_agentra else [])
        try:
            from agentra.connectors import github_issues

            duplicate_of = self._find_similar_open_bug(diagnosis)
            if duplicate_of and duplicate_of.isdigit():
                github_issues.add_comment(repo_url, int(duplicate_of), f"Still occurring (run {run_id}, source: {source}).\n\n{diagnosis}")
                if extra_labels:
                    github_issues.add_labels(repo_url, int(duplicate_of), extra_labels)
                logger.info("record_known_bug: run %s matched existing open bug #%s -- commented instead of filing a duplicate", run_id, duplicate_of)
                return int(duplicate_of)

            body = f"Description: {diagnosis}\n\nSeverity: {severity}\nSource: {source}\n\nProposed fix:\n{proposed_fix}"
            if external_id:
                body += f"\n\nExternal-ID: {external_id}"
            created = github_issues.create_issue(repo_url, title or diagnosis, body, labels=[_BUG_LABEL, _AGENTRA_LABEL, *extra_labels])
            return created.get("number")
        except Exception:
            logger.error("record_known_bug: failed to create a GitHub issue on %s -- bug %r was NOT recorded anywhere", repo_url, diagnosis, exc_info=True)
            return None

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
        """Stamps resume-correlation data (app id, run id, branch, session_id,
        the original tracking issue, the original question) onto a
        needs_human issue that record_known_bug just filed/updated -- the
        same post-and-read-most-recent comment pattern as
        record_in_progress_branch/record_spec, so the needs_human GitHub
        issue stays the single source of truth for blocked-run state (per
        architecture review) rather than a new database collection.
        Best-effort: a failure here just means a resume has to fall back to
        whatever the run record in the dashboard's own registry still has."""
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
        """The most recently stamped resume-correlation data for a
        needs_human issue, or None. Read back by a future GitHub-comment-
        driven resume path (not built in this pass -- see
        .agentra/memory/architecture/design.md)."""
        repo_url = self._repo_url()
        if not repo_url:
            return None
        try:
            from agentra.connectors import github_issues

            return github_issues.get_human_input_context(repo_url, issue_number)
        except Exception:
            return None

    def record_human_answer(self, issue_number: int, answer: str, resumed_run_key: str | None = None) -> None:
        """Comments the human's answer onto the needs_human issue and removes
        the need_human label (GitHub issue is updated to reflect it was
        answered) -- called once a dashboard answer submission has been
        accepted and a resume dispatched. Best-effort, same as every other
        issue-lifecycle write here."""
        repo_url = self._repo_url()
        if not repo_url:
            return
        try:
            from agentra.connectors import github_issues

            github_issues.record_human_answer(repo_url, issue_number, answer, resumed_run_key)
            github_issues.remove_label(repo_url, issue_number, _NEED_HUMAN_LABEL)
        except Exception:
            logger.warning("record_human_answer: failed for issue #%s on %s", issue_number, repo_url, exc_info=True)

    def issue_html_url(self, issue_number: int) -> str | None:
        """The browsable GitHub URL for an issue, or None if this repo has
        no github.com remote. Used to build the GitHub-issue link in a
        HUMAN_INPUT_REQUIRED Slack notification and the dashboard's 'Needs
        your input' panel -- no live API call, just owner/repo/number."""
        repo_url = self._repo_url()
        if not repo_url:
            return None
        try:
            from agentra.connectors import github_issues

            return github_issues.issue_html_url(repo_url, issue_number)
        except Exception:
            return None

    def find_unanswered_human_input_comment(self, issue_number: int) -> str | None:
        """The human's answer to a needs_human issue's blocking question, if
        one has been posted as a plain GitHub comment since the question was
        filed -- the polling-based half of the GitHub-issue-comment resume
        channel (see github_issue_lifecycle.py's
        find_unanswered_human_input_comment for the matching heuristic;
        there is no inbound webhook receiving these). None if the issue has
        no Human-Input-Required marker yet or nothing has been posted since."""
        repo_url = self._repo_url()
        if not repo_url:
            return None
        try:
            from agentra.connectors import github_issues

            return github_issues.find_unanswered_human_input_comment(repo_url, issue_number)
        except Exception:
            return None

    def clear_known_bug(self, id_: str, resolution_note: str | None = None) -> None:
        """Marks status:shipped and leaves the issue OPEN (mark_shipped) —
        it only actually closes once production promotion runs it through
        mark_status_done (server.py's _record_production_release)."""
        if not id_.isdigit():
            logger.warning("clear_known_bug: %r is not a GitHub issue number, nothing to mark shipped", id_)
            return
        repo_url = self._repo_url()
        if not repo_url:
            return
        try:
            from agentra.connectors import github_issues

            github_issues.mark_shipped(repo_url, int(id_), comment=resolution_note or "Resolved by agentra.")
        except Exception:
            logger.warning("clear_known_bug: failed to mark GitHub issue #%s shipped on %s", id_, repo_url, exc_info=True)

    def clear_resolved_auth_bugs(self, run_id: str) -> list[str]:
        """GitHub issue #42 hardening: a Claude Code CLI auth/login-failure
        blocking bug is unlike every other blocking_agentra bug (e.g. a
        missing GitHub permission) in one important way -- there's a cheap,
        unambiguous way to confirm it's actually resolved: just get one
        real Claude Code CLI turn back with no auth failure. Every other
        unfixable-by-agentra bug stays blocking until a human explicitly
        closes/clears the issue on GitHub, because agentra has no reliable
        way to verify THOSE are fixed on its own; this one it does.

        Without this, once filed, an auth-failure bug stayed open forever
        (nothing else in this codebase ever called clear_known_bug on it --
        implement_feature can't "implement a fix" for a human needing to
        run `claude /login`), so it kept surfacing as the same still-open
        blocking bug on every future scheduled trigger indefinitely even
        after a human had already fixed the underlying credentials and
        simply hadn't also remembered to separately close the GitHub issue
        by hand -- confirmed to be exactly why GitHub issue #42 itself kept
        resurfacing despite two prior shipped fixes that only handled
        detection/escalation, not this closing-the-loop step.

        Callers (run_autonomous_cycle, orchestrator.run_cycle) must only
        call this after confirming *this exact run* actually got a real,
        successful Claude Code CLI turn back with no auth failure of its
        own this run -- see their own call sites for that guard; this
        method itself does not re-verify anything, it just clears whatever
        is currently open and auth-classified.

        Returns the external_id (GitHub issue number as str) of each bug
        cleared, for logging/tests."""
        cleared: list[str] = []
        for bug in self.blocking_bugs():
            if is_login_required_failure(f"{bug.get('diagnosis', '')}\n{bug.get('proposed_fix', '')}"):
                self.clear_known_bug(
                    bug["external_id"],
                    resolution_note=(
                        f"Auto-resolved by run {run_id}: a subsequent autonomous cycle completed a "
                        "Claude Code CLI call successfully, confirming re-authentication worked. "
                        "Closing so this doesn't keep blocking or resurfacing on future cycles."
                    ),
                )
                cleared.append(bug["external_id"])
        return cleared

    def record_failure(self, run_id: str, step_name: str, text: str, severity: str = "high") -> None:
        """The one place a failed agent turn's full output should be reported to.
        Transient failures are just logged; permanent failures become GitHub Issues.

        GitHub issue #42: a Claude Code CLI auth/login failure additionally
        gets an active Slack escalation (the same human-in-the-loop channel
        GitHub issue #34 built for HUMAN_INPUT_REQUIRED decisions -- see
        connectors/slack.py) with a clear, actionable message, since a human
        needs to actually go run `claude /login` before anything else can
        proceed -- filing the GitHub issue alone is easy to miss. Only sent
        for a genuinely new occurrence (diagnosis not already matching an
        open bug -- see _find_similar_open_bug below): once reported, this
        guards against re-notifying for every subsequent occurrence of the
        *same* unresolved failure -- both the (bounded, same-cycle) case of
        more than one tool call hitting it before that cycle's own
        hard-stop takes effect, AND every later cycle's own self-test retry
        (run_autonomous_cycle no longer hard-blocks pre-flight purely on an
        open auth-classified blocking bug -- see clear_resolved_auth_bugs
        below for why one more real attempt is safe/cheap here) hitting it
        again while a human hasn't actually fixed credentials yet."""
        if is_transient_failure(text):
            self.log(run_id, f"{step_name} failed (transient, not filed as a bug): {text[:200]}")
            return
        unfixable = cannot_be_fixed_by_agentra(text)
        diagnosis = f"{step_name} failed during an autonomous cycle"
        auth_failure = is_login_required_failure(text)
        already_reported = auth_failure and self._find_similar_open_bug(diagnosis) is not None
        issue_number = self.record_known_bug(
            run_id, severity, diagnosis, text[:2000],
            source="autonomous-failure", needs_human=unfixable, blocking_agentra=unfixable,
        )
        if auth_failure and not already_reported:
            self._notify_claude_code_auth_failure(run_id, issue_number)

    def _notify_claude_code_auth_failure(self, run_id: str, issue_number: int | None) -> None:
        """GitHub issue #42: best-effort outbound Slack notification for a
        Claude Code CLI auth/login failure, reusing GitHub issue #34's
        connectors/slack.py -- silently skipped (never raised) if Slack
        isn't configured for this deployment, same as every other call to
        notify_human_input_required."""
        try:
            from agentra import urls
            from agentra.connectors import slack

            issue_url = self.issue_html_url(issue_number) if issue_number is not None else None
            slack.notify_human_input_required(
                app=self.repo.name,
                run_id=run_id,
                question=(
                    "Claude Code session is not authenticated on this runner -- "
                    "run /login and re-trigger."
                ),
                issue_url=issue_url,
                dashboard_url=urls.dashboard_run_url(run_id, self.repo.name),
            )
        except Exception:
            logger.warning("_notify_claude_code_auth_failure: failed to notify for run %s", run_id, exc_info=True)

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
        """Stamps status:done and closes the issue — called once something
        actually reaches production (server.py's _record_production_release).
        Closing an already-closed issue is a harmless no-op."""
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
        """Like record_failure, but for a failure that happened while working
        an already-known tracking issue (resolves_id/sub_feature_of was set on
        the implement_feature call) -- posts the failure as a comment on THAT
        issue instead of filing a brand-new, disconnected "X failed during an
        autonomous cycle" bug report.

        Without this, a failure mid-resume (e.g. hitting the cost cap while
        continuing issue #2's work) produced a second, unrelated issue with no
        reference back to #2 at all -- a human looking at #2 would have no way
        to know it had failed, and a human looking at the new generic bug
        report would have no idea it was actually about #2's in-progress work.
        Reported as: agent creating a new issue for the same problem while
        working in another issue."""
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
        """The branch an interrupted implement_feature call pushed work to, if any.
        Deliberately NOT folded into known_bugs()/feature_queue() themselves —
        those are called on every dashboard poll; this is one extra live GitHub
        call per entry, paid only by callers that actually act on resume_branch."""
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

    def resume_session_id_for(self, external_id: str) -> str | None:
        """The Claude session_id an interrupted issue's build was using, if
        any — lets a resumed cycle continue that same conversation instead of
        cold-starting implement_feature from scratch for this issue."""
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

"""Persistent memory store, scoped to the target repo being improved.

vision.md section 9 originally laid this out as five categories
(architecture, decisions, features, metrics, failures) under
<repo>/.agentra/memory/, each a write-only per-run ledger. Four of those
five turned out to have zero readers anywhere in the codebase (confirmed
live) and were retired: decisions/features/metrics were pure duplication
of documentation.md's changelog and git commit history; failures/*.md was
replaced by record_failure()'s actual triage policy (a permanent failure
becomes a GitHub issue, a transient one is just logged) instead of an
archive nothing ever consulted. architecture/ is what's left, and it's
different in kind from the other four: not a per-run log at all, but a
handful of named, live-maintained "steering files" (codebase.md,
design.md, testing-notes.md, documentation.md) that each get overwritten
fresh by whichever agent is responsible for that fact, or read by every
downstream agent as shared context.

App-agnostic by design: everything here is plain JSON/YAML files agentra
owns and reads/writes itself, or -- for known_bugs/feature_queue/shipped/
objective/environments -- GitHub Issues/Actions Variables directly, with no
local mirror at all (this module's own _repo_url() requires a github.com
remote; there is deliberately no local-file fallback if GitHub is
unreachable or unconfigured, a known availability tradeoff -- see
known_bugs()'s own docstring). A shipped feature is an open 'feature'-
labeled issue stamped "status:shipped" (record_shipped()/shipped_features(),
connectors/github_issues.py's mark_shipped) -- it only actually closes once
promoted to production (mark_status_done, called from server.py's
_record_production_release), so an issue's own open/closed state maps 1:1
onto the dashboard's "Ready to Review"/"Release to Production" split. Its
individual parts, if it has any, are separate 'story'-labeled sub-issues
(those DO close immediately on shipping -- GitHub's own sub-issue
"completed" tracking is open/closed-based, not label-based); only the whole
feature's own issue carries 'feature' -- so the whole feature lifecycle --
requested, in progress, shipped, released -- lives as one GitHub Issue with
a run_id/commit_sha trail, not a JSON ledger duplicating it.
Getting real data INTO the backlog
(customer feedback from whatever database a given app uses) is the job of
a per-app adapter command
(EnvironmentConfig.feedback_fetch_command, invoked by orchestrator.py/
brain.py) -- agentra's own code never talks to Firestore/Postgres/anything
app-specific directly.

Git hygiene
-----------
On first use, Memory writes a .agentra/.gitignore into the target repo so that:

  Committed (audit trail, config)       Not committed (noisy per-run data)
  ──────────────────────────────────    ────────────────────────────────────
  memory/architecture/                  logs/  (verbose timestamped traces)
  released.json                         test_artifacts/ (pre-prod screenshots)
  feedback_sync_state.json
  codebase_spec_commit.json

This means the memory system travels with the repo across clones, CI runners,
and contributors, while run logs stay local. known_bugs.json/feature_queue.json/
shipped.json/work_updates.json/objective.yaml/environments.yaml no longer exist
as local files at all -- GitHub Issues/Variables are their only home now. Agent
chat history, the live standup channel, and session continuity moved out
entirely too -- see chat_store.py -- they were never a fit for a customer's own
git history in the first place.
"""

import datetime as dt
import difflib
import json
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

CATEGORIES = ("architecture",)

_SAFETY_DETAIL_LIMIT = 200


def format_safety_denial_line(tool_name: str, pattern: str, detail: str, limit: int = _SAFETY_DETAIL_LIMIT) -> str:
    """Build the run-log line for a blocked tool call, tagged with a
    '[safety]' channel so it's greppable inside the otherwise-freeform
    per-run log that Memory.log/record_safety_denial write to.

    A module-level function (not just a Memory method) so agents/safety.py's
    PreToolUse hook -- which has no Memory instance or run_id, only the
    ambient run logger from base.py's run_log_scope -- can build the same
    line shape that Memory.record_safety_denial below does, and hand it to
    that logger directly instead of needing new plumbing to call
    record_safety_denial itself.
    """
    truncated = detail if len(detail) <= limit else detail[: limit - 3] + "..."
    return f"[safety] denied tool={tool_name} pattern={pattern!r} detail={truncated!r}"


# Written to <repo>/.agentra/.gitignore on first use. We commit the audit
# trail and config; ignore the verbose per-run logs and binary test
# screenshots that add noise (and, for screenshots, real repo bloat since
# every image is a distinct blob) without audit-trail value in history.
_GITIGNORE_CONTENT = """\
# Generated by agentra — do not edit manually.
# Commit the audit trail; ignore per-run logs and test screenshots.

# ── ignored ──────────────────────────────────────────────────────────────────
logs/
test_artifacts/

# ── tracked (listed for clarity; git tracks everything not ignored) ───────────
# memory/
# released.json
# feedback_sync_state.json
# environments.yaml
"""


def _ensure_gitignore(root: Path) -> None:
    """Write .agentra/.gitignore if it is missing or doesn't ignore
    logs/ or test_artifacts/ -- the latter check retrofits repos that
    already have a committed .gitignore from before test_artifacts/
    (screenshots) existed, so they don't start accidentally committing
    binary screenshot blobs the moment they upgrade."""
    gitignore = root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        if "logs/" in content and "test_artifacts/" in content:
            return  # already correct, skip the write
    gitignore.write_text(_GITIGNORE_CONTENT)


# ── GitHub Issues as the authoritative backlog ─────────────────────────────
# known_bugs()/feature_queue() read live from GitHub Issues (labels "bug" and
# "feature") when the target repo's remote is on github.com and the GitHub
# App is installed with Issues access -- [] (empty, GitHub unreachable/
# unconfigured/App not installed) otherwise, no local fallback at all. This
# means a human filing or closing an issue directly on GitHub is picked
# up/dropped by Discovery Agent automatically, without going through
# agentra's dashboard at all -- the actual point of making GitHub
# authoritative.
# Stamped onto every issue agentra itself creates (below), and required by
# every read filter below too -- a human can file a "bug" or "feature"
# labeled issue directly on GitHub for their own tracking, unrelated to
# agentra; without this, that issue would silently show up in agentra's own
# backlog/dashboard as if agentra were meant to work on it. GitHub's REST
# `labels` query param is an AND filter across multiple names (an issue
# must carry all of them to match), which is exactly what's needed here.
_AGENTRA_LABEL = "agentra"
_BUG_LABEL = "bug"
_FEATURE_LABEL = "feature"
# A multi-part feature's individual pieces (record_shipped's sub_feature_of/
# more_parts_expected paths) are labeled "story", not "feature" -- the parent
# tracking issue (or a simple, single-call feature) is the only thing labeled
# "feature", so feature_queue()/shipped_features() (both filtered to
# _FEATURE_LABEL) only ever surface whole features, never individual parts.
_STORY_LABEL = "story"

# A bug agentra diagnosed as something it cannot fix by retrying or writing
# different code -- a credential/permission problem, an external outage, a
# decision only a human can make -- gets both of these on top of "bug"
# (see cannot_be_fixed_by_agentra below and record_known_bug's needs_human/
# blocking_agentra params). need_human means "don't attempt this one, it
# needs a human" -- check_backlog/discover_opportunities filter it out of
# what's offered as workable. blocking_agentra means "agentra's own further
# progress is blocked until a human acts" -- run_autonomous_cycle refuses to
# even start an autonomous cycle while one is open (see blocking_bugs()
# below), rather than let it keep retrying variations of a fix that can
# never work. Confirmed live: 4 consecutive cycles each burned cost trying a
# different angle on the identical "403 Write access to repository not
# granted" failure -- this label pair is what should have made cycle 2 stop
# and wait for a human instead of trying again.
_NEED_HUMAN_LABEL = "need_human"
_BLOCKING_AGENTRA_LABEL = "blocking_agentra"

# Lifecycle status: status:in-progress marks real work having started
# (added alongside the in-progress-branch marker, see
# record_in_progress_branch); status:shipped marks code committed --
# record_shipped/clear_known_bug stamp this but deliberately leave the
# issue OPEN (github_issues.mark_shipped); status:done marks having
# actually reached production (added at promotion time, see server.py's
# _record_production_release, which is also what finally CLOSES the
# issue -- see mark_status_done below). So an issue's own open/closed
# state and these two labels move together, not independently: the
# dashboard's "Ready to Review" section is open issues carrying
# status:shipped; "Release to Production" is closed ones.
_STATUS_IN_PROGRESS_LABEL = "status:in-progress"
_STATUS_SHIPPED_LABEL = "status:shipped"
_STATUS_DONE_LABEL = "status:done"

# ── Failure triage: replaces the old write-only memory/failures/*.md ledger
# (confirmed nothing in this codebase ever read it back). A permanent
# failure (a real defect in the work attempted) gets filed via
# record_known_bug -- which creates a real GitHub Issue when the target
# repo has one configured, so Discovery Agent prioritizes fixing it next
# cycle instead of it vanishing into a file nothing reads. A transient
# failure (infra/capacity hiccups: rate/usage limits, max-turns
# exhaustion, the Claude Code CLI's own contradictory-result quirk -- see
# agents/base.py's _CONTRADICTORY_RESULT_SUFFIX) isn't a defect at all; a
# retry next cycle is the fix, not a backlog entry, so it's just logged. ──
_TRANSIENT_FAILURE_PATTERNS = [
    re.compile(r"Reached maximum number of turns \(\d+\)"),
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"overloaded", re.IGNORECASE),
    re.compile(r"returned an error result: success"),
]


def is_transient_failure(text: str) -> bool:
    return any(p.search(text) for p in _TRANSIENT_FAILURE_PATTERNS)


# Auth/permission failures specifically: no brief, no retry, no different code
# can fix these -- the fix is always a human granting/rotating something
# outside the repo. Distinct from _TRANSIENT_FAILURE_PATTERNS above (those
# resolve themselves on retry; these never do without human action), and from
# an ordinary bug (those a future implement_feature call CAN fix). Deliberately
# narrow/conservative: a false negative just means a fixable-looking bug gets
# retried normally (today's behavior); a false positive would wrongly halt the
# whole orchestrator, so this only matches the unambiguous cases.
_UNFIXABLE_BY_AGENTRA_PATTERNS = [
    re.compile(r"unauthorized", re.IGNORECASE),
    re.compile(r"permission denied", re.IGNORECASE),
    re.compile(r"access.{0,20}not granted", re.IGNORECASE),
    re.compile(r"write access.{0,20}not granted", re.IGNORECASE),
]
# Deliberately no bare \b401\b/\b403\b pattern -- confirmed live (GitHub
# issue #17): a Testing Agent report that mentioned "401 instead of 200"
# in unrelated prose (describing an already-diagnosed, unrelated ambient
# env var tripping an auth test -- not anything blocking agentra itself)
# got classified as unfixable and labeled blocking_agentra, which halted
# every future autonomous cycle over a false positive. A bare status code
# is too common in ordinary diagnostic/test-report text to be a reliable
# signal on its own; the phrase-based patterns above still catch the real
# cases ("403 Write access to repository not granted", "401 Unauthorized")
# without matching an incidental number.


def cannot_be_fixed_by_agentra(text: str) -> bool:
    return any(p.search(text) for p in _UNFIXABLE_BY_AGENTRA_PATTERNS)


def _label_names(issue: dict) -> set[str]:
    return {lbl["name"] if isinstance(lbl, dict) else lbl for lbl in issue.get("labels", [])}


# A dashboard submission (memory.py's record_known_bug/record_feature_request,
# `title` param) puts the short human-written title on the issue itself and
# the fuller free-text description as a "Description: ..." line in the body,
# so the dashboard's backlog list can keep showing the substantive text
# (diagnosis/description) rather than a title that might be much shorter (or,
# for a caller with no separate title -- every non-dashboard/autonomous
# caller -- much longer, since the whole diagnosis becomes the title then).
# Falls back to the title when there's no such line, which covers every
# issue created before this existed and every autonomous-filed one, both of
# which only ever had a single piece of text to begin with.
_DESCRIPTION_RE = re.compile(r"^Description: (.+)$", re.MULTILINE)


def _issue_description(issue: dict) -> str:
    match = _DESCRIPTION_RE.search(issue.get("body") or "")
    return match.group(1) if match else issue["title"]


def _github_bug_to_dict(issue: dict) -> dict:
    # run_id set to the same value as external_id (not None): discovery.py's
    # prompt tells the LLM to reference a known_bug by its run_id
    # specifically ("id=b.get('run_id', '')"), unlike feature_queue entries
    # which use external_id -- clear_known_bug() already matches on either,
    # so giving both fields the issue number keeps that resolution path
    # working for GitHub-sourced bugs without having to change discovery.py.
    issue_number = str(issue["number"])
    labels = _label_names(issue)
    return {
        "run_id": issue_number,
        "severity": "medium",
        "diagnosis": _issue_description(issue),
        "proposed_fix": issue.get("body") or "",
        "source": "github",
        "external_id": issue_number,
        "html_url": issue.get("html_url"),
        "needs_human": _NEED_HUMAN_LABEL in labels,
        "blocking_agentra": _BLOCKING_AGENTRA_LABEL in labels,
    }


def _github_closed_bug_to_dict(issue: dict) -> dict:
    # Separate from _github_bug_to_dict (open bugs) rather than adding
    # closed_at there -- that dict shape is exact-compared in several
    # existing tests, and closed_at is meaningless for an open issue anyway.
    issue_number = str(issue["number"])
    labels = _label_names(issue)
    return {
        "run_id": issue_number,
        "severity": "medium",
        "diagnosis": _issue_description(issue),
        "proposed_fix": issue.get("body") or "",
        "source": "github",
        "external_id": issue_number,
        "html_url": issue.get("html_url"),
        "closed_at": issue.get("closed_at"),
        "status_done": _STATUS_DONE_LABEL in labels,
    }


def _github_feature_to_dict(issue: dict) -> dict:
    return {
        "description": _issue_description(issue),
        "source": "github",
        "external_id": str(issue["number"]),
        "html_url": issue.get("html_url"),
    }


# A shipped feature/bug fix -- open+status:shipped or closed+status:done
# (see shipped_features()/closed_bugs()) -- has run_id/commit_sha stamped
# into its issue body as structured lines by mark_shipped's body_suffix, so
# these can be parsed back out of the same list call, no per-issue
# follow-up request needed.
_SHIPPED_RUN_ID_RE = re.compile(r"^Shipped-Run-ID: (.+)$", re.MULTILINE)
_SHIPPED_COMMIT_RE = re.compile(r"^Shipped-Commit: (.+)$", re.MULTILINE)


def _github_shipped_to_dict(issue: dict) -> dict:
    body = issue.get("body") or ""
    run_id_m = _SHIPPED_RUN_ID_RE.search(body)
    commit_m = _SHIPPED_COMMIT_RE.search(body)
    labels = _label_names(issue)
    return {
        "feature": issue["title"],
        "commit_sha": commit_m.group(1) if commit_m else None,
        "run_id": run_id_m.group(1) if run_id_m else None,
        "ts": issue.get("closed_at"),
        "external_id": str(issue["number"]),
        "html_url": issue.get("html_url"),
        "status_done": _STATUS_DONE_LABEL in labels,
    }


class Memory:
    def __init__(self, repo: Path):
        self.repo = repo
        self.root = repo / ".agentra"
        self.memory_root = self.root / "memory"
        self.log_root = self.root / "logs"
        self.released_path = self.root / "released.json"
        self.feedback_sync_state_path = self.root / "feedback_sync_state.json"
        self.codebase_spec_commit_path = self.root / "codebase_spec_commit.json"
        for category in CATEGORIES:
            (self.memory_root / category).mkdir(parents=True, exist_ok=True)
        self.log_root.mkdir(parents=True, exist_ok=True)
        _ensure_gitignore(self.root)

    def write(self, category: str, name: str, content: str) -> Path:
        if category not in CATEGORIES:
            raise ValueError(f"unknown memory category: {category}")
        path = self.memory_root / category / f"{name}.md"
        # The category directory can vanish between __init__ and this call -- e.g.
        # implementation.py's _checkout_feature_branch does `git clean -fd .agentra/`
        # then `git checkout -B <feature_branch>`, and git doesn't track empty
        # directories, so a category with no committed file in it (e.g. a repo's
        # first-ever feature under memory/features/) simply doesn't come back after
        # that checkout. Recreate it defensively rather than assume __init__'s mkdir
        # still holds.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def read(self, category: str, name: str) -> str | None:
        path = self.memory_root / category / f"{name}.md"
        return path.read_text() if path.exists() else None

    def log(self, run_id: str, content: str) -> Path:
        path = self.log_root / f"{run_id}.log"
        self.log_root.mkdir(parents=True, exist_ok=True)
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
        with path.open("a") as f:
            f.write(f"[{timestamp}] {content}\n")
        self._log_to_firestore(run_id, timestamp, content)
        return path

    def _log_to_firestore(self, run_id: str, timestamp: str, content: str) -> None:
        # Per-run logs were the one data type the storage audit found with
        # no durable copy anywhere -- .agentra/logs/ is gitignored on
        # purpose (verbose, not audit-trail material) and REPOS_ROOT itself
        # is documented VM-local-only (registry.py), so a run's own log was
        # permanently gone the moment its VM instance rebuilt. Mirrors the
        # same collection every run/signal/agent_step already durably lives
        # in (registry.firestore_client()) -- best-effort and silently
        # skipped if Firestore isn't configured (local dev) or the write
        # fails, exactly like every other Firestore mirror in this
        # codebase: local-first, Firestore durability is a bonus, never a
        # requirement for a cycle to keep running.
        try:
            from agentra import registry

            db = registry.firestore_client()
            if db is None:
                return
            from google.cloud import firestore as gcf

            db.collection("run_logs").document(run_id).set(
                {"lines": gcf.ArrayUnion([f"[{timestamp}] {content}"])}, merge=True
            )
        except Exception:
            logger.warning("log: failed to mirror run %s log line to Firestore", run_id, exc_info=True)

    def record_safety_denial(self, run_id: str, tool_name: str, pattern: str, detail: str) -> Path:
        """Durable audit trail for agents/safety.py's guarded_pre_tool_use:
        every blocked tool call gets a '[safety]'-tagged line in the run's
        log via the same self.log() channel everything else in a cycle
        writes to, instead of the deny decision going back to the SDK with
        zero trace left anywhere. `detail` is the (already truncated)
        command/file_path that tripped `pattern`.

        In practice the hook itself calls format_safety_denial_line directly
        through the ambient run logger (base.py's run_log_scope) rather than
        this method, since it has no Memory instance or run_id of its own --
        see agents/safety.py. This method exists for any caller that *does*
        hold a Memory instance, and to keep the line format defined in one
        place."""
        return self.log(run_id, format_safety_denial_line(tool_name, pattern, detail))

    def shipped_features(self) -> list[dict]:
        """Each entry: {feature, commit_sha, run_id, ts, external_id,
        status_done}. A shipped feature is a 'feature'-labeled issue that's
        either open with "status:shipped" (implemented, awaiting production
        promotion -- the dashboard's "Ready to Review") or closed (already
        promoted -- status_done is True, "Release to Production"; see
        mark_status_done, the only thing that ever closes one). commit_sha/
        run_id are parsed back out of the body stamp record_shipped()
        writes; there is no local shipped.json anymore (see record_shipped's
        docstring for why one ledger is enough). commit_sha links the
        dashboard's Shipped list to the actual artifact (a GitHub commit
        URL, built client-side from the app's repo_url + this sha); run_id
        links it back to the exact run/agent-steps/log that produced it;
        external_id is the GitHub issue number, for building a direct link
        to the audit trail."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("shipped_features: %s has no github.com remote -- shipped history is unreadable", self.repo)
            return []
        try:
            from agentra.connectors import github_issues

            open_shipped = [
                i
                for i in github_issues.list_open_issues(repo_url, labels=[_FEATURE_LABEL, _AGENTRA_LABEL])
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
    ) -> dict | None:
        """Records a shipped feature (or one part of one) as an open GitHub
        'feature'-labeled issue stamped "status:shipped" (a part is
        'story'-labeled instead and DOES close immediately -- see
        sub_feature_of below) -- the same ledger feature_queue() reads (open,
        no status:shipped) and shipped_features() reads (status:shipped or
        status:done), so "pending" vs. "shipped" vs. "released" is just an
        issue's own state/labels, never separate things to keep in sync. The
        issue only actually closes once mark_status_done runs it through
        production promotion (server.py's _record_production_release).

        - sub_feature_of set: this part continues a multi-part feature
          already started (its parent issue number). A new sub-issue is
          created and closed for THIS part (sub-issues always close
          immediately -- GitHub's own sub-issue "completed" count is
          open/closed-based); the parent itself only gets status:shipped
          once more_parts_expected=False signals the whole feature is now
          done -- until then it stays open with no such label, discoverable
          via in_progress_features() so a later cycle resumes it instead of
          abandoning it.
        - sub_feature_of unset, more_parts_expected=True: starts a new
          multi-part feature. If resolves_id names a feature_queue issue,
          THAT issue becomes the (left-open) parent; otherwise a fresh
          parent issue is created and left open. Either way this call's
          own part becomes the parent's first sub-issue, closed as usual.
        - Neither set: today's simple, single-call feature. resolves_id
          (a feature_queue issue) is marked shipped directly as the record;
          with no resolves_id, a fresh issue is opened and marked shipped.
          Unchanged from before multi-part features existed.

        Returns {"issue_number": N, "board_issue_number": M} on success -- M
        is this call's own issue except for the sub_feature_of/
        more_parts_expected paths, where it's the parent's number (the one
        to reference on a following call). None if recording failed."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("record_shipped: %s has no github.com remote -- shipped feature %r was NOT recorded anywhere", self.repo, feature)
            return None
        note = f"Shipped as {feature!r}" + (f" (run {run_id})" if run_id else "") + (f" (commit {commit_sha})" if commit_sha else "") + "."
        body_suffix = "---\n" + (f"Shipped-Run-ID: {run_id}\n" if run_id else "") + (f"Shipped-Commit: {commit_sha}\n" if commit_sha else "")
        try:
            from agentra.connectors import github_issues

            if sub_feature_of and sub_feature_of.isdigit():
                parent_number = int(sub_feature_of)
                issue = github_issues.create_sub_issue(
                    repo_url, parent_number, feature, "Autonomously shipped by agentra.", labels=[_STORY_LABEL, _AGENTRA_LABEL]
                )
                issue_number = issue["number"]
                github_issues.close_issue(repo_url, issue_number, comment=note, body_suffix=body_suffix)
                if not more_parts_expected:
                    github_issues.mark_shipped(
                        repo_url, parent_number, comment=f"All parts shipped (run {run_id})." if run_id else "All parts shipped."
                    )
                board_issue_number = parent_number

            elif more_parts_expected:
                if resolves_id and resolves_id.isdigit():
                    parent_number = int(resolves_id)  # the feature_queue issue itself becomes the parent, left open
                else:
                    parent_issue = github_issues.create_issue(
                        repo_url,
                        feature,
                        "Tracks a multi-part feature; stays open until every part has shipped.",
                        labels=[_FEATURE_LABEL, _AGENTRA_LABEL],
                    )
                    parent_number = parent_issue["number"]
                issue = github_issues.create_sub_issue(
                    repo_url, parent_number, feature, "Autonomously shipped by agentra.", labels=[_STORY_LABEL, _AGENTRA_LABEL]
                )
                issue_number = issue["number"]
                github_issues.close_issue(repo_url, issue_number, comment=note, body_suffix=body_suffix)
                board_issue_number = parent_number

            elif resolves_id and resolves_id.isdigit():
                issue_number = int(resolves_id)
                github_issues.mark_shipped(repo_url, issue_number, comment=note, body_suffix=body_suffix)
                board_issue_number = issue_number

            else:
                # Safety net, not just a prompt hope: implement_feature's caller is
                # supposed to pass resolves_id/resolves_origin when this call resolves
                # a known bug or feature-queue item, but confirmed live -- three
                # separate times (issues #13/#16, #1/#19, #6/#15) -- it doesn't always.
                # A known-bug fix in particular NEVER reaches this branch's resolves_id
                # path anyway (brain.py only forwards resolves_id when
                # resolves_origin=="feature_queue"; a known_bug fix relies entirely on
                # a SEPARATE clear_known_bug call the caller must also remember to
                # make), so the original backlog entry silently stayed open forever
                # while an orphaned fresh issue carried the shipped record -- the same
                # piece of work then showed up as both still-open (backlog) and
                # already-shipped (ready to review). Before creating a fresh issue,
                # check whether a similar bug or feature-queue item is already open and
                # mark THAT as shipped instead.
                duplicate_of = self._find_similar_open(feature, self.known_bugs(), "diagnosis") or self._find_similar_open(
                    feature, self.feature_queue(), "description"
                )
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
        """Each entry: {feature, commit_sha, ts, release_run_id} -- the
        production release ledger. This is intentionally separate from
        shipped_features(): shipped means "implemented and in pre-prod", while
        released means "made it to prod". Older released.json files that might
        only contain a plain list[str] normalize into the same shape."""
        if not self.released_path.exists():
            return []
        raw = json.loads(self.released_path.read_text())
        if not isinstance(raw, list):
            return []
        return [
            {
                "feature": e,
                "commit_sha": None,
                "ts": None,
                "release_run_id": None,
            }
            if isinstance(e, str)
            else e
            for e in raw
        ]

    def record_released(
        self,
        feature: str,
        release_run_id: str,
        commit_sha: str | None = None,
    ) -> None:
        released = self.released_features()
        if any(f["feature"] == feature for f in released):
            return
        released.append(
            {
                "feature": feature,
                "commit_sha": commit_sha,
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "release_run_id": release_run_id,
            }
        )
        self.released_path.write_text(json.dumps(released, indent=2))

    # ── Signals: production issues, from agentra's own monitoring (debug-prod) ──
    # or from customer bug reports pulled in via feedback_fetch_command. Same
    # ledger either way -- Discovery Agent treats a confirmed bug as a confirmed
    # bug regardless of who/what found it.

    def _repo_url(self) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo), "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    def known_bugs(self) -> list[dict]:
        """Open 'bug'-labeled issues still needing a fix -- excludes ones
        already stamped "status:shipped" by clear_known_bug (fixed,
        awaiting production promotion; see closed_bugs()) even though
        those are technically still open too."""
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
        """Resolved bugs -- open+status:shipped (fixed, awaiting production
        promotion) or closed (already promoted; status_done True -- see
        mark_status_done). Paired with shipped_features() by the
        dashboard's "Ready to Review"/"Release to Production" views:
        together they're everything agentra has actually finished,
        regardless of type."""
        repo_url = self._repo_url()
        if not repo_url:
            return []
        try:
            from agentra.connectors import github_issues

            open_shipped = [
                i
                for i in github_issues.list_open_issues(repo_url, labels=[_BUG_LABEL, _AGENTRA_LABEL])
                if _STATUS_SHIPPED_LABEL in _label_names(i)
            ]
            closed = github_issues.list_closed_issues(repo_url, labels=[_BUG_LABEL, _AGENTRA_LABEL])
            return [_github_closed_bug_to_dict(i) for i in open_shipped + closed]
        except Exception:
            logger.error("closed_bugs: GitHub Issues unavailable for %s", repo_url, exc_info=True)
            return []

    _DUPLICATE_BUG_SIMILARITY_THRESHOLD = 0.6

    def _find_similar_open(self, text: str, candidates: list[dict], field: str) -> str | None:
        """Shared by _find_similar_open_bug (record_known_bug's duplicate
        suppression) and record_shipped's own orphan-issue safety net
        below -- difflib similarity, not exact match, since the text being
        compared is LLM-generated free text that varies call to call even
        when describing the same underlying thing."""
        for candidate in candidates:
            existing = candidate.get(field) or ""
            if difflib.SequenceMatcher(None, text, existing).ratio() >= self._DUPLICATE_BUG_SIMILARITY_THRESHOLD:
                return candidate.get("external_id")
        return None

    def _find_similar_open_bug(self, diagnosis: str) -> str | None:
        """Best-effort duplicate suppression: record_failure() fires on
        every non-transient failure, and an unfixed real bug produces the
        same failure on every retry until someone actually fixes it --
        without this, each cycle filed its own fresh issue for the
        identical problem (confirmed live: 4 near-identical "403 Write
        access to repository not granted" bugs, #7-#10, from four
        consecutive cycles hitting the same broken code path). Only checks
        OPEN bugs -- a bug that recurs after being marked fixed is a real
        regression, not a duplicate, and should get its own fresh issue."""
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
    ) -> None:
        """needs_human/blocking_agentra: set when the caller already knows
        agentra cannot fix this itself (see cannot_be_fixed_by_agentra) --
        stamped onto the GitHub issue as the "need_human"/"blocking_agentra"
        labels (on top of "bug"), which check_backlog/discover_opportunities
        and run_autonomous_cycle's pre-flight check read back. If a similar
        bug is already open, these are added to THAT issue instead (GitHub's
        add-labels endpoint is additive) -- the original occurrence might not
        have been recognized as unfixable, but a later one can be.

        `title`: a short human-written title (the dashboard's "add to
        backlog" form collects one) -- becomes the issue's actual title,
        with `diagnosis` recorded as a "Description: ..." body line instead
        (see _issue_description) so known_bugs() still surfaces the
        substantive text rather than a title that might be much shorter.
        Every non-dashboard caller (autonomous bug filing) leaves this
        unset, so diagnosis becomes the title directly -- unchanged from
        before this param existed."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("record_known_bug: %s has no github.com remote -- bug %r was NOT recorded anywhere", self.repo, diagnosis)
            return
        extra_labels = ([_NEED_HUMAN_LABEL] if needs_human else []) + ([_BLOCKING_AGENTRA_LABEL] if blocking_agentra else [])
        try:
            from agentra.connectors import github_issues

            duplicate_of = self._find_similar_open_bug(diagnosis)
            if duplicate_of and duplicate_of.isdigit():
                github_issues.add_comment(
                    repo_url, int(duplicate_of), f"Still occurring (run {run_id}, source: {source}).\n\n{diagnosis}"
                )
                if extra_labels:
                    github_issues.add_labels(repo_url, int(duplicate_of), extra_labels)
                logger.info(
                    "record_known_bug: run %s matched existing open bug #%s -- commented instead of filing a duplicate",
                    run_id, duplicate_of,
                )
                return

            body = f"Description: {diagnosis}\n\nSeverity: {severity}\nSource: {source}\n\nProposed fix:\n{proposed_fix}"
            if external_id:
                body += f"\n\nExternal-ID: {external_id}"
            github_issues.create_issue(repo_url, title or diagnosis, body, labels=[_BUG_LABEL, _AGENTRA_LABEL, *extra_labels])
        except Exception:
            logger.error("record_known_bug: failed to create a GitHub issue on %s -- bug %r was NOT recorded anywhere", repo_url, diagnosis, exc_info=True)

    def clear_known_bug(self, id_: str, resolution_note: str | None = None) -> None:
        # resolution_note: what actually shipped to fix this (feature name + commit) --
        # callers that know it should pass it, so the issue itself carries a real
        # "shipped" record instead of a content-free "Resolved by agentra." Marks
        # status:shipped and leaves the issue OPEN (github_issues.mark_shipped) --
        # it only actually closes once production promotion runs it through
        # mark_status_done (server.py's _record_production_release), same as a
        # shipped feature (see record_shipped's docstring).
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

    def record_failure(self, run_id: str, step_name: str, text: str, severity: str = "high") -> None:
        """The one place a failed agent turn's full output should be
        reported to -- see is_transient_failure's docstring above for the
        permanent-vs-transient policy this implements."""
        if is_transient_failure(text):
            self.log(run_id, f"{step_name} failed (transient, not filed as a bug): {text[:200]}")
            return
        unfixable = cannot_be_fixed_by_agentra(text)
        self.record_known_bug(
            run_id, severity, f"{step_name} failed during an autonomous cycle", text[:2000], source="autonomous-failure",
            needs_human=unfixable, blocking_agentra=unfixable,
        )

    def blocking_bugs(self) -> list[dict]:
        """Open bugs labeled both "bug" and "blocking_agentra" -- see the
        label constants' docstring above. run_autonomous_cycle checks this
        before starting an autonomous cycle at all; non-empty means agentra
        should not attempt anything else until a human resolves it."""
        return [b for b in self.known_bugs() if b.get("blocking_agentra")]

    def record_in_progress_branch(self, issue_number: int, branch: str, run_id: str | None = None) -> None:
        """Marks `branch` (and run_id, for traceability -- see
        github_issues.record_in_progress_branch's docstring) as where an
        implement_feature call's work for this issue lives. Also adds the
        status:in-progress label -- this is the "agent action" trigger for
        the issue's lifecycle status (see _STATUS_IN_PROGRESS_LABEL's
        comment above): real work has demonstrably started, independent of
        the issue's own open/closed state. Best-effort throughout: failure
        just means a future cycle won't be offered a resume / the status
        label won't update, not a reason to fail the call recording it."""
        repo_url = self._repo_url()
        if not repo_url:
            return
        try:
            from agentra.connectors import github_issues

            github_issues.record_in_progress_branch(repo_url, issue_number, branch, run_id)
            github_issues.add_labels(repo_url, issue_number, [_STATUS_IN_PROGRESS_LABEL])
        except Exception:
            logger.warning("record_in_progress_branch: failed for issue #%s on %s", issue_number, repo_url, exc_info=True)

    def mark_status_done(self, issue_number: int) -> None:
        """The "final deployment" trigger for an issue's lifecycle status
        (see _STATUS_DONE_LABEL's comment above) -- called once whatever
        this issue tracks has actually reached production (server.py's
        _record_production_release), regardless of whether a human clicked
        Promote in the dashboard or an autonomous cycle triggered it; both
        paths call the same promotion code. Also closes the issue -- under
        the current model status:done and "closed" happen together (see
        record_shipped/clear_known_bug's mark_shipped, which stamps
        status:shipped but deliberately leaves it open); closing an
        already-closed issue is a harmless no-op, so this is safe to call
        on something promoted before this label existed too. Best-effort,
        same reasoning as record_in_progress_branch."""
        repo_url = self._repo_url()
        if not repo_url:
            return
        try:
            from agentra.connectors import github_issues

            github_issues.add_labels(repo_url, issue_number, [_STATUS_DONE_LABEL])
            github_issues.close_issue(repo_url, issue_number, comment="Released to production.")
        except Exception:
            logger.warning("mark_status_done: failed for issue #%s on %s", issue_number, repo_url, exc_info=True)

    def record_commit(self, issue_number: int, commit_sha: str) -> None:
        """Links commit_sha on this issue -- see
        github_issues.record_commit's docstring for why a tracking issue
        needs every commit linked, not just the last one. Best-effort,
        same reasoning as record_in_progress_branch."""
        repo_url = self._repo_url()
        if not repo_url:
            return
        try:
            from agentra.connectors import github_issues

            github_issues.record_commit(repo_url, issue_number, commit_sha)
        except Exception:
            logger.warning("record_commit: failed for issue #%s on %s", issue_number, repo_url, exc_info=True)

    def resume_branch_for(self, external_id: str) -> str | None:
        """The branch an interrupted implement_feature call for this bug/
        feature-queue issue pushed its work to, if any (see
        github_issues.record_in_progress_branch/get_in_progress_branch) --
        None if it's never had one, the id isn't a real issue number, or
        the lookup fails. Deliberately NOT folded into known_bugs()/
        feature_queue() themselves: those are read on every dashboard poll
        and discover_opportunities call, and this is one extra live GitHub
        call per entry -- callers that actually act on resume_branch
        (brain.py's check_backlog) look it up themselves, in parallel,
        rather than paying that cost everywhere."""
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
        """The run_id that pushed resume_branch_for's branch, if recorded --
        see github_issues.get_in_progress_run_id's docstring. Informational
        (look up that run's own log for context on what was already tried),
        not something a resuming call needs to function."""
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

    def record_spec(self, issue_number: int, spec: dict) -> None:
        """Persists Requirements Agent's finalized spec on this issue -- see
        github_issues.record_spec's docstring. Best-effort, same reasoning
        as record_in_progress_branch: a failure here just means a resumed
        cycle regenerates the spec instead of reusing it, not a reason to
        fail the implement_feature call that's trying to record it."""
        repo_url = self._repo_url()
        if not repo_url:
            return
        try:
            from agentra.connectors import github_issues

            github_issues.record_spec(repo_url, issue_number, spec)
        except Exception:
            logger.warning("record_spec: failed for issue #%s on %s", issue_number, repo_url, exc_info=True)

    def get_spec(self, issue_number: int) -> dict | None:
        """The most recently recorded spec for this issue, or None if it's
        never had one or the lookup fails."""
        repo_url = self._repo_url()
        if not repo_url:
            return None
        try:
            from agentra.connectors import github_issues

            return github_issues.get_spec(repo_url, issue_number)
        except Exception:
            return None

    # ── Queue: feature requests, from customers or added by an admin. Considered
    # by Discovery Agent above its own autonomous ideation, below real signals. ──

    def feature_queue(self) -> list[dict]:
        """Open 'feature'-labeled issues not yet shipped -- excludes ones
        already stamped "status:shipped" (implemented, awaiting production
        promotion; see shipped_features()) even though those are
        technically still open too."""
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
        sub-issue -- a multi-part feature record_shipped started (via
        sub_feature_of/more_parts_expected) but hasn't yet signaled
        complete. A plain not-yet-started feature_queue entry has zero
        sub-issues, so this and feature_queue() describe different things
        even though both can include the same open issue -- check_backlog
        surfaces this first so a new cycle resumes unfinished work instead
        of abandoning it to start something else.

        sub_issues_total > 0 alone isn't enough, though: a feature can be
        broken into planned sub-issues (a manual breakdown, or any path
        other than record_shipped actually completing one) with zero of
        them shipped -- confirmed live, issue #2 sat with two open,
        unstarted sub-issues and nothing done, yet still got surfaced here
        ahead of real bugs. sub_issues_completed > 0 is the "has work
        actually begun" signal instead: a candidate with zero completed
        sub-issues is filtered out (github_issues.list_in_progress_features
        already excludes fully-shipped parents via "status:shipped")."""
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

    def record_feature_request(
        self,
        description: str,
        source: str = "customer",
        external_id: str | None = None,
        title: str | None = None,
    ) -> None:
        """`title`: a short human-written title (the dashboard's "add to
        backlog" form collects one) -- becomes the issue's actual title,
        with `description` recorded as a "Description: ..." body line
        instead (see _issue_description/record_known_bug's own `title`
        param, same pattern on the bug side)."""
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("record_feature_request: %s has no github.com remote -- feature %r was NOT recorded anywhere", self.repo, description)
            return
        try:
            from agentra.connectors import github_issues

            body = f"Description: {description}\n\nSource: {source}"
            if external_id:
                body += f"\n\nExternal-ID: {external_id}"
            github_issues.create_issue(repo_url, title or description, body, labels=[_FEATURE_LABEL, _AGENTRA_LABEL])
        except Exception:
            logger.error("record_feature_request: failed to create a GitHub issue on %s -- feature %r was NOT recorded anywhere", repo_url, description, exc_info=True)

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

    # ── Settings: the current objective. Read by default so --objective becomes
    # optional; `agentra objective set/show` manages this file directly. ──────

    _OBJECTIVE_VARIABLE = "AGENTRA_OBJECTIVE"

    def get_objective(self) -> str | None:
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("get_objective: %s has no github.com remote -- no objective is configured", self.repo)
            return None
        try:
            from agentra.connectors import github_variables

            return github_variables.list_variables(repo_url).get(self._OBJECTIVE_VARIABLE)
        except Exception:
            logger.error("get_objective: GitHub Variables unavailable for %s -- objective is unreadable until it recovers", repo_url, exc_info=True)
            return None

    def set_objective(self, objective: str) -> None:
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("set_objective: %s has no github.com remote -- objective was NOT saved anywhere", self.repo)
            return
        try:
            from agentra.connectors import github_variables

            github_variables.set_variable(repo_url, self._OBJECTIVE_VARIABLE, objective)
        except Exception:
            logger.error("set_objective: failed to save to GitHub Variables for %s -- objective was NOT saved anywhere", repo_url, exc_info=True)

    # ── Feedback sync checkpoint: how far feedback_fetch_command has been read,
    # so each cycle only asks for what's new since last time. ────────────────

    def feedback_sync_state(self) -> dict:
        if not self.feedback_sync_state_path.exists():
            return {}
        return json.loads(self.feedback_sync_state_path.read_text())

    def update_feedback_sync_state(self, last_synced_at: str) -> None:
        self.feedback_sync_state_path.write_text(json.dumps({"last_synced_at": last_synced_at}, indent=2))

    # ── Codebase spec cache checkpoint: the commit SHA the last
    # architecture/codebase.md scan was generated at. Implementation Agent
    # is the only agent that commits source changes, so an unchanged HEAD
    # since this SHA means the repo genuinely hasn't moved and the cached
    # scan is still accurate -- see agents/codebase.py's run_cached(). ────

    def codebase_spec_commit(self) -> str | None:
        if not self.codebase_spec_commit_path.exists():
            return None
        return json.loads(self.codebase_spec_commit_path.read_text()).get("commit_sha")

    def set_codebase_spec_commit(self, commit_sha: str) -> None:
        self.codebase_spec_commit_path.write_text(json.dumps({"commit_sha": commit_sha}, indent=2))

    # ── Standups: TASK-019. "Yesterday" comes from these -- the run logs are
    # the one place with real per-event timestamps that isn't GitHub itself
    # (known_bugs/feature_queue are point-in-time snapshots of open issues,
    # not event logs; shipped_features() does carry a real closed_at per
    # entry now, via GitHub). "Today" is just the current backlog
    # (known_bugs/feature_queue/objective), read directly by the caller. ──

    def recent_log_lines(self, since: dt.datetime) -> list[str]:
        """Every timestamped line across all run logs at or after `since`,
        oldest first. The run logs are the only append-only, per-event
        record in .agentra/ itself -- known_bugs()/feature_queue() are
        current snapshots of GitHub's open issues, with no history of
        *when* an entry was added."""
        lines: list[str] = []
        if not self.log_root.is_dir():
            return lines
        for path in self.log_root.glob("*.log"):
            for raw_line in path.read_text().splitlines():
                if not raw_line.startswith("["):
                    continue
                ts_str, _, rest = raw_line[1:].partition("] ")
                try:
                    ts = dt.datetime.fromisoformat(ts_str)
                except ValueError:
                    continue
                if ts >= since:
                    lines.append(f"[{ts.isoformat()}] {rest}")
        lines.sort()
        return lines

    # Standup reports, the live standup channel, agent chat history, and
    # agent session continuity all moved to chat_store.py (server-side,
    # AGENTRA_HOME) -- see that module's docstring. They used to live here
    # as git-committed files under the target repo's own .agentra/, which
    # was never the right home for agentra's own operational chat state.

    def append_documentation(self, entry: str) -> None:
        """Appends one dated line to architecture/documentation.md's
        running changelog, instead of overwriting the whole file the way
        write() does -- the top of documentation.md is a static
        architecture description (human-edited via the dashboard's Edit
        App modal), the changelog lives below it as its own section so
        recording a shipped feature here never clobbers that description.
        This is the human-readable prose trail of what actually shipped,
        meant to accumulate as living documentation -- shipped_features()
        is the structured/queryable record of the same events, backed by
        closed GitHub issues rather than a JSON array nobody browses."""
        existing = self.read("architecture", "documentation") or ""
        marker = "## Changelog"
        if marker not in existing:
            existing = existing.rstrip() + f"\n\n{marker}\n" if existing else f"{marker}\n"
        date_str = dt.datetime.now(dt.timezone.utc).date().isoformat()
        updated = existing.rstrip() + f"\n- {date_str}: {entry}\n"
        self.write("architecture", "documentation", updated)

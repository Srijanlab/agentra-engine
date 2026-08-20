"""agentra.memory — persistent memory store scoped to the target repo.

The five memory categories originally planned in vision.md section 9
(architecture, decisions, features, metrics, failures) were audited:
decisions/features/metrics had zero readers (pure duplication of
documentation.md's changelog and git commit history); failures/*.md was
replaced by record_failure()'s triage policy (permanent failure → GitHub
Issue, transient → just logged). Only architecture/ remains, as live-maintained
steering files (codebase.md, design.md, testing-notes.md, documentation.md).

Known bugs, feature queue, shipped features, and objective all live directly
in GitHub Issues/Variables — no local .agentra/*.json mirror. _repo_url()
requires a github.com remote; there is deliberately no local-file fallback if
GitHub is unreachable, a known availability tradeoff.

Per-run logs, standup channel messages, and agent chat history moved to
chat_store.py (server-side, AGENTRA_HOME) — they were never a fit for a
customer's own git history.

Submodule responsibilities:
  core.py     — label constants, regex patterns, converter helpers
  issues.py   — MemoryIssuesMixin (bugs, failures, lifecycle tracking)
  features.py — MemoryFeaturesMixin (feature queue, shipped, released)
  settings.py — MemorySettingsMixin (objective, sync state, logs, docs)
"""

from __future__ import annotations

import datetime as dt
import logging
import subprocess
from pathlib import Path

from agentra.memory.core import (
    CATEGORIES,
    _ensure_gitignore,
    format_safety_denial_line,
    is_transient_failure,
    is_login_required_failure,
    cannot_be_fixed_by_agentra,
)
from agentra.memory.features import MemoryFeaturesMixin
from agentra.memory.issues import MemoryIssuesMixin
from agentra.memory.settings import MemorySettingsMixin

logger = logging.getLogger(__name__)


class Memory(MemoryIssuesMixin, MemoryFeaturesMixin, MemorySettingsMixin):
    """Repo-scoped memory. All GitHub-backed methods require a github.com
    HTTPS remote on `repo`; all filesystem-backed methods only need `repo`
    to exist locally."""

    def __init__(self, repo: Path) -> None:
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
        # The category directory can vanish between __init__ and this call —
        # implementation.py's _checkout_feature_branch does `git clean -fd .agentra/`
        # then `git checkout -B <feature_branch>`, and git doesn't track empty
        # directories, so a category with no committed file (e.g. a repo's first-ever
        # feature under memory/features/) simply doesn't come back after that checkout.
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
        """Per-run logs were the one data type with no durable copy anywhere —
        .agentra/logs/ is gitignored (verbose, not audit-trail material) and
        REPOS_ROOT is VM-local-only, so a run's log was permanently gone the
        moment its VM rebuilt. Best-effort and silently skipped if Firestore
        isn't configured, same as every other Firestore mirror in this codebase."""
        try:
            from agentra import registry
            from google.cloud import firestore as gcf

            db = registry.firestore_client()
            if db is None:
                return
            new_line = f"[{timestamp}] {content}"
            # Check document size before writing to avoid exceeding Firestore's
            # 1MB limit. Get current doc, estimate new size, truncate if needed.
            try:
                doc_ref = db.collection("run_logs").document(run_id)
                current = doc_ref.get()
                if current.exists:
                    lines = current.get("lines") or []
                    # Rough estimate: assume avg line ~100 bytes; if total lines
                    # would exceed 900KB (leaving 100KB buffer), keep only last 500
                    if len(lines) > 500:
                        lines = lines[-500:]
                    lines.append(new_line)
                    doc_ref.set({"lines": lines}, merge=True)
                else:
                    doc_ref.set({"lines": [new_line]}, merge=True)
            except Exception as e:
                # Fall back to ArrayUnion if size check fails
                try:
                    db.collection("run_logs").document(run_id).set(
                        {"lines": gcf.ArrayUnion([new_line])}, merge=True
                    )
                except Exception:
                    logger.warning("log: failed to mirror run %s log line to Firestore", run_id, exc_info=True)
        except Exception:
            logger.warning("log: failed to mirror run %s log line to Firestore", run_id, exc_info=True)

    def record_safety_denial(self, run_id: str, tool_name: str, pattern: str, detail: str) -> Path:
        """Durable audit trail for agents/safety.py's guarded_pre_tool_use:
        every blocked tool call gets a '[safety]'-tagged line in the run's log.
        In practice the hook calls format_safety_denial_line through the ambient
        run logger (base.py's run_log_scope) rather than this method — see
        agents/safety.py. This method exists for callers that hold a Memory instance."""
        return self.log(run_id, format_safety_denial_line(tool_name, pattern, detail))

    def _repo_url(self) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo), "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None


__all__ = [
    "Memory",
    "format_safety_denial_line",
    "is_transient_failure",
    "is_login_required_failure",
    "cannot_be_fixed_by_agentra",
]

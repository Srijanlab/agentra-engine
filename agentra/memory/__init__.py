"""agentra.memory — persistent memory store scoped to the target repo."""

from __future__ import annotations

import datetime as dt
import logging
import subprocess
import time
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
from agentra.memory.issue_lifecycle import MemoryIssueLifecycleMixin
from agentra.memory.issues import MemoryIssuesMixin
from agentra.memory.settings import MemorySettingsMixin

logger = logging.getLogger(__name__)

# Bug #77: a per-line Firestore read+write (blocking the async agent loop
# every ~200-300ms during SDK streaming) was replaced with an in-memory
# per-run buffer that is flushed to Firestore as a single full-document
# write only periodically and once at run completion.
FIRESTORE_FLUSH_MAX_LINES = 200
FIRESTORE_FLUSH_INTERVAL_SECONDS = 300
FIRESTORE_MAX_LINES = 500


class Memory(MemoryIssuesMixin, MemoryIssueLifecycleMixin, MemoryFeaturesMixin, MemorySettingsMixin):
    """Repo-scoped memory."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.root = repo / ".agentra"
        self.memory_root = self.root / "memory"
        self.log_root = self.root / "logs"
        self.released_path = self.root / "released.json"
        self.feedback_sync_state_path = self.root / "feedback_sync_state.json"
        self.codebase_spec_commit_path = self.root / "codebase_spec_commit.json"
        self._log_buffers: dict[str, list[str]] = {}
        self._log_buffer_meta: dict[str, dict[str, float]] = {}
        # Cloud mode: the engine has no writable checkout dir. The GitHub- and
        # Firestore-backed methods don't need these paths; the local-file ones
        # will raise OSError at call time, which callers handle.
        try:
            for category in CATEGORIES:
                (self.memory_root / category).mkdir(parents=True, exist_ok=True)
            self.log_root.mkdir(parents=True, exist_ok=True)
            _ensure_gitignore(self.root)
        except OSError:
            logger.debug("Memory(%s): read-only fs, skipping scaffold", repo)

    def write(self, category: str, name: str, content: str) -> Path:
        if category not in CATEGORIES:
            raise ValueError(f"unknown memory category: {category}")
        path = self.memory_root / category / f"{name}.md"
        # The category directory can vanish between __init__ and this call —
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
        line = f"[{timestamp}] {content}"
        with path.open("a") as f:
            f.write(line + "\n")
        self._buffer_log_line(run_id, line)
        return path

    def _buffer_log_line(self, run_id: str, line: str) -> None:
        """Appends to the run's in-memory buffer and triggers a periodic Firestore safety flush at a bounded cadence (default: every 200 lines or 5 minutes, whichever comes first) instead of on every call."""
        self._log_buffers.setdefault(run_id, []).append(line)
        meta = self._log_buffer_meta.setdefault(
            run_id, {"lines_since_flush": 0, "last_flush": time.monotonic()}
        )
        meta["lines_since_flush"] += 1
        due_by_lines = meta["lines_since_flush"] >= FIRESTORE_FLUSH_MAX_LINES
        due_by_time = (time.monotonic() - meta["last_flush"]) >= FIRESTORE_FLUSH_INTERVAL_SECONDS
        if due_by_lines or due_by_time:
            self._flush_run_log_to_firestore(run_id)

    def finalize_run_log(self, run_id: str) -> None:
        """Best-effort final Firestore durability flush for `run_id`, called once a run reaches a terminal state (completed, failed, or waiting_for_human)."""
        self._flush_run_log_to_firestore(run_id)
        self._log_buffers.pop(run_id, None)
        self._log_buffer_meta.pop(run_id, None)

    def _flush_run_log_to_firestore(self, run_id: str) -> None:
        """Per-run logs were the one data type with no durable copy anywhere — .agentra/logs/ is gitignored (verbose, not audit-trail material) and REPOS_ROOT is VM-local-only, so a run's log was permanently gone the moment its VM rebuilt. Writes the buffered lines as a single full-document overwrite (no per-line read-modify-write)."""
        meta = self._log_buffer_meta.get(run_id)
        try:
            lines = self._log_buffers.get(run_id) or []
            if not lines:
                return
            from agentra import registry

            db = registry.firestore_client()
            if db is None:
                return
            tail = lines[-FIRESTORE_MAX_LINES:]
            db.collection("run_logs").document(run_id).set({"lines": tail})
        except Exception:
            logger.warning("log: failed to flush run %s log to Firestore", run_id, exc_info=True)
        finally:
            if meta is not None:
                meta["lines_since_flush"] = 0
                meta["last_flush"] = time.monotonic()

    def record_safety_denial(self, run_id: str, tool_name: str, pattern: str, detail: str) -> Path:
        """Durable audit trail for agents/safety.py's guarded_pre_tool_use: every blocked tool call gets a '[safety]'-tagged line in the run's log."""
        return self.log(run_id, format_safety_denial_line(tool_name, pattern, detail))

    def _repo_url(self) -> str | None:
        from agentra import registry

        return registry.repo_url_for_path(self.repo)


__all__ = [
    "Memory",
    "format_safety_denial_line",
    "is_transient_failure",
    "is_login_required_failure",
    "cannot_be_fixed_by_agentra",
]

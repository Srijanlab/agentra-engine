"""memory/settings.py — MemorySettingsMixin.

Covers objective get/set (GitHub Variables), feedback sync state,
codebase spec cache, standup log reading, and documentation changelog append.
"""

from __future__ import annotations

import datetime as dt
import logging

logger = logging.getLogger(__name__)

from agentra.memory.core import _OBJECTIVE_VARIABLE


class MemorySettingsMixin:
    """Mixin for Memory: objective, feedback sync state, codebase spec commit,
    recent log lines, and documentation changelog. Assumes self._repo_url()
    is defined on the base class."""

    def get_objective(self) -> str | None:
        repo_url = self._repo_url()
        if not repo_url:
            logger.error("get_objective: %s has no github.com remote -- no objective is configured", self.repo)
            return None
        try:
            from agentra.connectors import github_variables

            return github_variables.list_variables(repo_url).get(_OBJECTIVE_VARIABLE)
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

            github_variables.set_variable(repo_url, _OBJECTIVE_VARIABLE, objective)
        except Exception:
            logger.error("set_objective: failed to save to GitHub Variables for %s -- objective was NOT saved anywhere", repo_url, exc_info=True)

    def feedback_sync_state(self) -> dict:
        if not self.feedback_sync_state_path.exists():
            return {}
        import json

        return json.loads(self.feedback_sync_state_path.read_text())

    def update_feedback_sync_state(self, last_synced_at: str) -> None:
        import json

        self.feedback_sync_state_path.write_text(json.dumps({"last_synced_at": last_synced_at}, indent=2))

    def codebase_spec_commit(self) -> str | None:
        """The commit SHA the last architecture/codebase.md scan was generated at.
        Implementation Agent is the only agent that commits source changes, so an
        unchanged HEAD since this SHA means the cached scan is still accurate —
        see agents/codebase.py's run_cached()."""
        if not self.codebase_spec_commit_path.exists():
            return None
        import json

        return json.loads(self.codebase_spec_commit_path.read_text()).get("commit_sha")

    def set_codebase_spec_commit(self, commit_sha: str) -> None:
        import json

        self.codebase_spec_commit_path.write_text(json.dumps({"commit_sha": commit_sha}, indent=2))

    def recent_log_lines(self, since: dt.datetime) -> list[str]:
        """Every timestamped line across all run logs at or after `since`,
        oldest first. Run logs are the only append-only, per-event record in
        .agentra/ itself — known_bugs()/feature_queue() are current snapshots
        of GitHub's open issues, with no history of *when* an entry was added."""
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

    def append_documentation(self, entry: str) -> None:
        """Appends one dated line to architecture/documentation.md's running
        changelog instead of overwriting the whole file — the top of
        documentation.md is a static architecture description (human-edited
        via the dashboard), the changelog lives below it so recording a
        shipped feature never clobbers that description."""
        existing = self.read("architecture", "documentation") or ""
        marker = "## Changelog"
        if marker not in existing:
            existing = existing.rstrip() + f"\n\n{marker}\n" if existing else f"{marker}\n"
        date_str = dt.datetime.now(dt.timezone.utc).date().isoformat()
        updated = existing.rstrip() + f"\n- {date_str}: {entry}\n"
        self.write("architecture", "documentation", updated)

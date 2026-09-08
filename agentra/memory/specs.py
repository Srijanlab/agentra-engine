"""memory/specs.py — per-repo `.agentra/` spec files and the freshness ledger.

These are LOCAL-FILE, never proxied to the engine: each code repo owns its own
`.agentra/architecture.md` / `design.md` / `testing.md` / `state.json`, committed
to that repo (see docs/agentra-spec.md and deployment.persist_repo_specs).
"""

from __future__ import annotations

import json
from pathlib import Path

from agentra.memory.core import SPEC_FILES


class MemorySpecsMixin:
    """Mixed into Memory. Roots at `self.root` (`.agentra/`), not `.agentra/memory/`."""

    root: Path
    state_path: Path

    def spec_path(self, name: str) -> Path:
        if name not in SPEC_FILES:
            raise ValueError(f"unknown spec file: {name!r} (expected one of {SPEC_FILES})")
        return self.root / f"{name}.md"

    def read_spec(self, name: str) -> str | None:
        path = self.spec_path(name)
        return path.read_text() if path.exists() else None

    def write_spec(self, name: str, content: str) -> Path:
        path = self.spec_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if content.endswith("\n") else content + "\n")
        return path

    def read_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text())
        except (ValueError, OSError):
            return {}

    def write_state(self, patch: dict) -> None:
        """Shallow-merge `patch` into `.agentra/state.json`."""
        state = self.read_state()
        state.update(patch)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2) + "\n")

    def indexed_sha(self) -> str | None:
        """The commit the architecture/design templates were last built from."""
        return self.read_state().get("indexed_sha")

    def spec_synced_sha(self) -> str | None:
        """The commit `state.json` itself was last committed at."""
        return self.read_state().get("spec_synced_sha")

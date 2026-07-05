"""Persistent memory store, scoped to the target repo being improved.

Mirrors the layout from vision.md section 9: architecture, decisions,
features, metrics, failures. Lives at <repo>/.agentos/memory/ so it travels
with the repo and accumulates across runs.
"""

import datetime as dt
from pathlib import Path

CATEGORIES = ("architecture", "decisions", "features", "metrics", "failures")


class Memory:
    def __init__(self, repo: Path):
        self.root = repo / ".agentos"
        self.memory_root = self.root / "memory"
        self.log_root = self.root / "logs"
        for category in CATEGORIES:
            (self.memory_root / category).mkdir(parents=True, exist_ok=True)
        self.log_root.mkdir(parents=True, exist_ok=True)

    def write(self, category: str, name: str, content: str) -> Path:
        if category not in CATEGORIES:
            raise ValueError(f"unknown memory category: {category}")
        path = self.memory_root / category / f"{name}.md"
        path.write_text(content)
        return path

    def read(self, category: str, name: str) -> str | None:
        path = self.memory_root / category / f"{name}.md"
        return path.read_text() if path.exists() else None

    def log(self, run_id: str, content: str) -> Path:
        path = self.log_root / f"{run_id}.log"
        with path.open("a") as f:
            timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
            f.write(f"[{timestamp}] {content}\n")
        return path

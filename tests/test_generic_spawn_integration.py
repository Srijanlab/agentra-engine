"""Real integration test for agents/generic.py::spawn — confirms a task spec
defined entirely at call time (no dedicated agent module/class) actually runs
via the same query() path every other agent in this codebase uses, and that
its outcome lands in the normal Memory.log() ledger.

Same convention as tests/test_safety_integration.py: costs a small amount of
real API usage, not run automatically. Run explicitly:
    python tests/test_generic_spawn_integration.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentos.agents.generic import TaskSpec, spawn
from agentos.memory import Memory


async def main() -> None:
    failures = []

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        spec = TaskSpec(
            name="marker_check",
            prompt='Run this exact single bash command in one Bash tool call and report what it printed: echo "SPAWN_MARKER_7c1a"',
            system_prompt="You are a test agent. Follow the user's instructions exactly, using Bash.",
            allowed_tools=["Bash"],
            max_turns=5,
        )
        result = await spawn(repo, spec, run_id="test-run")

        if not result.ok:
            failures.append(f"spawn() reported ok=False: {result.text[:500]!r}")
        elif "SPAWN_MARKER_7c1a" not in result.text:
            failures.append(f"expected marker not found in result text: {result.text[:500]!r}")
        else:
            print("Case 1 PASS: ad hoc TaskSpec ran via spawn() and the expected output came back")

        log_path = Memory(repo).log_root / "test-run.log"
        if not log_path.exists():
            failures.append("Case 2 FAILED: no log file written for the spawn run")
        else:
            log_text = log_path.read_text()
            if "spawn[marker_check]: starting" not in log_text or "spawn[marker_check]: ok=" not in log_text:
                failures.append(f"Case 2 FAILED: log file missing expected start/end entries: {log_text!r}")
            else:
                print("Case 2 PASS: spawn start/end were logged under the task's name in the normal ledger")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f" - {f}")
        sys.exit(1)
    print("\nAll generic spawn integration checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

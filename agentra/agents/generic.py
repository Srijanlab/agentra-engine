"""Generic sub-agent spawning — a task spec (prompt + tool/permission profile) in, an AgentResult out, with no dedicated Python module or class required per task type."""

from dataclasses import dataclass, field
from pathlib import Path

from agentra.agents.base import AgentResult, run_agent
from agentra.agents.git_ops import GitOpError, pull_latest, push_branch
from agentra.memory import Memory


@dataclass
class TaskSpec:
    """One arbitrary unit of sub-agent work."""

    name: str
    prompt: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=lambda: ["Read", "Glob", "Grep"])
    permission_mode: str = "bypassPermissions"
    max_turns: int | None = None
    # Deliberately no allow_prod field here. Every other place in this codebase
    retry_on_contradictory_result: bool = True
    # TASK-014: deterministic git plumbing, matching implementation.py's own
    pull_before: str | None = None
    push_after: str | None = None


async def spawn(repo: Path, spec: TaskSpec, mem: Memory | None = None, run_id: str | None = None) -> AgentResult:
    """Run one ad hoc sub-agent task to completion."""
    if mem is None:
        mem = Memory(repo)
    if run_id is None:
        import uuid

        run_id = uuid.uuid4().hex[:8]

    if spec.pull_before:
        try:
            pull_latest(repo, spec.pull_before)
            mem.log(run_id, f"spawn[{spec.name}]: pulled latest {spec.pull_before!r}")
        except GitOpError as exc:
            mem.log(run_id, f"spawn[{spec.name}]: {exc}")
            return AgentResult(ok=False, text=str(exc), json_data=None, cost_usd=0.0, turns=0)

    mem.log(run_id, f"spawn[{spec.name}]: starting | tools={spec.allowed_tools} permission_mode={spec.permission_mode!r}")
    result = await run_agent(
        prompt=spec.prompt,
        system_prompt=spec.system_prompt,
        cwd=repo,
        allowed_tools=spec.allowed_tools,
        permission_mode=spec.permission_mode,
        max_turns=spec.max_turns,
        allow_prod=False,
        retry_on_contradictory_result=spec.retry_on_contradictory_result,
        agent_label=f"Custom Agent: {spec.name}",
    )
    mem.log(run_id, f"spawn[{spec.name}]: ok={result.ok} turns={result.turns} cost=${result.cost_usd:.4f}")

    if spec.push_after and result.ok:
        try:
            push_branch(repo, spec.push_after)
            mem.log(run_id, f"spawn[{spec.name}]: pushed {spec.push_after!r}")
        except GitOpError as exc:
            # The agent turn itself succeeded -- don't discard that outcome,
            mem.log(run_id, f"spawn[{spec.name}]: {exc}")
            result.ok = False
            result.text += f"\n\n[agentra] {exc}"

    return result

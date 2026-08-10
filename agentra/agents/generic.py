"""Generic sub-agent spawning — a task spec (prompt + tool/permission profile)
in, an AgentResult out, with no dedicated Python module or class required per
task type.

Every specialized agent in this package (codebase.py, discovery.py, etc.) is
already just a fixed system_prompt + allowed_tools wired into
agents/base.py::run_agent() — this module is that same mechanism made
directly callable for a task the orchestrator thinks up at runtime, instead
of requiring a new .py file for every new kind of work. The eight existing
specialized agents are unaffected: they keep their own modules (clearer
system prompts, dedicated pre/post logic like implementation.py's branch
checkout), this is the path for everything that doesn't warrant one.

Traceability: every spawn is logged through the same Memory.log() ledger
every other agent call uses (<repo>/.agentra/logs/<run_id>.log), tagged with
the task's name and its outcome — so a spawned run is inspectable the same
way as a fixed-pipeline one, not a shadow path with its own bookkeeping.
"""

from dataclasses import dataclass, field
from pathlib import Path

from agentra.agents.base import AgentResult, run_agent
from agentra.memory import Memory


@dataclass
class TaskSpec:
    """One arbitrary unit of sub-agent work.

    name is a short, log-friendly identifier for this task type (e.g.
    "dependency_audit") — not a run id, which spawn() generates or accepts
    separately.
    """

    name: str
    prompt: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=lambda: ["Read", "Glob", "Grep"])
    permission_mode: str = "bypassPermissions"
    max_turns: int | None = None
    # Deliberately no allow_prod field here. Every other place in this codebase
    # that can touch production (orchestrator.py's auto-remediate path,
    # agents/deployment.py::promote_prod) does so through one explicit,
    # narrowly-scoped call — never a general-purpose spawn path. A caller that
    # genuinely needs prod access should use that path, not this one.
    retry_on_contradictory_result: bool = True


async def spawn(repo: Path, spec: TaskSpec, mem: Memory | None = None, run_id: str | None = None) -> AgentResult:
    """Run one ad hoc sub-agent task to completion.

    mem/run_id let a caller that already has an active Memory/run_id (e.g.
    brain.py's OrchestratorSession) fold this spawn into its existing log
    file instead of starting a new one; omit both for a one-off, standalone
    spawn (e.g. from a script or the CLI), which then logs under its own
    fresh run id.
    """
    if mem is None:
        mem = Memory(repo)
    if run_id is None:
        import uuid

        run_id = uuid.uuid4().hex[:8]

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
    )
    mem.log(run_id, f"spawn[{spec.name}]: ok={result.ok} turns={result.turns} cost=${result.cost_usd:.4f}")
    return result

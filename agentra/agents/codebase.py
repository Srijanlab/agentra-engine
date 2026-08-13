"""Codebase Understanding Agent (vision.md 5.2).

Read-only scan of the target repo: framework, architecture, existing
features. Output feeds every downstream agent, so it never touches Write/Edit/Bash.
"""

import subprocess
from pathlib import Path

from agentra.agents.base import AgentResult, extract_json_block, run_agent
from agentra.memory import Memory

SYSTEM_PROMPT = """You are the Codebase Understanding Agent in an autonomous \
product engineering system. Your only job is to build an accurate, concise \
picture of the repository you are pointed at. You are read-only: never \
propose edits, never run mutating commands.

Investigate:
- Framework(s) and language(s) in use
- Overall architecture (monolith, serverless, microservices, etc.)
- Backend/data layer
- Existing user-facing features
- Test and build tooling already configured (so later agents know what to run)

End your response with a fenced ```json block shaped like:
{
  "framework": "...",
  "backend": "...",
  "architecture": "...",
  "features": ["...", "..."],
  "test_commands": ["..."],
  "build_commands": ["..."],
  "notes": "..."
}
"""


async def run(repo: Path) -> AgentResult:
    prompt = (
        "Scan this repository and produce the codebase understanding summary "
        "described in your system prompt."
    )
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        agent_label="Codebase Agent",
    )


def _current_head_sha(repo: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


async def run_cached(repo: Path, mem: Memory) -> AgentResult:
    """Like run(), but skips the (real, multi-turn) LLM scan and reuses the
    last generated summary when HEAD hasn't moved since it was generated.

    Implementation Agent is the only agent that ever commits source
    changes (agents/implementation.py) -- every other agent, including
    this one, is read-only or deploy-only. So an unchanged HEAD since the
    cached scan's commit SHA means the repo genuinely hasn't changed, and
    re-running a full repo exploration would just reproduce the same
    summary at the cost of another real agent turn. Every call site that
    used to do `cb = await codebase.run(repo); mem.write(...)` should call
    this instead -- the mem.write() on a fresh generation happens here too,
    so callers don't need to duplicate that."""
    head = _current_head_sha(repo)
    cached_sha = mem.codebase_spec_commit()
    if head is not None and head == cached_sha:
        cached_text = mem.read("architecture", "codebase")
        if cached_text:
            return AgentResult(
                ok=True,
                text=cached_text,
                json_data=extract_json_block(cached_text),
                cost_usd=0.0,
                turns=0,
            )

    result = await run(repo)
    if result.ok:
        mem.write("architecture", "codebase", result.text)
        new_head = _current_head_sha(repo)
        if new_head is not None:
            mem.set_codebase_spec_commit(new_head)
    return result

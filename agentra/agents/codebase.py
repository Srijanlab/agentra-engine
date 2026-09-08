"""Codebase Understanding Agent — maintains each code repo's `.agentra/` spec.

Writes `.agentra/architecture.md` (fixed template) + `design.md` and records the
commit it was built from in `.agentra/state.json` (`indexed_sha`). `sync_spec`
only spends an LLM turn when that repo's HEAD moved since `indexed_sha` -- and
then it does a bounded *delta* update, not a full rewrite. See docs/agentra-spec.md.
"""

import subprocess
from dataclasses import replace
from pathlib import Path

from agentra.agents.base import AgentResult, extract_json_block, run_agent
from agentra.memory import Memory
from agentra.memory.core import spec_header, strip_spec_header

_ARCH_TEMPLATE = """\
# {repo} — Architecture

## Purpose
## Stack
## Module map
## Invariants
## Conventions
## Gotchas
"""

SYSTEM_PROMPT = """You are the Codebase Understanding Agent in an autonomous \
product engineering system. You maintain one file: `.agentra/architecture.md` for \
the repository you are pointed at. You are read-only: never propose edits, never \
run mutating commands.

`architecture.md` has EXACTLY these six sections, in this order, nothing else:

## Purpose      -- one or two sentences: what this repo is and who/what consumes it
## Stack        -- languages, frameworks, build/test tooling actually configured
## Module map   -- the top-level packages/dirs and what each is responsible for
## Invariants   -- rules the code depends on staying true (e.g. "loop reaches all \
state via /internal RPC, never touches the datastore directly")
## Conventions  -- patterns a new change is expected to follow
## Gotchas      -- real footguns visible in the code, NOT generic best-practice advice

Keep every section terse. The whole file must stay under ~200 lines. Report only \
what the code itself demonstrates, not guesses about intent.

You are given a MODE:

- MODE=full  -- produce the whole file from scratch.
- MODE=delta -- you are given the CURRENT architecture.md body and the git diff \
since it was last built. Update ONLY the sections the diff actually affects. Copy \
every other section through byte-for-byte. Do not rewrite prose the diff doesn't \
touch. Keep the file bounded.

End your response with a fenced ```json block:
{
  "mode": "full" | "delta",
  "architecture_md": "the full six-section file body (no provenance header)",
  "design_md": "a short paragraph or bullet list of concrete design decisions/patterns actually in the code -- for .agentra/design.md",
  "test_commands": ["..."],
  "build_commands": ["..."]
}
"""


def _current_head_sha(repo: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _git_diff(repo: Path, base: str, head: str | None) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "diff", "--stat", "-p", f"{base}..{head or 'HEAD'}"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return ""
    return result.stdout[:24000] if result.returncode == 0 else ""


def _joined(architecture: str | None, design: str | None) -> str:
    """The string later agents receive as `codebase_summary`: the architecture
    body plus a short design tail, both header-stripped."""
    parts = []
    if architecture:
        parts.append(strip_spec_header(architecture).strip())
    if design:
        parts.append("## Design notes\n" + strip_spec_header(design).strip())
    return "\n\n".join(parts)


def _testing_stub(head: str | None, data: dict) -> str:
    cmds = data.get("test_commands") or []
    builds = data.get("build_commands") or []
    lines = [spec_header("agent:testing", head), "# Testing", "", "## Local test recipe"]
    for c in cmds:
        lines.append(f"- test: `{c}`")
    for b in builds:
        lines.append(f"- build: `{b}`")
    lines += ["", "## Last run", "(none yet)"]
    return "\n".join(lines)


async def run(
    repo: Path,
    *,
    mode: str = "full",
    current_architecture: str | None = None,
    diff: str | None = None,
) -> AgentResult:
    if mode == "delta":
        prompt = (
            f"MODE=delta\n\nCURRENT architecture.md body:\n{current_architecture or '(missing)'}\n\n"
            f"Git diff since it was last built:\n{diff or '(no diff available)'}\n\n"
            "Apply the minimal update described in your system prompt."
        )
    else:
        prompt = (
            "MODE=full\n\nScan this repository and produce the full architecture.md "
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


async def sync_spec(
    repo: Path, mem: Memory, *, owner_repo_name: str, force_full: bool = False
) -> AgentResult:
    """Ensure `repo`'s `.agentra/architecture.md` + `design.md` + `state.json` are
    current. HEAD == state.indexed_sha and not force_full -> return the cached
    spec at cost 0, no LLM. Otherwise a full (first time / force) or delta
    (HEAD moved) scan. Does NOT git-commit -- deployment.persist_repo_specs does."""
    head = _current_head_sha(repo)
    cached_arch = mem.read_spec("architecture")
    indexed = mem.indexed_sha()

    if cached_arch and not force_full and indexed == head:
        return AgentResult(
            ok=True,
            text=_joined(cached_arch, mem.read_spec("design")),
            json_data=extract_json_block(cached_arch),
            cost_usd=0.0,
            turns=0,
        )

    if cached_arch and indexed and not force_full:
        result = await run(
            repo, mode="delta",
            current_architecture=strip_spec_header(cached_arch),
            diff=_git_diff(repo, indexed, head),
        )
    else:
        result = await run(repo, mode="full")

    if not result.ok:
        return result  # nothing written; state untouched

    data = result.json_data or {}
    arch_body = (data.get("architecture_md") or "").strip() or _ARCH_TEMPLATE.format(repo=owner_repo_name)
    mem.write_spec("architecture", spec_header("agent:codebase", head) + arch_body)
    design_body = (data.get("design_md") or "").strip()
    if design_body:
        mem.write_spec("design", spec_header("agent:codebase", head) + design_body)
    if mem.read_spec("testing") is None and (data.get("test_commands") or data.get("build_commands")):
        mem.write_spec("testing", _testing_stub(head, data))
    mem.write_state({"indexed_sha": head})

    return replace(result, text=_joined(mem.read_spec("architecture"), mem.read_spec("design")))


async def run_cached(repo: Path, mem: Memory, cache_key: str = "codebase") -> AgentResult:
    """Deprecated shim for the legacy linear orchestrator (agentra/orchestrator.py).
    The brain uses sync_spec directly, per code repo."""
    return await sync_spec(
        repo, mem, owner_repo_name=(cache_key if cache_key != "codebase" else repo.name)
    )

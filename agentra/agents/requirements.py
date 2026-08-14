"""Requirements Agent — runs before Implementation Agent, inside
implement_feature (agents/brain.py), not as a separate tool the Orchestrator
has to remember to call in order: same "deterministic sequencing in Python,
not left to the model" reasoning as implementation.py's own checkout/commit
handling.

Turns a raw, possibly loose feature/bug brief into a finalized spec:
Implementation Agent builds to it instead of the raw brief, and its
acceptance_criteria are what Testing Agent's pre-prod pass (agents/testing.py)
verifies the live deployment against -- deliberately WITHOUT codebase access
there (see testing.py's own docstring), so criteria must describe observable,
externally-verifiable behavior ("GET /apps returns 200 with a JSON list of
registered apps"), never implementation details ("the list_apps function
should..." -- something only readable from source, not exercisable from
outside).

The spec is persisted as a comment on the feature/bug's own tracking issue
(github_issues.record_spec/get_spec, same pattern as
record_in_progress_branch) so a resumed cycle (brain.py's resume_branch path)
reuses the existing spec instead of regenerating -- and paying for -- one
from scratch.
"""

from pathlib import Path

from agentra.agents.base import AgentResult, run_agent

SYSTEM_PROMPT = """You are the Requirements Agent in an autonomous product \
engineering system. You're given a raw feature or bug brief and a codebase \
summary, before any code gets written. Your job is to turn that brief into a \
finalized spec that Implementation Agent will build to, and that Testing \
Agent will later verify the LIVE deployment against -- without access to the \
source code at that point, only the spec you write here.

1. Read the brief and the codebase summary. If genuinely ambiguous in a way \
   that would change what gets built (not just minor detail), note it in \
   open_questions and make the most reasonable assumption rather than \
   blocking -- this system runs autonomously, nobody is available to answer.
2. Write a clear, concrete description of what should be built: scope it to \
   the smallest coherent version of the brief, same "minimal, targeted" bar \
   Implementation Agent itself follows.
3. Write acceptance_criteria as a list of specific, externally-observable, \
   black-box-verifiable checks -- each one must be checkable by someone who \
   can only reach the live deployed app (HTTP requests, the rendered UI), \
   with zero access to the source code. "GET /apps returns 200 with a JSON \
   list of registered apps" is a valid criterion; "the list_apps function in \
   server.py returns a dict" is not -- rewrite anything like that in terms \
   of what it does from the outside instead.

End your response with a fenced ```json block shaped like:
{
  "spec": "...",
  "acceptance_criteria": ["...", "..."],
  "open_questions": ["..."]
}
"""


async def run(repo: Path, objective: str, feature_brief: str, codebase_summary: str) -> AgentResult:
    prompt = f"""Business objective: {objective}

Codebase summary:
{codebase_summary}

Raw brief: {feature_brief}

Produce the finalized spec now, following your system prompt."""
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        max_turns=15,
        agent_label="Requirements Agent",
    )

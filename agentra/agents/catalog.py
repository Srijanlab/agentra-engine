"""Per-agent capability metadata for the dashboard's Agent card -- what
each agent can actually do, which tools it's granted, and what permission
tier each tool carries. Read by GET /agents/metadata (server.py) and
AgentsPanel.tsx's card, replacing a per-agent "recent activity" feed with a
durable profile that doesn't depend on any run having happened yet.

Kept as one hand-maintained table rather than derived from each agent
module's allowed_tools at import time: allowed_tools describes capability,
not intent, and the skills prose here is what actually makes a card
readable -- that doesn't exist as structured data anywhere else. When an
agent's system prompt or allowed_tools changes, update its entry here too.

Every agent below runs with permission_mode="bypassPermissions" (agents/
base.py) -- there is no interactive "approve this tool call" step. The
"permission" tier on each tool describes what class of action it grants,
not an approval gate; the actual safety boundary is the Docker sandbox
each agent runs inside plus agents/safety.py's PreToolUse hook, which
blocks destructive Bash/Write/Edit patterns regardless of this table.
"""

from typing import TypedDict


class ToolPermission(TypedDict):
    name: str
    permission: str  # "read" | "write" | "execute" | "network" | "delegate"


class AgentMeta(TypedDict):
    skills: list[str]
    tools: list[ToolPermission]


_TOOL_PERMISSION = {
    "Read": "read",
    "Glob": "read",
    "Grep": "read",
    "Write": "write",
    "Edit": "write",
    "Bash": "execute",
    "WebSearch": "network",
}


def _tools(*names: str) -> list[ToolPermission]:
    return [{"name": n, "permission": _TOOL_PERMISSION[n]} for n in names]


def _delegated(*names: str) -> list[ToolPermission]:
    """The orchestrator's own tool surface -- not raw Claude tools, but
    named actions that each delegate to one specialized agent's full run
    (agents/brain.py's @tool-decorated functions)."""
    return [{"name": n, "permission": "delegate"} for n in names]


AGENT_METADATA: dict[str, AgentMeta] = {
    "orchestrator": {
        "skills": [
            "Scans the repo and refreshes the shared codebase understanding summary",
            "Reviews the backlog: shipped features, known bugs, and the feature request queue",
            "Asks the Discovery Agent for ranked feature opportunities",
            "Directs the Implementation Agent to build one concrete feature",
            "Requires local tests to pass before it will allow a pre-prod deploy",
            "Deploys to pre-prod and independently verifies the live deployment",
            "Assesses whether a shipped feature is actually measurable",
            "Can spawn a one-off custom sub-agent for tasks outside the standard pipeline",
        ],
        "tools": _delegated(
            "understand_codebase",
            "check_backlog",
            "discover_opportunities",
            "implement_feature",
            "run_local_tests",
            "deploy_pre_prod",
            "verify_pre_prod",
            "assess_feedback",
            "spawn_custom_agent",
        ),
    },
    "codebase": {
        "skills": [
            "Scans the repo to identify framework, language, and architecture",
            "Maps the backend/data layer and existing user-facing features",
            "Identifies test and build tooling already configured",
            "Strictly read-only -- never proposes or makes edits",
        ],
        "tools": _tools("Read", "Glob", "Grep"),
    },
    "discovery": {
        "skills": [
            "Ranks 3-5 feature opportunities from the codebase, analytics, and backlog",
            "Always prioritizes known production bugs first",
            "Then prioritizes the customer/admin feature request queue",
            "Only proposes its own autonomous ideas once bugs and requests are covered",
            "Uses WebSearch to check comparable products before citing a competitor gap",
        ],
        "tools": _tools("Read", "Glob", "Grep", "WebSearch"),
    },
    "implementation": {
        "skills": [
            "Implements the smallest coherent version of one specific feature",
            "Runs the project's own test/build commands and self-corrects until green",
            "Works exclusively on its own dedicated feature branch",
            "Commits its change once tests pass -- never pushes, never opens a PR",
        ],
        "tools": _tools("Read", "Write", "Edit", "Glob", "Grep", "Bash"),
    },
    "testing": {
        "skills": [
            "Local mode: independently runs lint, typecheck, and unit/integration tests",
            "Local mode: runs the project's configured e2e/browser tests against a local dev server",
            "Pre-prod mode: verifies the live deployed pre-prod URL after a deploy",
            "Never modifies source files -- reports bugs rather than patching them",
        ],
        "tools": _tools("Read", "Bash", "Glob", "Grep"),
    },
    "deployment": {
        "skills": [
            "Merges the feature branch into pre-prod and pushes (deterministic git, not LLM-driven)",
            "Deploys to the isolated Firebase pre-prod project and/or a Vercel preview",
            "The only agent allowed to touch production, and only via an explicit promote call",
            "Interprets vercel/firebase CLI output and captures preview URLs",
        ],
        "tools": _tools("Read", "Bash", "Glob", "Grep"),
    },
    "feedback": {
        "skills": [
            "Checks whether a newly shipped feature actually emits analytics events",
            "Names the 2-3 metrics that would prove or disprove the feature's impact",
            "Flags missing instrumentation as a real gap rather than skipping it",
        ],
        "tools": _tools("Read", "Glob", "Grep"),
    },
    "prod_debug": {
        "skills": [
            "Investigates a reported or suspected production issue",
            "Inspects Vercel/Firebase logs and correlates errors with recent commits",
            "Proposes a root cause hypothesis and a concrete, minimally-scoped fix",
            "Strictly read-only against production -- never deploys or modifies prod config",
        ],
        "tools": _tools("Read", "Bash", "Glob", "Grep"),
    },
    "custom": {
        "skills": [
            "One-off sub-agent spawned by the Orchestrator for tasks outside the standard pipeline",
            "Its prompt, role, and tool grant are defined fresh by the Orchestrator on each spawn",
            "Never has production access, regardless of which tools it's granted",
        ],
        "tools": _tools("Read", "Glob", "Grep"),
    },
}

PERMISSION_MODEL_NOTE = (
    "Every agent runs unattended (no per-call approval prompt) inside a "
    "Docker sandbox, with a PreToolUse safety hook blocking destructive "
    "Bash/Write/Edit patterns regardless of which tools are listed below."
)

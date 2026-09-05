"""Per-agent capability metadata for the dashboard's Agent card -- what each agent can actually do, which tools it's granted, and what permission tier each tool carries."""

from typing import TypedDict


class ToolPermission(TypedDict):
    name: str
    permission: str  # "read" | "write" | "execute" | "network" | "delegate"


class AgentMeta(TypedDict):
    skills: list[str]
    tools: list[ToolPermission]
    capability: str


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
    """The orchestrator's own tool surface -- not raw Claude tools, but..."""
    return [{"name": n, "permission": "delegate"} for n in names]


AGENT_METADATA: dict[str, AgentMeta] = {
    "orchestrator": {
        "skills": [
            "Scans the repo and refreshes the shared codebase understanding summary",
            "Reviews the backlog: shipped features, known bugs, and the feature request queue",
            "Asks the Discovery Agent for ranked feature opportunities",
            "Directs the Implementation Agent to build one concrete feature",
            "Resumes an already-coded backlog item straight to delivery, skipping re-implementation",
            "Requires local tests to pass before it will allow a pre-prod deploy",
            "Deploys to pre-prod and independently verifies the live deployment",
            "Assesses whether a shipped feature is actually measurable",
            "Can spawn a one-off custom sub-agent for tasks outside the standard pipeline",
            "Calls the Architecture Review Agent first for architecturally significant features (schema/API/cross-cutting changes)",
        ],
        "tools": _delegated(
            "understand_codebase",
            "check_backlog",
            "discover_opportunities",
            "assess_design_impact",
            "implement_feature",
            "resume_delivery",
            "run_local_tests",
            "deploy_pre_prod",
            "verify_pre_prod",
            "assess_feedback",
            "spawn_custom_agent",
        ),
        "capability": "orchestration",
    },
    "codebase": {
        "skills": [
            "Scans the repo to identify framework, language, and architecture",
            "Maps the backend/data layer and existing user-facing features",
            "Identifies test and build tooling already configured",
            "Strictly read-only -- never proposes or makes edits",
        ],
        "tools": _tools("Read", "Glob", "Grep"),
        "capability": "analysis",
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
        "capability": "product_discovery",
    },
    "architecture_review": {
        "skills": [
            "Assesses architectural blast radius of a proposed feature before it's built",
            "Flags schema changes, new API surfaces, and cross-cutting refactors as higher risk",
            "Names concrete concerns rather than a generic risk score",
            "Judges infra_cost_impact (none/low/material) with a concrete one-line reason",
            "Strictly read-only -- never proposes or makes edits, purely advisory to the Orchestrator",
        ],
        "tools": _tools("Read", "Glob", "Grep"),
        "capability": "architecture_review",
    },
    "human_answer_judge": {
        "skills": [
            "Reads a human's dashboard/Slack answer to a blocking infra-cost question",
            "Decides whether the answer authorizes proceeding, or is still unresolved",
            "Never overrules the human's decision -- only checks whether it was made",
            "Strictly read-only -- purely advisory to the infra-cost gate",
        ],
        "tools": [],
        "capability": "human_answer_judgment",
    },
    "implementation": {
        "skills": [
            "Implements the smallest coherent version of one specific feature",
            "Runs the project's own test/build commands and self-corrects until green",
            "Works exclusively on its own dedicated feature branch",
            "Commits its change once tests pass -- never pushes, never opens a PR",
        ],
        "tools": _tools("Read", "Write", "Edit", "Glob", "Grep", "Bash"),
        "capability": "coding",
    },
    "testing": {
        "skills": [
            "Local mode: independently runs lint, typecheck, and unit/integration tests",
            "Local mode: runs the project's configured e2e/browser tests against a local dev server",
            "Pre-prod mode: verifies the live deployed pre-prod URL after a deploy",
            "Never modifies source files -- reports bugs rather than patching them",
        ],
        "tools": _tools("Read", "Bash", "Glob", "Grep"),
        "capability": "testing",
    },
    "deployment": {
        "skills": [
            "Merges the feature branch into pre-prod and pushes (deterministic git, not LLM-driven)",
            "Deploys to the isolated Firebase pre-prod project and/or a Vercel preview",
            "The only agent allowed to touch production, and only via an explicit promote call",
            "Interprets vercel/firebase CLI output and captures preview URLs",
        ],
        "tools": _tools("Read", "Bash", "Glob", "Grep"),
        "capability": "deployment",
    },
    "feedback": {
        "skills": [
            "Checks whether a newly shipped feature actually emits analytics events",
            "Names the 2-3 metrics that would prove or disprove the feature's impact",
            "Flags missing instrumentation as a real gap rather than skipping it",
        ],
        "tools": _tools("Read", "Glob", "Grep"),
        "capability": "analytics",
    },
    "prod_debug": {
        "skills": [
            "Investigates a reported or suspected production issue",
            "Inspects Vercel/Firebase logs and correlates errors with recent commits",
            "Proposes a root cause hypothesis and a concrete, minimally-scoped fix",
            "Strictly read-only against production -- never deploys or modifies prod config",
        ],
        "tools": _tools("Read", "Bash", "Glob", "Grep"),
        "capability": "debugging",
    },
    "custom": {
        "skills": [
            "One-off sub-agent spawned by the Orchestrator for tasks outside the standard pipeline",
            "Its prompt, role, and tool grant are defined fresh by the Orchestrator on each spawn",
            "Never has production access, regardless of which tools it's granted",
        ],
        "tools": _tools("Read", "Glob", "Grep"),
        "capability": "general",
    },
}

PERMISSION_MODEL_NOTE = (
    "Every agent runs unattended (no per-call approval prompt) inside a "
    "Docker sandbox, with a PreToolUse safety hook blocking destructive "
    "Bash/Write/Edit patterns regardless of which tools are listed below."
)

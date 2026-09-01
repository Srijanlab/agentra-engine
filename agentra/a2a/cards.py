"""Build A2A AgentCards for Agentra's specialized agents from the catalog registry."""

from __future__ import annotations

from agentra.a2a.agent_card import AgentCard, AgentProvider, AgentSkill
from agentra.agents import catalog

SPECIALIZED_AGENT_IDS: list[str] = [
    "codebase",
    "discovery",
    "architecture_review",
    "human_answer_judge",
    "implementation",
    "testing",
    "deployment",
    "feedback",
    "prod_debug",
]

_TEXT_MODES = ["text/plain"]
_PROVIDER = AgentProvider(organization="Agentra", url="https://github.com/")


def _title(agent_id: str) -> str:
    return agent_id.replace("_", " ").title()


def _short(sentence: str, limit: int = 72) -> str:
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def _describe(agent_id: str, meta: catalog.AgentMeta) -> str:
    return f"Agentra {_title(agent_id)} agent — {meta['capability'].replace('_', ' ')} capability."


def _skills_for(agent_id: str, meta: catalog.AgentMeta) -> list[AgentSkill]:
    skills: list[AgentSkill] = []
    for i, sentence in enumerate(meta["skills"], start=1):
        skills.append(
            AgentSkill(
                id=f"{agent_id}.skill.{i}",
                name=_short(sentence),
                description=sentence,
                tags=[meta["capability"]],
                input_modes=_TEXT_MODES,
                output_modes=_TEXT_MODES,
            )
        )
    for tool in meta["tools"]:
        skills.append(
            AgentSkill(
                id=f"{agent_id}.tool.{tool['name'].lower()}",
                name=f"{tool['name']} tool",
                description=f"Uses the {tool['name']} tool ({tool['permission']} permission).",
                tags=["tool", tool["permission"]],
            )
        )
    return skills


def _agent_card(agent_id: str, base_url: str) -> AgentCard:
    meta = catalog.AGENT_METADATA[agent_id]
    return AgentCard(
        name=agent_id,
        description=_describe(agent_id, meta),
        url=f"{base_url}/a2a/agents/{agent_id}" if base_url else f"/a2a/agents/{agent_id}",
        provider=_PROVIDER,
        default_input_modes=list(_TEXT_MODES),
        default_output_modes=list(_TEXT_MODES),
        skills=_skills_for(agent_id, meta),
    )


def build_agent_cards(base_url: str = "") -> list[AgentCard]:
    """Return one AgentCard per specialized agent (orchestrator and custom excluded)."""
    return [_agent_card(agent_id, base_url) for agent_id in SPECIALIZED_AGENT_IDS]


def get_agent_card(agent_id: str, base_url: str = "") -> AgentCard | None:
    """Return the AgentCard for one specialized agent id, or None if unknown."""
    if agent_id not in SPECIALIZED_AGENT_IDS:
        return None
    return _agent_card(agent_id, base_url)


def build_service_card(base_url: str = "") -> AgentCard:
    """Return the aggregate AgentCard for the Agentra service, enumerating every specialized agent."""
    skills = [
        AgentSkill(
            id=agent_id,
            name=_title(agent_id),
            description=_describe(agent_id, catalog.AGENT_METADATA[agent_id]),
            tags=["agent", catalog.AGENT_METADATA[agent_id]["capability"]],
            input_modes=list(_TEXT_MODES),
            output_modes=list(_TEXT_MODES),
        )
        for agent_id in SPECIALIZED_AGENT_IDS
    ]
    return AgentCard(
        name="Agentra",
        description=(
            "Agentra autonomous product-engineering service. Exposes "
            f"{len(SPECIALIZED_AGENT_IDS)} specialized agents for discovery via A2A."
        ),
        url=f"{base_url}/a2a/agents" if base_url else "/a2a/agents",
        provider=_PROVIDER,
        default_input_modes=list(_TEXT_MODES),
        default_output_modes=list(_TEXT_MODES),
        skills=skills,
    )

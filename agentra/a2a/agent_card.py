"""A2A AgentCard and its nested capability, skill, and provider models."""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from agentra.a2a._base import A2ABaseModel

A2A_PROTOCOL_VERSION = "0.2.5"


class AgentProvider(A2ABaseModel):
    """The organization publishing an agent."""

    organization: str
    url: str


class AgentCapabilities(A2ABaseModel):
    """Optional protocol features an agent supports."""

    streaming: bool = False
    push_notifications: bool = False
    state_transition_history: bool = False


class AgentSkill(A2ABaseModel):
    """A discrete capability an agent can perform, as advertised on its AgentCard."""

    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: Optional[list[str]] = None
    input_modes: Optional[list[str]] = None
    output_modes: Optional[list[str]] = None


class AgentCard(A2ABaseModel):
    """The discovery document describing an agent's identity, capabilities, and skills."""

    name: str
    description: str
    url: str = ""
    version: str = "0.1.0"
    protocol_version: str = A2A_PROTOCOL_VERSION
    provider: Optional[AgentProvider] = None
    documentation_url: Optional[str] = None
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    default_input_modes: list[str] = Field(default_factory=lambda: ["text/plain"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text/plain"])
    skills: list[AgentSkill]

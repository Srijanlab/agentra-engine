"""Additive read-only A2A agent-discovery endpoints for the existing FastAPI app."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from agentra.a2a import cards
from agentra.a2a.agent_card import AgentCard

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get("/a2a/agents", response_model=list[AgentCard])
async def list_a2a_agent_cards(request: Request) -> list[AgentCard]:
    return cards.build_agent_cards(_base_url(request))


@router.get("/a2a/agents/{agent_id}", response_model=AgentCard)
async def get_a2a_agent_card(agent_id: str, request: Request) -> AgentCard:
    card = cards.get_agent_card(agent_id, _base_url(request))
    if card is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent_id!r}")
    return card


@router.get("/.well-known/agent-card.json", response_model=AgentCard)
async def get_service_agent_card(request: Request) -> AgentCard:
    return cards.build_service_card(_base_url(request))

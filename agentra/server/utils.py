"""server/utils.py — utility functions and shared prompts for FastAPI routes."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from agentra import registry
from agentra.server.state import _active_runs, _app_locks

logger = logging.getLogger("agentra.server")

AGENT_CHAT_SYSTEM_PROMPTS = {
    "orchestrator": "You are the Orchestrator Agent. You decide which agent to call next and sequence tasks. You are now communicating directly with a human user. Respond to their queries in a helpful and concise manner.",
    "codebase": "You are the Codebase Agent. Your role is read-only scanning of the repository: mapping framework, architecture, and existing patterns. Respond to user queries about the code.",
    "discovery": "You are the Discovery Agent. Your role is deciding what to build next based on codebase, analytics, and backlog signals.",
    "implementation": "You are the Implementation Agent. Your role is implementing features and fixing bugs on dedicated branches.",
    "testing": "You are the Testing Agent. Your role is verifying the code locally and independently verifying live deployments.",
    "deployment": "You are the Deployment Agent. Your role is deploying verified feature branches to pre-prod or promoting to production.",
    "feedback": "You are the Analytics Feedback Agent. Your role is checking if a shipped feature is measurable and naming success metrics.",
    "prod_debug": "You are the Production Debugging Agent. Your role is diagnosing production alarms and auto-remediating issues.",
    "custom": "You are a Custom Agent. Your role is executing one-off sub-tasks that do not fit specialized agents.",
}


def _lock_for(app_name: str) -> asyncio.Lock:
    return _app_locks.setdefault(app_name, asyncio.Lock())


def _set_run(run_key: str, **fields: Any) -> None:
    _active_runs.setdefault(run_key, {}).update(fields)
    registry.record_run(run_key, **fields)


def _server_log(channel: str, message: str) -> None:
    """Structured server-side event log -- stdout / CloudWatch only."""
    logger.info("[server:%s] %s", channel, message)


def _paused_response(source: str) -> dict:
    _server_log(source, "system is paused -- trigger skipped")
    return {"triggered": False, "reason": "system is paused"}


def _app_branch(app_name: str) -> str:
    coord = registry.get_coordination_repo(app_name)
    return coord.branch if coord else "main"


def _chat_agent_label(agent_id: str) -> str:
    if agent_id == "orchestrator":
        return "Orchestrator"
    if agent_id == "prod_debug":
        return "Production Debugging Agent"
    if agent_id == "feedback":
        return "Analytics Feedback Agent"
    if agent_id == "custom":
        return "Custom Agent"
    return agent_id.capitalize() + " Agent"


def _strip_log_timestamp(raw_line: str) -> str:
    if not raw_line.startswith("["):
        return raw_line
    ts_str, sep, rest = raw_line[1:].partition("] ")
    if not sep:
        return raw_line
    try:
        dt.datetime.fromisoformat(ts_str)
    except ValueError:
        return raw_line
    return rest

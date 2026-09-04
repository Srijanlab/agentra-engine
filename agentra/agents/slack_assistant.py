"""Conversational agentra assistant: answers questions about the running system
and takes actions against its own HTTP API, driven from a Slack DM or @mention
(distinct from the #68 human-input escalation threads)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from agentra import chat_store, registry
from agentra.agents.base import run_agent
from agentra.server.utils import _server_log

logger = logging.getLogger(__name__)

_AGENTRA_REPO_URL = "https://github.com/Srijanlab/srijanlab-agentra.git"
_MAX_TURNS = 24


def _api_base() -> str:
    return os.environ.get("AGENTRA_SELF_API_BASE") or f"http://localhost:{os.environ.get('PORT', '8080')}"


def _agentra_repo() -> Path | None:
    for name, entry in registry.list_apps().items():
        if entry.get("repo_url") == _AGENTRA_REPO_URL:
            repo = registry.get_app_repo(name)
            if repo is not None:
                return repo
    return None


def _system_prompt(slack_user_id: str | None = None) -> str:
    base = _api_base()
    who = (
        f"You are talking to Slack user {slack_user_id}."
        if slack_user_id
        else "You are talking to a human operator."
    )
    return f"""You are the agentra assistant, reachable in Slack by DM or @mention. \
agentra is a self-hosted autonomous SDLC system: specialized agents (Orchestrator, \
Codebase, Discovery, Implementation, Testing, Deployment, ...) run cycles against \
registered apps, tracked as GitHub issues and Firestore runs.

{who} Two things you can do:

1. Answer questions about the system. Its own HTTP API is at {base} (no auth needed \
   from this host). Use `curl -s` for reads. Useful endpoints (read the route files \
   under agentra/server/routes/ for the full list and request bodies):
     GET  {base}/apps                         registered apps
     GET  {base}/runs?limit=20                recent runs
     GET  {base}/runs/<run_key>/logs          one run's log
     GET  {base}/loops                        loops (one per tracked issue)
     GET  {base}/needs-human                  runs blocked on a human
     GET  {base}/system/llm-backend           current model backend
     GET  {base}/apps/<app>/backlog           an app's backlog board
   You can also Read/Grep/Glob this repo to explain how something works.

2. Take actions the operator asks for, by calling the matching endpoint (POST with \
   `curl -s -X POST -H 'content-type: application/json' -d '{{...}}'`): trigger a \
   cycle, pause/resume the system, submit a backlog item, answer a needs-human \
   issue, switch the LLM backend, etc.

Rules:
- Never promote anything to production, and never run a destructive shell command \
  (rm -rf, dropping data, force-pushing) -- if the operator asks, explain that has \
  to be done deliberately elsewhere.
- Before any state-changing action, do it only if the operator's request is \
  unambiguous; otherwise ask a short clarifying question first.
- Reply in plain Slack text, concise, no markdown headers. Report what you actually \
  did (which endpoint, the response) rather than narrating plans."""


_FRIENDLY_ERROR = "Sorry — I couldn't finish that turn. Try rephrasing, or check the dashboard."


async def answer(user_text: str, *, thread_key: str, slack_user_id: str | None = None) -> str:
    """Run one assistant turn for a Slack conversation, resuming the thread's prior
    session when there is one. Returns the reply text to post back."""
    agent_id = f"slack-assistant:{thread_key}"
    repo = _agentra_repo()
    if repo is None:
        return (
            "I can't reach the agentra repo checkout on this host, so I can't act right now. "
            "Is the `agentra` app registered?"
        )

    chat_store.record_agent_chat_message("agentra", agent_id, "human", user_text)
    resume_session_id = chat_store.get_agent_session_id("agentra", agent_id)

    result = await run_agent(
        prompt=user_text,
        system_prompt=_system_prompt(slack_user_id),
        cwd=repo,
        allowed_tools=["Bash", "Read", "Grep", "Glob"],
        max_turns=_MAX_TURNS,
        agent_label="Slack Assistant",
        resume=resume_session_id,
    )

    if not result.ok:
        _server_log("slack", f"assistant turn not ok user={slack_user_id}: {(result.text or '')!r}")
        return _FRIENDLY_ERROR

    reply = (result.text or "").strip() or "(no response)"
    if result.session_id:
        chat_store.set_agent_session_id("agentra", agent_id, result.session_id)
    chat_store.record_agent_chat_message("agentra", agent_id, "agent", reply)
    return reply

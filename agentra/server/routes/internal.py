"""server/routes/internal.py — the loop's only door to engine-held state.

Token-gated RPC (`AGENTRA_INTERNAL_TOKEN`), separate from the Firebase user gate.
The loop calls `registry.*` / `Memory.*` methods here instead of touching Firestore
or GitHub itself.
"""

from __future__ import annotations

import dataclasses
import hmac
import logging
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from agentra import registry
from agentra.connectors import github_app
from agentra.memory import Memory

logger = logging.getLogger("agentra.server.internal")

router = APIRouter(prefix="/internal")


def _client_ips(request: Request) -> set[str]:
    """Trusted source addresses for the request. On Vercel `x-real-ip` and
    `x-vercel-forwarded-for` are platform-set and overwrite any inbound value,
    so they can't be spoofed. `x-forwarded-for` is deliberately NOT consulted --
    its leftmost entry is client-controllable."""
    ips: set[str] = set()
    for header in ("x-real-ip", "x-vercel-forwarded-for"):
        for part in request.headers.get(header, "").split(","):
            if part.strip():
                ips.add(part.strip())
    if not ips and request.client:  # local/dev: no proxy in front
        ips.add(request.client.host)
    return ips


def _require_token(request: Request, authorization: str | None = Header(default=None)) -> None:
    allowed = {ip.strip() for ip in os.environ.get("AGENTRA_INTERNAL_ALLOWED_IPS", "").split(",") if ip.strip()}
    if allowed and not (_client_ips(request) & allowed):
        raise HTTPException(status_code=403, detail="not allowed from this address")

    expected = os.environ.get("AGENTRA_INTERNAL_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="internal API not configured")
    prefix = "bearer "
    got = authorization[len(prefix):] if (authorization or "").lower().startswith(prefix) else ""
    if not hmac.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="bad internal token")


# --- exposed method whitelists -------------------------------------------------

_REGISTRY_METHODS = frozenset({
    "list_apps", "register_app", "remove_app",
    "get_slack_channel", "set_slack_channel",
    "is_paused", "pause", "resume",
    "get_llm_backend", "set_llm_backend",
    "record_slack_thread", "resolve_slack_thread", "slack_thread_for",
    "get_run", "list_runs", "record_run", "last_run_at",
    "list_loops", "loop_id_for", "loop_id_for_issue",
    "list_agent_steps", "record_agent_step",
    "list_waiting_for_human", "reconcile_stale_runs", "reconcile_waiting_for_human",
    "submit_request", "dispatch_once",
})

_MEMORY_METHODS = frozenset({
    "known_bugs", "in_progress_items", "closed_bugs", "blocking_bugs",
    "record_known_bug", "clear_known_bug",
    "clear_resolved_transient_bugs", "clear_resolved_auth_bugs",
    "record_failure", "record_failure_on_issue",
    "code_complete_items", "shipped_pending_test_items", "tested_items",
    "feature_queue", "in_progress_features", "shipped_features",
    "record_code_complete", "record_shipped_to_preprod", "record_tested",
    "released_features", "pending_promotion_features", "record_released",
    "record_feature_request", "clear_feature_request",
    "get_objective", "set_objective", "append_documentation",
    "set_codebase_spec_commit", "codebase_spec_commit",
    "record_human_input_context", "get_human_input_context",
    "record_human_answer", "human_input_pending",
    "escalate_existing_issue", "issue_html_url",
    "find_unanswered_human_input_comment",
    "record_in_progress_branch", "mark_status_done", "record_commit",
    "resume_branch_for", "resume_run_id_for", "resume_session_id_for",
    "run_ids_for", "record_spec", "get_spec",
})


def _json_safe(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _json_safe(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return value


class _UrlMemory(Memory):
    """Memory whose GitHub-backed methods work off a repo_url, with no local
    checkout (the engine has none). Local-file methods write to a throwaway dir
    and are not exposed over RPC."""

    def __init__(self, repo_url: str) -> None:
        self._forced_url = repo_url
        super().__init__(Path(tempfile.mkdtemp(prefix="agentra-mem-")))

    def _repo_url(self) -> str:
        return self._forced_url


def _memory_for(repo_url: str) -> Memory:
    for app in registry.list_apps().values():
        if app.get("repo_url") == repo_url and Path(app.get("repo_path", "")).is_dir():
            return Memory(Path(app["repo_path"]))
    return _UrlMemory(repo_url)


class RpcRequest(BaseModel):
    target: str
    method: str
    args: list = []
    kwargs: dict = {}
    repo_url: str | None = None


@router.post("/rpc", dependencies=[Depends(_require_token)])
async def rpc(req: RpcRequest) -> dict:
    if req.target == "registry":
        if req.method not in _REGISTRY_METHODS:
            raise HTTPException(status_code=403, detail=f"registry.{req.method} is not exposed")
        fn = getattr(registry, req.method)
    elif req.target == "memory":
        if req.method not in _MEMORY_METHODS:
            raise HTTPException(status_code=403, detail=f"memory.{req.method} is not exposed")
        if not req.repo_url:
            raise HTTPException(status_code=400, detail="repo_url is required for memory calls")
        fn = getattr(_memory_for(req.repo_url), req.method)
    else:
        raise HTTPException(status_code=400, detail=f"unknown target {req.target!r}")

    try:
        result = fn(*req.args, **req.kwargs)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("rpc %s.%s failed: %s", req.target, req.method, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
    return {"result": _json_safe(result)}


class RunLogRequest(BaseModel):
    lines: list[str]


@router.post("/runs/{run_id}/log", dependencies=[Depends(_require_token)])
async def run_log(run_id: str, req: RunLogRequest) -> dict:
    """Durable copy of a run's log tail (the loop's disk is ephemeral); the
    dashboard streams it back from here via /runs/{key}/logs."""
    db = registry.firestore_client()
    if db is None:
        raise HTTPException(status_code=503, detail="Firestore unavailable")
    tail = req.lines[-500:]
    db.collection("run_logs").document(run_id).set({"lines": tail})
    return {"ok": True, "lines": len(tail)}


class SlackMessageRequest(BaseModel):
    text: str
    channel: str | None = None
    thread_ts: str | None = None


@router.post("/slack/message", dependencies=[Depends(_require_token)])
async def slack_message(req: SlackMessageRequest) -> dict:
    """The loop sends Slack via the engine so it never holds SLACK_BOT_TOKEN."""
    from agentra.connectors import slack

    data = slack._post_message(req.text, channel=req.channel, thread_ts=req.thread_ts)
    return {"ok": data is not None, "data": data}


class GitTokenRequest(BaseModel):
    repo_url: str


@router.post("/git-token", dependencies=[Depends(_require_token)])
async def git_token(req: GitTokenRequest) -> dict:
    """A short-lived installation token for git clone/push -- so the loop never
    holds the GitHub App private key."""
    try:
        token = github_app.get_installation_token(req.repo_url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")
    return {"token": token}

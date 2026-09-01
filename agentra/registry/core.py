"""registry/core.py — shared variables and core app registration for agentra."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("agentra.registry")

_env_value = os.environ.get("AGENTRA_HOME")
AGENTRA_HOME = Path(_env_value) if _env_value else Path.home() / ".agentra"
APPS_PATH = AGENTRA_HOME / "apps.json"
INBOX_ROOT = AGENTRA_HOME / "inbox"
PAUSE_PATH = AGENTRA_HOME / "paused.json"
_LLM_BACKEND_PATH = AGENTRA_HOME / "llm_backend.json"
_RUNS_PATH = AGENTRA_HOME / "runs.json"
_AGENT_STEPS_PATH = AGENTRA_HOME / "agent_steps.jsonl"

_repos_env_value = os.environ.get("AGENTRA_REPOS_ROOT")
REPOS_ROOT = Path(_repos_env_value) if _repos_env_value else AGENTRA_HOME / "repos"

STALE_PROCESSING_SECONDS = 10 * 60
REQUEST_TYPES = ("bug", "feature_request", "objective_change")

# Human-in-the-loop escalation (GitHub issue #34): how long a run may sit in
HUMAN_INPUT_MAX_WAIT_SECONDS = float(os.environ.get("AGENTRA_HUMAN_INPUT_MAX_WAIT_HOURS", "24")) * 3600


def _init_firestore():
    project = os.environ.get("AGENTRA_FIRESTORE_PROJECT")
    if not project:
        return None
    try:
        from google.cloud import firestore
    except ImportError:
        return None
    return firestore.Client(project=project)


_db = _init_firestore()


def firestore_client():
    return _db


def _local_apps() -> dict[str, dict]:
    if not APPS_PATH.exists():
        return {}
    return json.loads(APPS_PATH.read_text())


def _local_save_apps(apps: dict[str, dict]) -> None:
    APPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    APPS_PATH.write_text(json.dumps(apps, indent=2))


def list_apps() -> dict[str, dict]:
    if _db is not None:
        return {doc.id: doc.to_dict() for doc in _db.collection("apps").stream()}
    return _local_apps()


def register_app(name: str, repo_path: str, repo_url: str | None = None, branch: str | None = None) -> None:
    entry: dict = {"repo_path": str(Path(repo_path).resolve())}
    if repo_url:
        entry["repo_url"] = repo_url
    if branch:
        entry["branch"] = branch

    if _db is not None:
        _db.collection("apps").document(name).set(entry)
        return

    apps = _local_apps()
    apps[name] = entry
    _local_save_apps(apps)
    for sub in ("pending", "processing", "done"):
        (INBOX_ROOT / name / sub).mkdir(parents=True, exist_ok=True)


def remove_app(name: str) -> bool:
    if _db is not None:
        doc_ref = _db.collection("apps").document(name)
        if not doc_ref.get().exists:
            return False
        doc_ref.delete()
        return True

    apps = _local_apps()
    if name not in apps:
        return False
    del apps[name]
    _local_save_apps(apps)
    return True


def set_slack_channel(name: str, channel_id: str | None) -> None:
    """Per-app Slack channel override for notify_shipped/notify_human_input_required, stored
    directly on the app's registry entry (Firestore apps/{name}, or the local apps.json
    fallback) -- distinct from EnvironmentConfig (GitHub Variables) and Memory (git-committed
    notes), since this is registry-level routing config, not deploy or app-content config."""
    if _db is not None:
        _db.collection("apps").document(name).set({"slack_channel_id": channel_id}, merge=True)
        return
    apps = _local_apps()
    if name in apps:
        apps[name]["slack_channel_id"] = channel_id
        _local_save_apps(apps)


def get_slack_channel(name: str) -> str | None:
    """The per-app Slack channel set via set_slack_channel, or None if unset -- callers fall
    back to the global SLACK_HUMAN_INPUT_CHANNEL env var (connectors/slack.py)."""
    return (list_apps().get(name) or {}).get("slack_channel_id")


def _remote_head_sha(repo_url: str, branch: str) -> str | None:
    # git_ops.remote_head_sha, not a bare `git ls-remote` -- this used to run
    from agentra.agents.git_ops import remote_head_sha

    sha = remote_head_sha(repo_url, branch)
    if sha is None:
        logger.warning("get_app_repo: ls-remote %s %s failed or returned nothing", repo_url, branch)
    return sha


def _local_head_sha(repo: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("get_app_repo: rev-parse HEAD in %s failed: %s", repo, exc)
        return None
    return result.stdout.strip() if result.returncode == 0 else None


_SYNC_CHECK_INTERVAL_SECONDS = 30
_last_sync_check: dict[str, float] = {}


def _sync_if_stale(repo: Path, repo_url: str, branch: str) -> None:
    if not (repo / ".git").exists():
        logger.info("get_app_repo: %s is not a git checkout, skipping remote resync", repo)
        return
    # get_app_repo runs on nearly every dashboard/API request -- without this,
    key = str(repo)
    now = time.monotonic()
    last_checked = _last_sync_check.get(key)
    if last_checked is not None and (now - last_checked) < _SYNC_CHECK_INTERVAL_SECONDS:
        return
    _last_sync_check[key] = now
    try:
        remote_sha = _remote_head_sha(repo_url, branch)
        if remote_sha is None:
            return
        if _local_head_sha(repo) == remote_sha:
            return
        logger.info("get_app_repo: %s is stale vs origin/%s, resyncing", repo, branch)
        from agentra.agents.git_ops import pull_latest

        pull_latest(repo, branch)
    except Exception:
        logger.exception(
            "get_app_repo: failed to resync %s to %s@%s; falling back to existing local checkout",
            repo, repo_url, branch,
        )


def get_app_repo(name: str) -> Path | None:
    app = list_apps().get(name)
    if app is None:
        return None
    repo = Path(app["repo_path"])
    if not repo.exists() and app.get("repo_url"):
        from agentra.agents.git_ops import GitOpError, clone_repo

        try:
            clone_repo(app["repo_url"], repo, branch=app.get("branch", "main"))
        except GitOpError:
            return None
    elif repo.exists() and app.get("repo_url"):
        _sync_if_stale(repo, app["repo_url"], app.get("branch", "main"))
    return repo if repo.exists() else None


def is_paused() -> dict | None:
    if _db is not None:
        doc = _db.collection("system").document("pause").get()
        return doc.to_dict() if doc.exists else None
    if not PAUSE_PATH.exists():
        return None
    return json.loads(PAUSE_PATH.read_text())


def pause(reason: str | None = None) -> None:
    record = {"paused_at": time.time(), "reason": reason}
    if _db is not None:
        _db.collection("system").document("pause").set(record)
        return
    PAUSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_PATH.write_text(json.dumps(record, indent=2))


def resume() -> None:
    if _db is not None:
        _db.collection("system").document("pause").delete()
        return
    PAUSE_PATH.unlink(missing_ok=True)


VALID_LLM_BACKENDS = ("claude", "nim")
_DEFAULT_LLM_BACKEND = "claude"
_LLM_BACKEND_CACHE_TTL_SECONDS = 30
_llm_backend_cache: tuple[float, str] | None = None


def get_llm_backend() -> str:
    """Which LLM the agent SDK talks to: "claude" (api.anthropic.com, default) or "nim"
    (the self-hosted NVIDIA NIM proxy). Global toggle, set from the dashboard, read on
    every agent turn -- cached briefly to spare Firestore."""
    global _llm_backend_cache
    now = time.monotonic()
    if _llm_backend_cache is not None and (now - _llm_backend_cache[0]) < _LLM_BACKEND_CACHE_TTL_SECONDS:
        return _llm_backend_cache[1]

    if _db is not None:
        doc = _db.collection("system").document("llm_backend").get()
        backend = (doc.to_dict() or {}).get("backend") if doc.exists else None
    elif _LLM_BACKEND_PATH.exists():
        backend = json.loads(_LLM_BACKEND_PATH.read_text()).get("backend")
    else:
        backend = None

    backend = backend if backend in VALID_LLM_BACKENDS else _DEFAULT_LLM_BACKEND
    _llm_backend_cache = (now, backend)
    return backend


def set_llm_backend(backend: str) -> None:
    if backend not in VALID_LLM_BACKENDS:
        raise ValueError(f"unknown llm backend {backend!r} -- expected one of {VALID_LLM_BACKENDS}")
    global _llm_backend_cache
    _llm_backend_cache = None
    if _db is not None:
        _db.collection("system").document("llm_backend").set({"backend": backend})
        return
    _LLM_BACKEND_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LLM_BACKEND_PATH.write_text(json.dumps({"backend": backend}, indent=2))


def persist_agentra_dir(repo: Path, branch: str, message: str) -> str | None:
    from agentra.agents.git_ops import GitOpError, commit_and_push

    try:
        commit_and_push(repo, branch, message, [".agentra/"])
        return None
    except GitOpError as exc:
        return str(exc)

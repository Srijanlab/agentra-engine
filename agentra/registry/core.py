"""registry/core.py — shared variables and core app registration for agentra."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from agentra.registry import _cache

logger = logging.getLogger("agentra.registry")

_env_value = os.environ.get("AGENTRA_HOME")
AGENTRA_HOME = Path(_env_value) if _env_value else Path.home() / ".agentra"
APPS_PATH = AGENTRA_HOME / "apps.json"
INBOX_ROOT = AGENTRA_HOME / "inbox"
PAUSE_PATH = AGENTRA_HOME / "paused.json"
_SLACK_THREADS_PATH = AGENTRA_HOME / "slack_threads.json"
_LLM_BACKEND_PATH = AGENTRA_HOME / "llm_backend.json"
_RUNS_PATH = AGENTRA_HOME / "runs.json"
_LOOPS_PATH = AGENTRA_HOME / "loops.json"
_AGENT_STEPS_PATH = AGENTRA_HOME / "agent_steps.jsonl"

_repos_env_value = os.environ.get("AGENTRA_REPOS_ROOT")
REPOS_ROOT = Path(_repos_env_value) if _repos_env_value else AGENTRA_HOME / "repos"

# A cycle is one long await (a sub-agent dispatch can itself run 10-20 min with
# no orchestrator-level checkpoint), so the reaper only fires on a genuine hang.
STALE_PROCESSING_SECONDS = 60 * 60
REQUEST_TYPES = ("bug", "feature_request", "objective_change")

# Human-in-the-loop escalation (GitHub issue #34): how long a run may sit in
HUMAN_INPUT_MAX_WAIT_SECONDS = float(os.environ.get("AGENTRA_HUMAN_INPUT_MAX_WAIT_HOURS", "24")) * 3600


# Vercel injects a fresh OIDC JWT per invocation; identity_pool reads it from a
# file. sync_oidc_token_file() copies the env var onto disk -- call it before any
# Firestore use (see server middleware).
_OIDC_TOKEN_FILE = "/tmp/agentra_vercel_oidc_token"


def sync_oidc_token_file(token: str | None = None) -> None:
    # Fluid Compute delivers the OIDC JWT as the x-vercel-oidc-token request
    # header (not an env var), so the caller passes it in from the request.
    token = token or os.environ.get("VERCEL_OIDC_TOKEN")
    if not token:
        return
    os.environ["VERCEL_OIDC_TOKEN"] = token  # so downstream env checks pass
    try:
        with open(_OIDC_TOKEN_FILE, "w") as fh:
            fh.write(token)
    except OSError:
        pass


def _gcp_credentials():
    """Credentials for Firestore, in priority order. Returns None to fall back to
    ADC / the metadata server (i.e. running on GCP)."""
    # 1. Keyless: Vercel OIDC -> Workload Identity Federation (survives the
    #    iam.disableServiceAccountKeyCreation org policy -- no key anywhere).
    wif_config = os.environ.get("GCP_WORKLOAD_IDENTITY_CONFIG")
    if wif_config and os.environ.get("VERCEL_OIDC_TOKEN"):
        sync_oidc_token_file()
        from google.auth import identity_pool

        cfg = json.loads(wif_config)
        cfg.setdefault("credential_source", {})["file"] = _OIDC_TOKEN_FILE
        return identity_pool.Credentials.from_info(cfg)
    # 2. A service-account key from an env var, if one is ever available.
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_info(json.loads(sa_json))
    return None


def _init_firestore():
    project = os.environ.get("AGENTRA_FIRESTORE_PROJECT")
    if not project:
        return None
    try:
        from google.cloud import firestore

        creds = _gcp_credentials()
        if creds is not None:
            return firestore.Client(project=project, credentials=creds)
        return firestore.Client(project=project)
    except Exception:
        # Never crash the whole app on a credential/import problem -- endpoints
        # that need Firestore will 503, the rest (and /health) still work.
        logger.error("Firestore init failed -- running without it", exc_info=True)
        return None


_db = _init_firestore()


def ensure_firestore():
    """Lazy init for the Vercel path: the OIDC token only exists per-request, so
    _db can't be built at import. Call once the token file is in place."""
    global _db
    if _db is None:
        _db = _init_firestore()
    return _db


def firestore_client():
    return _db


def _init_dynamodb():
    """Static IAM keys, not OIDC federation -- this AWS account has a diagnosed
    prior failure of AssumeRoleWithWebIdentity (see deploy/aws's CiUser comment
    in the loop repo), so no per-request token refresh is needed here at all,
    unlike Firestore's WIF dance above. AGENTRA_AWS_* is prefixed rather than
    bare AWS_* because Vercel's own Lambda-based runtime reserves those bare
    names for its own unrelated execution-role credentials."""
    prefix = os.environ.get("AGENTRA_DYNAMODB_TABLE_PREFIX")
    if not prefix:
        return None
    try:
        import boto3

        session = boto3.Session(
            aws_access_key_id=os.environ.get("AGENTRA_AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AGENTRA_AWS_SECRET_ACCESS_KEY"),
            region_name=os.environ.get("AGENTRA_AWS_REGION"),
        )
        return session.resource("dynamodb")
    except Exception:
        # Never crash the whole app on a credential/import problem -- endpoints
        # that need DynamoDB will 503, the rest (and /health) still work.
        logger.error("DynamoDB init failed -- running without it", exc_info=True)
        return None


# Migration in progress: apps/runs/loops/requests still read/write Firestore
# via `_db` above until their own PRs land; collections already ported (see
# is_paused/pause/resume/get_llm_backend/set_llm_backend below) use `_ddb`
# instead. Once every collection is ported and Firestore is decommissioned,
# `_ddb` absorbs `_db`'s name -- kept distinct for now so a not-yet-ported
# collection can't be handed a DynamoDB resource and call Firestore-shaped
# methods on it.
_ddb = _init_dynamodb()


def dynamodb_resource():
    return _ddb


def _local_apps() -> dict[str, dict]:
    if not APPS_PATH.exists():
        return {}
    return json.loads(APPS_PATH.read_text())


def _local_save_apps(apps: dict[str, dict]) -> None:
    APPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    APPS_PATH.write_text(json.dumps(apps, indent=2))


def list_apps() -> dict[str, dict]:
    if _db is not None:
        return _cache.get_or_set(
            "apps", lambda: {doc.id: doc.to_dict() for doc in _db.collection("apps").stream()}, ttl=20
        )
    return _local_apps()


def register_app(
    name: str,
    repo_path: str | None = None,
    repo_url: str | None = None,
    branch: str | None = None,
    repos: list[dict] | None = None,
) -> None:
    """`repos`, when given, registers a multi-repo app (each entry: name, repo_url,
    branch, role -- exactly one "coordination" -- and optional deploy_strategy);
    otherwise this is a legacy single-repo app, its one repo implicitly both
    coordination and code."""
    if repos:
        entry: dict = {"repos": repos}
    else:
        entry = {"repo_path": str(Path(repo_path).resolve())}
        if repo_url:
            entry["repo_url"] = repo_url
        if branch:
            entry["branch"] = branch

    _cache.drop("apps")
    if _db is not None:
        _db.collection("apps").document(name).set(entry)
        return

    apps = _local_apps()
    apps[name] = entry
    _local_save_apps(apps)
    for sub in ("pending", "processing", "done"):
        (INBOX_ROOT / name / sub).mkdir(parents=True, exist_ok=True)


def remove_app(name: str) -> bool:
    _cache.drop("apps")
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
    _cache.drop("apps")
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


@dataclass
class RepoSpec:
    """One repo of an app. `role` is "coordination" (issues, .agentra/memory, the
    app's objective -- exactly one per app) or "code" (deployable). A legacy
    single-repo app has exactly one RepoSpec that is both."""

    name: str
    path: Path | None
    repo_url: str | None
    branch: str
    role: str
    deploy_strategy: str | None = None


def _repo_specs(app_name: str, entry: dict) -> list[RepoSpec]:
    """Every repo in an app entry, as unresolved specs (path = the stored path, not
    necessarily a checkout that exists on this host) -- the legacy shape
    ({repo_path, repo_url, branch}, one repo that is both coordination and code) or
    the multi-repo shape ({"repos": [...]})."""
    raw = entry.get("repos")
    if not raw:
        return [
            RepoSpec(
                name=app_name,
                path=Path(entry["repo_path"]),
                repo_url=entry.get("repo_url"),
                branch=entry.get("branch", "main"),
                role="coordination",
                deploy_strategy=entry.get("deploy_strategy"),
            )
        ]
    return [
        RepoSpec(
            name=r["name"],
            path=Path(r["path"]) if r.get("path") else REPOS_ROOT / app_name / r["name"],
            repo_url=r.get("repo_url"),
            branch=r.get("branch", "main"),
            role=r.get("role", "code"),
            deploy_strategy=r.get("deploy_strategy"),
        )
        for r in raw
    ]


def _resolve_repo(repo_url: str | None, branch: str, stored_path: Path, clone_dest: Path) -> Path | None:
    """Ensure one repo's checkout exists on this host and return its path -- cloning
    to `clone_dest` if the stored path isn't here, resyncing if stale. Cloud mode
    (Firestore-backed, no persistent disk) just returns the stored path unresolved."""
    if _db is not None:
        return stored_path
    if stored_path.exists() and repo_url:
        _sync_if_stale(stored_path, repo_url, branch)
        return stored_path
    if not repo_url:
        return stored_path if stored_path.exists() else None
    # The stored path has no checkout on THIS host -- it's from wherever the app
    # last ran (GCP VM, Vercel sandbox, ...). A fresh clone goes to this runtime's
    # REPOS_ROOT, never back to that foreign (often unwritable) path, which used to
    # fail every cycle with a read-only-filesystem mkdir.
    if clone_dest.exists():
        _sync_if_stale(clone_dest, repo_url, branch)
        return clone_dest
    from agentra.agents.git_ops import GitOpError, clone_repo

    try:
        clone_repo(repo_url, clone_dest, branch=branch)
    except GitOpError:
        return None
    return clone_dest if clone_dest.exists() else None


def get_app_repos(name: str) -> dict[str, RepoSpec]:
    """Every repo of `name`, resolved to a checkout on this host. A legacy
    single-repo app resolves its one (coordination+code) repo to REPOS_ROOT/name,
    exactly as get_app_repo always has; a multi-repo app resolves each repo to
    REPOS_ROOT/name/<repo-name>."""
    # Go through the registry facade, not core.list_apps: on the loop the app
    # registry lives in the engine and is reachable only via the RPC proxy
    # (core.list_apps() there is an empty local store).
    from agentra import registry as _reg

    app = _reg.list_apps().get(name)
    if app is None:
        return {}
    legacy = "repos" not in app
    out: dict[str, RepoSpec] = {}
    for spec in _repo_specs(name, app):
        clone_dest = REPOS_ROOT / name if legacy else REPOS_ROOT / name / spec.name
        resolved = _resolve_repo(spec.repo_url, spec.branch, spec.path, clone_dest)
        out[spec.name] = RepoSpec(
            name=spec.name, path=resolved, repo_url=spec.repo_url,
            branch=spec.branch, role=spec.role, deploy_strategy=spec.deploy_strategy,
        )
    return out


def get_coordination_repo(name: str) -> RepoSpec | None:
    for spec in get_app_repos(name).values():
        if spec.role == "coordination":
            return spec
    return None


def get_app_repo(name: str) -> Path | None:
    spec = get_coordination_repo(name)
    return spec.path if spec else None


def repo_url_for_path(repo: Path) -> str | None:
    """The GitHub URL for a checkout path -- the registry's stored repo_url (works
    with no local git), falling back to `git remote get-url origin` for local use."""
    try:
        from agentra import registry as _reg

        for app_name, app in _reg.list_apps().items():
            for spec in _repo_specs(app_name, app):
                if not spec.repo_url:
                    continue
                if spec.path == repo or (spec.path is not None and spec.path.name == repo.name):
                    return spec.repo_url
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _read_pause_item() -> dict | None:
    from agentra.registry import _dynamo

    item = _dynamo.get_item(_dynamo.table("system"), {"key": "pause"})
    return {"paused_at": item["paused_at"], "reason": item.get("reason")} if item else None


def is_paused() -> dict | None:
    if _ddb is not None:
        return _cache.get_or_set("pause", _read_pause_item, ttl=10)
    if not PAUSE_PATH.exists():
        return None
    return json.loads(PAUSE_PATH.read_text())


def pause(reason: str | None = None) -> None:
    record = {"paused_at": time.time(), "reason": reason}
    _cache.drop("pause")
    if _ddb is not None:
        from agentra.registry import _dynamo

        _dynamo.put_item(_dynamo.table("system"), {"key": "pause", **record})
        return
    PAUSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_PATH.write_text(json.dumps(record, indent=2))


def resume() -> None:
    _cache.drop("pause")
    if _ddb is not None:
        from agentra.registry import _dynamo

        _dynamo.table("system").delete_item(Key={"key": "pause"})
        return
    PAUSE_PATH.unlink(missing_ok=True)


_SLACK_THREAD_CAP = 300


def _slack_threads() -> dict:
    if _db is not None:
        doc = _db.collection("system").document("slack_threads").get()
        return doc.to_dict() or {} if doc.exists else {}
    if not _SLACK_THREADS_PATH.exists():
        return {}
    return json.loads(_SLACK_THREADS_PATH.read_text())


def record_slack_thread(thread_ts: str, *, app: str, issue_number: int) -> None:
    """Remembers which needs_human issue a Slack HUMAN_INPUT_REQUIRED thread anchors, so a
    human's reply in that thread routes back to the right run and every follow-up question
    stays in the same thread (GitHub issue #68's two-way loop)."""
    entry = {"app": app, "issue_number": issue_number}
    if _db is not None:
        _db.collection("system").document("slack_threads").set({thread_ts: entry}, merge=True)
        return
    threads = _slack_threads()
    threads[thread_ts] = entry
    for stale in list(threads)[:-_SLACK_THREAD_CAP]:
        del threads[stale]
    _SLACK_THREADS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SLACK_THREADS_PATH.write_text(json.dumps(threads, indent=2))


def resolve_slack_thread(thread_ts: str) -> dict | None:
    """{"app", "issue_number"} for a Slack thread recorded by record_slack_thread, or None."""
    return _slack_threads().get(thread_ts)


def slack_thread_for(app: str, issue_number: int) -> str | None:
    """The thread_ts already anchoring this issue's conversation, or None -- so a follow-up
    question replies into the existing thread instead of starting a fresh top-level message."""
    for ts, entry in _slack_threads().items():
        if entry.get("app") == app and entry.get("issue_number") == issue_number:
            return ts
    return None


VALID_LLM_BACKENDS = ("claude", "claude_token", "nim")
_DEFAULT_LLM_BACKEND = "claude"
_LLM_BACKEND_CACHE_TTL_SECONDS = 30
_llm_backend_cache: tuple[float, str] | None = None


def get_llm_backend() -> str:
    """Which LLM the agent SDK talks to: "claude" (api.anthropic.com, default) or "nim"
    (the self-hosted NVIDIA NIM proxy). Global toggle, set from the dashboard, read on
    every agent turn -- cached briefly to spare the backing store."""
    global _llm_backend_cache
    now = time.monotonic()
    if _llm_backend_cache is not None and (now - _llm_backend_cache[0]) < _LLM_BACKEND_CACHE_TTL_SECONDS:
        return _llm_backend_cache[1]

    if _ddb is not None:
        from agentra.registry import _dynamo

        item = _dynamo.get_item(_dynamo.table("system"), {"key": "llm_backend"})
        backend = item.get("backend") if item else None
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
    if _ddb is not None:
        from agentra.registry import _dynamo

        _dynamo.put_item(_dynamo.table("system"), {"key": "llm_backend", "backend": backend})
        return
    _LLM_BACKEND_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LLM_BACKEND_PATH.write_text(json.dumps({"backend": backend}, indent=2))


def persist_agentra_dir(repo: Path, branch: str, message: str) -> str | None:
    # `.agentra/` is the local-JSON fallback store; with Firestore it holds
    # nothing worth committing, and the host has no git. No-op in cloud mode.
    if _db is not None:
        return None
    from agentra.agents.git_ops import GitOpError, commit_and_push

    try:
        commit_and_push(repo, branch, message, [".agentra/"])
        return None
    except GitOpError as exc:
        return str(exc)

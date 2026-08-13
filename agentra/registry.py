"""Multi-app registry and durable inbox — the "every app sends requests to
agentra" layer (vision.md's system operating across many apps, not tied to
one project).

This is agentra's own operational state about *which* apps it manages and
*what's waiting to be absorbed* into them -- distinct from any single app's
own audit trail, which lives inside that app's own repo (agentra/memory.py,
git-committed, travels with the project). That split is deliberate: project
knowledge belongs with the project (portable, human-readable, versioned);
agentra's own bookkeeping belongs in agentra's own durable storage.

Backed by Firestore when AGENTRA_FIRESTORE_PROJECT is set (the deployed
environment); falls back to local JSON files under AGENTRA_HOME otherwise,
so local dev needs no GCP credentials at all. Firestore replaced an earlier
GCS-FUSE-mounted-JSON-files design -- that worked for plain read/write but
gcsfuse's limited POSIX semantics kept surfacing real bugs elsewhere
(chmod failures cloning repos onto the same mount), and a real database
gives proper atomic claims for the inbox instead of relying on filesystem
rename semantics on a network filesystem.

Durability is the whole point of the inbox, not an afterthought: a
submitted request is durably stored (Firestore write, or the pending/
file-write-then-atomic-rename fallback) before this function ever returns
success to the caller. Processing claims a request atomically (a Firestore
transaction, or a filesystem rename in the fallback), and only marks it
done once it's been merged into the target app's own ledgers. If the
process crashes between those two steps, the request is still sitting in
"processing" state -- dispatch_once() checks for exactly that on every run
and resumes it, so nothing is ever silently lost to an in-memory queue a
crash would wipe out.
"""

import datetime as dt
import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentra.memory import Memory

logger = logging.getLogger("agentra.registry")

# NOTE: not `Path(os.environ.get(...)) or Path.home() / ".agentra"` -- Path objects
# have no __bool__/__len__, so Path("") is truthy and that pattern silently never
# falls through to the default (resolves to the cwd instead, wherever that happens
# to be for a given invocation). Caught this live: it made the registry appear to
# lose all state between container runs, since each run's cwd differed.
_env_value = os.environ.get("AGENTRA_HOME")
AGENTRA_HOME = Path(_env_value) if _env_value else Path.home() / ".agentra"
APPS_PATH = AGENTRA_HOME / "apps.json"
INBOX_ROOT = AGENTRA_HOME / "inbox"
PAUSE_PATH = AGENTRA_HOME / "paused.json"

# TASK-016/018: where server.py's clone-on-register path checks a newly
# registered GitHub repo out to. This is genuinely local-disk-only (not
# Firestore-backed, obviously -- a repo checkout isn't a document) and not
# expected to survive a restart; registry.get_app_repo() re-clones from
# repo_url on demand when it's missing, so the checkout is self-healing
# rather than durable -- the actual durable copy of a project's history is
# whatever it has pushed to its own git remote.
_repos_env_value = os.environ.get("AGENTRA_REPOS_ROOT")
REPOS_ROOT = Path(_repos_env_value) if _repos_env_value else AGENTRA_HOME / "repos"

# A request left in "processing" longer than this is assumed to be from a
# crashed prior dispatch run, not one that's genuinely still in flight
# (processing itself is a fast, in-process JSON merge -- see dispatch_once
# -- so a real in-progress case never gets anywhere near this long).
STALE_PROCESSING_SECONDS = 10 * 60

REQUEST_TYPES = ("bug", "feature_request", "objective_change")


def _init_firestore():
    """None if Firestore isn't configured/available -- explicit opt-in via
    AGENTRA_FIRESTORE_PROJECT (not just any ambient GOOGLE_CLOUD_PROJECT a
    shell happens to have set, which could otherwise surprise local dev
    into trying to reach a real project) or the SDK isn't installed."""
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
    """None if Firestore isn't configured -- server.py's signal log uses
    this rather than reaching into the module-private _db directly."""
    return _db


# ── Apps registry ───────────────────────────────────────────────────────────


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
    """repo_url/branch are optional (a local `agentra apps add` registration
    has neither) -- when present, get_app_repo() below uses them to
    re-clone automatically if repo_path ever goes missing (see REPOS_ROOT's
    comment on why the checkout itself isn't durable)."""
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


def _remote_head_sha(repo_url: str, branch: str) -> str | None:
    """`git ls-remote`'s HEAD sha for `branch` on `repo_url`, or None if the
    remote couldn't be reached (network blip, revoked credentials, branch
    renamed, etc.). None is a "can't tell" signal, not an error -- callers
    must treat it as "assume not stale" rather than let a trigger fail
    because the remote happened to be briefly unreachable."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", repo_url, f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("get_app_repo: ls-remote %s failed: %s", repo_url, exc)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        logger.warning(
            "get_app_repo: ls-remote %s %s returned nothing (rc=%s): %s",
            repo_url, branch, result.returncode, result.stderr.strip(),
        )
        return None
    return result.stdout.split()[0]


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


def _sync_if_stale(repo: Path, repo_url: str, branch: str) -> None:
    """Refresh an EXISTING checkout to match origin/`branch` when it's fallen
    behind -- e.g. an objective.yaml edit made via the dashboard, or a
    manual fix pushed straight to the branch, neither of which touch this
    on-disk checkout on their own. Compares local HEAD against the remote's
    HEAD for the tracked branch first so the common case (already current)
    costs one `ls-remote` and no fetch/reset.

    Best-effort by design: anything that goes wrong here (remote
    unreachable, auth failure, local checkout in a weird state) is logged
    and swallowed so a trigger always falls back to using whatever checkout
    is already on disk rather than crashing."""
    if not (repo / ".git").exists():
        logger.info("get_app_repo: %s is not a git checkout, skipping remote resync", repo)
        return
    try:
        remote_sha = _remote_head_sha(repo_url, branch)
        if remote_sha is None:
            return  # couldn't reach the remote -- use the existing checkout as-is
        if _local_head_sha(repo) == remote_sha:
            return  # already up to date

        logger.info("get_app_repo: %s is stale vs origin/%s, resyncing", repo, branch)
        from agentra.agents.git_ops import pull_latest

        pull_latest(repo, branch)
    except Exception:
        logger.exception(
            "get_app_repo: failed to resync %s to %s@%s; falling back to existing local checkout",
            repo, repo_url, branch,
        )


def get_app_repo(name: str) -> Path | None:
    """None if `name` isn't registered, or if its checkout is missing *and*
    can't be recovered. Every existing caller already goes through this one
    function, so the re-clone-if-missing recovery (TASK-018) needs no
    changes anywhere else: a restarted instance transparently re-clones a
    registered app's repo the next time anything asks for it.

    Also keeps an EXISTING checkout from silently going stale: an
    externally-made .agentra/ change (an objective.yaml edit via the
    dashboard, or a manual fix pushed to the branch) lands on the remote,
    not on this on-disk checkout, so every call here checks the local HEAD
    against the remote's before handing the path back and resyncs if
    they've diverged (see _sync_if_stale). That check is best-effort -- a
    remote that can't be reached just means we proceed with the existing
    checkout, same as before this existed."""
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


# ── Kill switch (TASK-017) ──────────────────────────────────────────────────
# A durable, global marker every trigger path in server.py checks before
# dispatching new agent work -- must survive a restart, and a human hitting
# "pause" needs that to actually stick even if the instance recycles a
# minute later.


def is_paused() -> dict | None:
    """None if not paused; otherwise the pause record (who/when/why, for
    display -- the dashboard shows this so "who paused it and why" isn't
    lost the moment the button is clicked)."""
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


# ── Inbox: durable requests submitted for an app, drained by dispatch_once ──


def submit_request(
    app: str,
    request_type: str,
    description: str,
    severity: str | None = None,
    screenshot_url: str | None = None,
) -> str:
    """Durably enqueue a request for `app`. Returns the request id.

    This is the one function anything outside agentra should call to get a
    signal/feature-request/objective-change into the system -- a filesystem-
    based adapter script, or an HTTP handler (server.py's /trigger/queue).
    Either way, by the time this returns, the request is durably stored,
    not held anywhere in memory only.
    """
    if request_type not in REQUEST_TYPES:
        raise ValueError(f"unknown request type: {request_type!r}, must be one of {REQUEST_TYPES}")
    if app not in list_apps():
        raise ValueError(f"unknown app {app!r} -- register it first with `agentra apps add`")

    request_id = uuid.uuid4().hex[:12]

    if _db is not None:
        _db.collection("apps").document(app).collection("requests").document(request_id).set(
            {
                "id": request_id,
                "app": app,
                "type": request_type,
                "description": description,
                "severity": severity,
                "screenshot_url": screenshot_url,
                "received_at": time.time(),
                "status": "pending",
            }
        )
        return request_id

    pending_dir = INBOX_ROOT / app / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": request_id,
        "app": app,
        "type": request_type,
        "description": description,
        "severity": severity,
        "screenshot_url": screenshot_url,
        "received_at": time.time(),
    }
    # Write to a temp file then atomic-rename into place, so a crash mid-write
    # never leaves a half-written, corrupt request file in pending/.
    tmp_path = pending_dir / f".{request_id}.tmp"
    final_path = pending_dir / f"{request_id}.json"
    tmp_path.write_text(json.dumps(payload, indent=2))
    os.rename(tmp_path, final_path)
    return request_id


@dataclass
class DispatchSummary:
    resumed_stale: int
    processed: int
    errors: list[str]


def _apply_request(repo: Path, request: dict) -> None:
    mem = Memory(repo)
    request_type = request["type"]
    if request_type == "bug":
        mem.record_known_bug(
            run_id=request["id"],
            severity=request.get("severity") or "medium",
            diagnosis=request["description"],
            proposed_fix="",
            source="customer",
            external_id=request["id"],
        )
    elif request_type == "feature_request":
        mem.record_feature_request(
            description=request["description"],
            source="customer",
            external_id=request["id"],
        )
    elif request_type == "objective_change":
        mem.set_objective(request["description"])
    else:
        raise ValueError(f"unknown request type: {request_type!r}")


def persist_agentra_dir(repo: Path, branch: str, message: str) -> str | None:
    """Commit and push .agentra/ -- returns an error string on failure
    (caller decides how to surface it), None on success. Any write that
    only touches the local checkout (Memory.record_*, mem.write, etc.) is
    NOT durable on its own -- confirmed live for the backlog-absorption
    path: an addition would vanish the next time a *different* Cloud Run
    instance (a fresh checkout, cloned straight from git) served a request
    for the same app, since nothing had ever pushed it anywhere. Shared by
    dispatch_once's backlog absorption and server.py's chat/work-update/
    standup endpoints -- every write path that touches .agentra/ needs
    this same "durable before the call returns success" bar register_app/
    update_app already hold themselves to."""
    from agentra.agents.git_ops import GitOpError, commit_and_push

    try:
        commit_and_push(repo, branch, message, [".agentra/"])
        return None
    except GitOpError as exc:
        return str(exc)


def _persist_backlog(app: str, repo: Path, branch: str, errors: list[str]) -> None:
    error = persist_agentra_dir(repo, branch, f"agentra: absorb inbox requests for {app!r}")
    if error:
        errors.append(f"{app}: applied locally but failed to push .agentra/: {error}")


# ── Local (file-based) fallback implementation ──────────────────────────────


def _local_resume_stale_processing(app: str) -> int:
    """Requests left in processing/ from a crashed prior dispatch run get moved
    back to pending/ so this run picks them up fresh. Safe to do unconditionally
    for anything old enough -- merges are idempotent (external_id dedup in
    memory.py), so reprocessing a request that actually did finish last time is
    a harmless no-op, not a duplicate."""
    resumed = 0
    processing_dir = INBOX_ROOT / app / "processing"
    if not processing_dir.is_dir():
        return 0
    now = time.time()
    for path in processing_dir.glob("*.json"):
        if now - path.stat().st_mtime < STALE_PROCESSING_SECONDS:
            continue  # plausibly still genuinely in flight; leave it alone
        target = INBOX_ROOT / app / "pending" / path.name
        os.rename(path, target)
        resumed += 1
    return resumed


def _local_dispatch_once() -> DispatchSummary:
    resumed_total = 0
    processed = 0
    errors: list[str] = []

    for app, info in _local_apps().items():
        repo = Path(info["repo_path"])
        resumed_total += _local_resume_stale_processing(app)

        pending_dir = INBOX_ROOT / app / "pending"
        processing_dir = INBOX_ROOT / app / "processing"
        done_dir = INBOX_ROOT / app / "done"
        processing_dir.mkdir(parents=True, exist_ok=True)
        done_dir.mkdir(parents=True, exist_ok=True)

        if not pending_dir.is_dir():
            continue

        applied_any = False
        for path in sorted(pending_dir.glob("*.json")):
            processing_path = processing_dir / path.name
            try:
                os.rename(path, processing_path)  # atomic claim
            except OSError as exc:
                errors.append(f"{app}/{path.name}: could not claim ({exc})")
                continue

            try:
                request = json.loads(processing_path.read_text())
                _apply_request(repo, request)
            except Exception as exc:
                errors.append(f"{app}/{path.name}: {exc}")
                continue  # leave it in processing/ -- resumed and retried next run

            os.rename(processing_path, done_dir / path.name)
            processed += 1
            applied_any = True

        if applied_any:
            _persist_backlog(app, repo, info.get("branch") or "main", errors)

    return DispatchSummary(resumed_stale=resumed_total, processed=processed, errors=errors)


# ── Firestore implementation ────────────────────────────────────────────────


def _firestore_try_claim(doc_ref) -> bool:
    """Atomically move one request from pending -> processing. False if it
    was already claimed (or gone) by the time this transaction runs --
    dispatch_once() just skips it, matching the local backend's
    os.rename-fails-if-already-moved behavior."""
    from google.cloud import firestore

    transaction = _db.transaction()

    @firestore.transactional
    def _claim(transaction):
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists or snapshot.get("status") != "pending":
            return False
        transaction.update(doc_ref, {"status": "processing", "claimed_at": time.time()})
        return True

    return _claim(transaction)


def _firestore_dispatch_once() -> DispatchSummary:
    from google.cloud.firestore_v1.base_query import FieldFilter

    resumed_total = 0
    processed = 0
    errors: list[str] = []
    now = time.time()

    for app, info in list_apps().items():
        repo = Path(info["repo_path"])
        requests_ref = _db.collection("apps").document(app).collection("requests")

        for doc in requests_ref.where(filter=FieldFilter("status", "==", "processing")).stream():
            claimed_at = doc.get("claimed_at") or 0
            if now - claimed_at < STALE_PROCESSING_SECONDS:
                continue  # plausibly still genuinely in flight
            doc.reference.update({"status": "pending"})
            resumed_total += 1

        applied_any = False
        for doc in requests_ref.where(filter=FieldFilter("status", "==", "pending")).stream():
            if not _firestore_try_claim(doc.reference):
                continue
            try:
                _apply_request(repo, doc.to_dict())
            except Exception as exc:
                errors.append(f"{app}/{doc.id}: {exc}")
                continue  # left in "processing" -- resumed and retried next run
            doc.reference.update({"status": "done"})
            processed += 1
            applied_any = True

        if applied_any:
            _persist_backlog(app, repo, info.get("branch") or "main", errors)

    return DispatchSummary(resumed_stale=resumed_total, processed=processed, errors=errors)


def dispatch_once() -> DispatchSummary:
    """Absorb everything currently sitting in the inbox into each app's own
    ledgers. Cheap and fast (no LLM calls) -- meant to run frequently (e.g.
    every few minutes via cron), separate from the actual (expensive,
    slower) `agentra run` cycles that act on the resulting backlog.
    """
    if _db is not None:
        return _firestore_dispatch_once()
    return _local_dispatch_once()


# ── Agent activity: which agent did what, per run, for the dashboard ───────
# agents/brain.py::OrchestratorSession.note() is already called after every
# one of the nine tool calls a cycle makes, with the AgentResult (ok/
# cost_usd/turns) each one produced right there -- this is that same trace
# made structured and durable instead of only a plain-text line in a
# gitignored, non-durable local .agentra/logs/<run_id>.log file.
_AGENT_STEPS_PATH = AGENTRA_HOME / "agent_steps.jsonl"


def record_agent_step(
    app: str,
    run_id: str,
    agent: str,
    ok: bool | None,
    cost_usd: float,
    turns: int | None,
    summary: str,
) -> None:
    record = {
        "app": app,
        "run_id": run_id,
        "agent": agent,
        "ok": ok,
        "cost_usd": cost_usd,
        "turns": turns,
        "summary": summary,
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if _db is not None:
        _db.collection("agent_steps").add(record)
        return
    _AGENT_STEPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _AGENT_STEPS_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


def list_agent_steps(app: str | None = None, limit: int = 100) -> list[dict]:
    """Newest first. Deliberately does not filter by `app` inside the
    Firestore query itself -- combining an equality filter on one field
    with order_by on another needs a composite index Firestore won't
    auto-create, and at this scale (a handful of apps, modest step volume)
    fetching `limit` globally-recent steps and filtering in Python is
    simpler than provisioning one. Revisit if step volume ever makes that
    genuinely wasteful."""
    # Fetching more than `limit` globally when filtering by app -- otherwise
    # a burst of steps from other apps could crowd out an app's own recent
    # history before the Python-side filter ever sees it.
    fetch_limit = limit * 5 if app is not None else limit

    if _db is not None:
        from google.cloud import firestore

        docs = (
            _db.collection("agent_steps")
            .order_by("ts", direction=firestore.Query.DESCENDING)
            .limit(fetch_limit)
            .stream()
        )
        steps = [d.to_dict() for d in docs]
    else:
        if not _AGENT_STEPS_PATH.exists():
            return []
        steps = [json.loads(line) for line in _AGENT_STEPS_PATH.read_text().splitlines()]
        steps.sort(key=lambda s: s["ts"], reverse=True)
        steps = steps[:fetch_limit]

    if app is not None:
        steps = [s for s in steps if s.get("app") == app]
    return steps[:limit]


# ── Runs: durable record of every background cycle server.py kicks off ─────
# server.py's _active_runs dict is process-local by design (see its own
# docstring) so polling a specific run_key from the same request that
# started it works without any of this. But the dashboard's "Runs" tab
# reads /runs across restarts and redeploys -- every real dogfooding cycle
# looked like it vanished the moment Cloud Run rolled a new revision, which
# is what actually prompted this. Firestore doc ID is the run_key itself so
# repeated status updates (queued -> running -> completed/failed) are a
# plain upsert, not an append log like agent_steps.
_RUNS_PATH = AGENTRA_HOME / "runs.json"


def _local_runs() -> dict[str, dict]:
    if not _RUNS_PATH.exists():
        return {}
    return json.loads(_RUNS_PATH.read_text())


def _local_save_runs(runs: dict[str, dict]) -> None:
    _RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _RUNS_PATH.write_text(json.dumps(runs, indent=2))


def record_run(run_key: str, **fields: Any) -> None:
    """Upsert -- pass only the fields that changed (e.g. just `status` on a
    transition, or `status`+`result` together on completion)."""
    if _db is not None:
        _db.collection("runs").document(run_key).set(fields, merge=True)
        return
    runs = _local_runs()
    runs.setdefault(run_key, {}).update(fields)
    _local_save_runs(runs)


def get_run(run_key: str) -> dict | None:
    if _db is not None:
        doc = _db.collection("runs").document(run_key).get()
        return doc.to_dict() if doc.exists else None
    return _local_runs().get(run_key)


def list_runs(limit: int = 50) -> list[dict]:
    """Newest first."""
    if _db is not None:
        from google.cloud import firestore

        docs = (
            _db.collection("runs")
            .order_by("started_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        return [{"run_key": d.id, **d.to_dict()} for d in docs]

    runs = _local_runs()
    ordered = sorted(
        ({"run_key": key, **info} for key, info in runs.items()),
        key=lambda r: r["started_at"],
        reverse=True,
    )
    return ordered[:limit]


# ── Loops: the "what actually happened, end to end" view ───────────────────
# A single run or a single agent_steps row each answer a narrow question
# ("what happened in this one cycle" / "how's this one agent doing").
# Neither answers "what did agentra accomplish working toward this
# objective, across every cycle it took." A loop groups every run made
# while an app's objective was unchanged -- deterministic from the
# objective text itself, not a separately tracked "current loop" pointer,
# so grouping needs no new start/stop action: editing an app's objective
# (the dashboard's Edit modal, or `agentra objective set`) naturally starts
# a new loop the moment the next run picks up the new text, and reverting
# to a previous exact objective naturally rejoins that earlier loop.


def loop_id_for(objective: str) -> str:
    import hashlib

    return hashlib.sha1(objective.encode("utf-8")).hexdigest()[:10]


def list_loops(app: str | None = None) -> list[dict]:
    """One object per loop_id for `app` (every app's loops, newest first,
    when app is None -- the dashboard's "All apps" view): the objective it
    was working toward, every run made under it (oldest first), and
    cost/agent aggregates across all of them. Cross-references agent_steps
    by run_key (server.py passes its run_key down as the cycle's own run_id,
    so the two are the same value for anything dispatched over HTTP -- see
    run_autonomous_cycle's run_id parameter)."""
    runs = [r for r in list_runs(limit=1000) if (app is None or r.get("app") == app) and r.get("loop_id")]
    steps = list_agent_steps(app=app, limit=2000)
    steps_by_run_key: dict[str, list[dict]] = {}
    for s in steps:
        steps_by_run_key.setdefault(s.get("run_id"), []).append(s)

    loops: dict[tuple[str, str], dict] = {}
    for run in runs:
        # loop_id alone is a hash of the objective text (loop_id_for),
        # nothing app-scoped about it -- fine when this function only ever
        # saw one app's runs, but aggregating across every app (app=None)
        # means two apps with the same objective text would otherwise
        # silently merge into a single loop mixing both apps' runs
        # together. Keying on (app, loop_id) keeps the single-app case
        # identical (app is constant within it anyway) while making the
        # cross-app case actually correct.
        key = (run.get("app"), run["loop_id"])
        loop = loops.setdefault(
            key, {"loop_id": run["loop_id"], "app": run.get("app"), "objective": run.get("objective"), "runs": []}
        )
        loop["runs"].append(run)

    result = []
    for loop in loops.values():
        loop["runs"].sort(key=lambda r: r["started_at"])
        loop["started_at"] = loop["runs"][0]["started_at"]
        loop["cost_usd"] = sum((r.get("result") or {}).get("cost_usd") or 0 for r in loop["runs"])
        loop["cycles_completed"] = sum(1 for r in loop["runs"] if r.get("status") == "completed")
        loop["cycles_total"] = len(loop["runs"])

        agent_totals: dict[str, dict] = {}
        for r in loop["runs"]:
            for s in steps_by_run_key.get(r["run_key"], []):
                bucket = agent_totals.setdefault(s["agent"], {"agent": s["agent"], "count": 0, "cost_usd": 0.0})
                bucket["count"] += 1
                bucket["cost_usd"] += s.get("cost_usd") or 0
        loop["agents"] = sorted(agent_totals.values(), key=lambda a: -a["cost_usd"])
        result.append(loop)

    result.sort(key=lambda l: l["started_at"], reverse=True)
    return result


def last_run_at(app: str, source: str | None = None) -> float | None:
    """Most recent started_at for `app` (optionally filtered to one
    trigger source), or None if it's never run. Backs the per-app
    schedule_hours check in server.py's /trigger/scheduled -- one Cloud
    Scheduler cron tick can serve every app on its own configured cadence
    by asking "was the last scheduled run recently enough" instead of
    needing a real GCP Scheduler job per app."""
    matches = [r for r in list_runs(limit=200) if r.get("app") == app and (source is None or r.get("source") == source)]
    if not matches:
        return None
    return max(r["started_at"] for r in matches)


# A run stuck at queued/running with no agent_steps activity for this long
# is presumed orphaned -- the container that was executing it died mid-cycle
# (OOM kill, revision rollout, etc.) without ever reaching the except block
# that would normally write status="failed". Confirmed live: a Cloud Run
# OOM kill left a run showing "running" in the dashboard indefinitely, with
# no process left anywhere that would ever complete it.
STALE_RUN_SECONDS = 600


def reconcile_stale_runs() -> list[str]:
    """Marks orphaned running/queued runs as failed. Called opportunistically
    from GET /runs (server.py) rather than a separate background loop --
    the dashboard already polls that endpoint every few seconds, so any
    open dashboard self-heals this within one poll cycle; a run nobody's
    looking at stays "running" a little longer, which is an acceptable
    tradeoff against the complexity of a standalone sweep task that could
    itself be killed mid-sweep. Returns the run_keys it marked failed."""
    now = time.time()
    steps_by_run: dict[str, float] = {}
    for step in list_agent_steps(limit=500):
        run_id = step.get("run_id")
        if not run_id:
            continue
        try:
            ts = dt.datetime.fromisoformat(step["ts"]).timestamp()
        except (KeyError, ValueError):
            continue
        if run_id not in steps_by_run or ts > steps_by_run[run_id]:
            steps_by_run[run_id] = ts

    marked: list[str] = []
    for run in list_runs(limit=200):
        if run.get("status") not in ("queued", "running"):
            continue
        run_key = run["run_key"]
        last_activity = steps_by_run.get(run_key, run.get("started_at", now))
        if now - last_activity > STALE_RUN_SECONDS:
            record_run(
                run_key,
                status="failed",
                error=f"orphaned: no activity for over {STALE_RUN_SECONDS // 60} minutes -- "
                "the process running this cycle likely died (e.g. an OOM kill or revision rollout)",
            )
            marked.append(run_key)
    return marked

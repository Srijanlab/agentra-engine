"""Multi-app registry and durable inbox — the "every app sends requests to
agentra" layer (vision.md's system operating across many apps, not tied to
one project).

Lives at ~/.agentra/ (AGENTRA_HOME), separate from each app's own
<repo>/.agentra/ memory -- this is agentra's own state about *which* apps
it manages and *what's waiting to be absorbed* into them, not any single
app's own audit trail.

Durability is the whole point here, not an afterthought: a submitted
request is written to disk (pending/) before this function ever returns
success to the caller. Processing claims a request by an atomic filesystem
rename (pending/ -> processing/), and only removes it (processing/ -> done/)
once it's been merged into the target app's own ledgers. If the process
crashes between those two steps, the request is still sitting in
processing/ on disk -- dispatch_once() checks for exactly that on every
run and resumes it, so nothing is ever silently lost to an in-memory queue
that a crash would wipe out.
"""

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from agentra.memory import Memory

# NOTE: not `Path(os.environ.get(...)) or Path.home() / ".agentra"` -- Path objects
# have no __bool__/__len__, so Path("") is truthy and that pattern silently never
# falls through to the default (resolves to the cwd instead, wherever that happens
# to be for a given invocation). Caught this live: it made the registry appear to
# lose all state between container runs, since each run's cwd differed.
_env_value = os.environ.get("AGENTRA_HOME")
AGENTRA_HOME = Path(_env_value) if _env_value else Path.home() / ".agentra"
APPS_PATH = AGENTRA_HOME / "apps.json"
INBOX_ROOT = AGENTRA_HOME / "inbox"

# TASK-016/018: where server.py's clone-on-register path checks a newly
# registered GitHub repo out to. Separate from AGENTRA_HOME (not
# AGENTRA_HOME/repos) so a deployment can put registry bookkeeping and repo
# checkouts on different volumes if it ever needs to -- deploy/gcp sets both
# to paths under the same GCS FUSE mount (storage.tf/cloudrun.tf), but
# nothing here assumes that.
_repos_env_value = os.environ.get("AGENTRA_REPOS_ROOT")
REPOS_ROOT = Path(_repos_env_value) if _repos_env_value else AGENTRA_HOME / "repos"

# A request left in processing/ longer than this is assumed to be from a crashed
# dispatch run, not one that's genuinely still in flight (processing itself is a
# fast, in-process JSON merge -- see dispatch_once -- so a real in-progress case
# never gets anywhere near this long).
STALE_PROCESSING_SECONDS = 10 * 60

REQUEST_TYPES = ("bug", "feature_request", "objective_change")


def _apps() -> dict[str, dict]:
    if not APPS_PATH.exists():
        return {}
    return json.loads(APPS_PATH.read_text())


def _save_apps(apps: dict[str, dict]) -> None:
    APPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    APPS_PATH.write_text(json.dumps(apps, indent=2))


# TASK-017: a durable, global kill switch every trigger path in server.py
# checks before dispatching new agent work. A plain marker file under
# AGENTRA_HOME rather than in-memory state -- must survive a restart (it
# lives on the same durable mount as apps.json once TASK-018's volume is in
# play), and a human hitting "pause" needs that to actually stick even if
# the instance recycles a minute later.
PAUSE_PATH = AGENTRA_HOME / "paused.json"


def is_paused() -> dict | None:
    """None if not paused; otherwise the pause record (who/when/why, for
    display -- the dashboard shows this so "who paused it and why" isn't
    lost the moment the button is clicked)."""
    if not PAUSE_PATH.exists():
        return None
    return json.loads(PAUSE_PATH.read_text())


def pause(reason: str | None = None) -> None:
    PAUSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_PATH.write_text(json.dumps({"paused_at": time.time(), "reason": reason}, indent=2))


def resume() -> None:
    PAUSE_PATH.unlink(missing_ok=True)


def register_app(name: str, repo_path: str) -> None:
    apps = _apps()
    apps[name] = {"repo_path": str(Path(repo_path).resolve())}
    _save_apps(apps)
    for sub in ("pending", "processing", "done"):
        (INBOX_ROOT / name / sub).mkdir(parents=True, exist_ok=True)


def remove_app(name: str) -> bool:
    apps = _apps()
    if name not in apps:
        return False
    del apps[name]
    _save_apps(apps)
    return True


def list_apps() -> dict[str, dict]:
    return _apps()


def get_app_repo(name: str) -> Path | None:
    app = _apps().get(name)
    return Path(app["repo_path"]) if app else None


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
    based adapter script, or later, an HTTP handler. Either way, by the time
    this returns, the request is on disk in pending/, not held anywhere in
    memory only.
    """
    if request_type not in REQUEST_TYPES:
        raise ValueError(f"unknown request type: {request_type!r}, must be one of {REQUEST_TYPES}")
    if app not in _apps():
        raise ValueError(f"unknown app {app!r} -- register it first with `agentra apps add`")

    request_id = uuid.uuid4().hex[:12]
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


def _resume_stale_processing(app: str) -> int:
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


def dispatch_once() -> DispatchSummary:
    """Absorb everything currently sitting in the inbox into each app's own
    ledgers. Cheap and fast (pure file/JSON work, no LLM calls) -- meant to run
    frequently (e.g. every few minutes via cron), separate from the actual
    (expensive, slower) `agentra run` cycles that act on the resulting backlog.
    """
    resumed_total = 0
    processed = 0
    errors: list[str] = []

    for app, info in _apps().items():
        repo = Path(info["repo_path"])
        resumed_total += _resume_stale_processing(app)

        pending_dir = INBOX_ROOT / app / "pending"
        processing_dir = INBOX_ROOT / app / "processing"
        done_dir = INBOX_ROOT / app / "done"
        processing_dir.mkdir(parents=True, exist_ok=True)
        done_dir.mkdir(parents=True, exist_ok=True)

        if not pending_dir.is_dir():
            continue

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

    return DispatchSummary(resumed_stale=resumed_total, processed=processed, errors=errors)

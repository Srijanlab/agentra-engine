"""registry/inbox.py — durable request inbox: submit and drain."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from agentra.registry import core

logger = logging.getLogger(__name__)


@dataclass
class DispatchSummary:
    resumed_stale: int
    processed: int
    errors: list[str]


def submit_request(
    app: str,
    request_type: str,
    description: str,
    severity: str | None = None,
    screenshot_url: str | None = None,
    title: str | None = None,
) -> str:
    if request_type not in core.REQUEST_TYPES:
        raise ValueError(f"unknown request type: {request_type!r}, must be one of {core.REQUEST_TYPES}")
    if app not in core.list_apps():
        raise ValueError(f"unknown app {app!r} -- register it first with `agentra apps add`")

    request_id = uuid.uuid4().hex[:12]

    if core._db is not None:
        core._db.collection("apps").document(app).collection("requests").document(request_id).set(
            {
                "id": request_id,
                "app": app,
                "type": request_type,
                "title": title,
                "description": description,
                "severity": severity,
                "screenshot_url": screenshot_url,
                "received_at": time.time(),
                "status": "pending",
            }
        )
        return request_id

    pending_dir = core.INBOX_ROOT / app / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": request_id, "app": app, "type": request_type, "title": title,
        "description": description, "severity": severity,
        "screenshot_url": screenshot_url, "received_at": time.time(),
    }
    tmp_path = pending_dir / f".{request_id}.tmp"
    final_path = pending_dir / f"{request_id}.json"
    tmp_path.write_text(json.dumps(payload, indent=2))
    os.rename(tmp_path, final_path)
    return request_id


def _apply_request(repo: Path, request: dict) -> None:
    from agentra.memory import Memory

    mem = Memory(repo)
    request_type = request["type"]
    if request_type == "bug":
        mem.record_known_bug(
            run_id=request["id"], severity=request.get("severity") or "medium",
            diagnosis=request["description"], proposed_fix="",
            source="customer", external_id=request["id"], title=request.get("title"),
        )
    elif request_type == "feature_request":
        mem.record_feature_request(
            description=request["description"], source="customer",
            external_id=request["id"], title=request.get("title"),
        )
    elif request_type == "objective_change":
        mem.set_objective(request["description"])
    else:
        raise ValueError(f"unknown request type: {request_type!r}")


def _local_resume_stale_processing(app: str) -> int:
    resumed = 0
    processing_dir = core.INBOX_ROOT / app / "processing"
    if not processing_dir.is_dir():
        return 0
    now = time.time()
    for path in processing_dir.glob("*.json"):
        if now - path.stat().st_mtime < core.STALE_PROCESSING_SECONDS:
            continue
        os.rename(path, core.INBOX_ROOT / app / "pending" / path.name)
        resumed += 1
    return resumed


def _local_dispatch_once() -> DispatchSummary:
    resumed_total = 0
    processed = 0
    errors: list[str] = []

    for app, info in core.list_apps().items():
        repo = Path(info["repo_path"])
        resumed_total += _local_resume_stale_processing(app)

        pending_dir = core.INBOX_ROOT / app / "pending"
        processing_dir = core.INBOX_ROOT / app / "processing"
        done_dir = core.INBOX_ROOT / app / "done"
        processing_dir.mkdir(parents=True, exist_ok=True)
        done_dir.mkdir(parents=True, exist_ok=True)

        if not pending_dir.is_dir():
            continue

        applied_any = False
        for path in sorted(pending_dir.glob("*.json")):
            processing_path = processing_dir / path.name
            try:
                os.rename(path, processing_path)
            except OSError as exc:
                errors.append(f"{app}/{path.name}: could not claim ({exc})")
                continue
            try:
                request = json.loads(processing_path.read_text())
                _apply_request(repo, request)
            except Exception as exc:
                errors.append(f"{app}/{path.name}: {exc}")
                continue
            os.rename(processing_path, done_dir / path.name)
            processed += 1
            applied_any = True

        if applied_any:
            error = core.persist_agentra_dir(repo, info.get("branch") or "main", f"agentra: absorb inbox requests for {app!r}")
            if error:
                errors.append(f"{app}: applied locally but failed to push .agentra/: {error}")

    return DispatchSummary(resumed_stale=resumed_total, processed=processed, errors=errors)


def _firestore_try_claim(doc_ref) -> bool:
    from google.cloud import firestore

    transaction = core._db.transaction()

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

    for app, info in core.list_apps().items():
        repo = Path(info["repo_path"])
        requests_ref = core._db.collection("apps").document(app).collection("requests")

        for doc in requests_ref.where(filter=FieldFilter("status", "==", "processing")).stream():
            claimed_at = doc.get("claimed_at") or 0
            if now - claimed_at < core.STALE_PROCESSING_SECONDS:
                continue
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
                continue
            doc.reference.update({"status": "done"})
            processed += 1
            applied_any = True

        if applied_any:
            error = core.persist_agentra_dir(repo, info.get("branch") or "main", f"agentra: absorb inbox requests for {app!r}")
            if error:
                errors.append(f"{app}: applied locally but failed to push .agentra/: {error}")

    return DispatchSummary(resumed_stale=resumed_total, processed=processed, errors=errors)


def dispatch_once() -> DispatchSummary:
    if core._db is not None:
        return _firestore_dispatch_once()
    return _local_dispatch_once()

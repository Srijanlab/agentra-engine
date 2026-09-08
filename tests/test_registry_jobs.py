"""registry/jobs.py — the work queue the engine records and the loop drains."""

import time

import pytest

from agentra import registry
from agentra.registry import jobs


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "AGENTRA_HOME", tmp_path)
    monkeypatch.setattr(registry, "_JOBS_PATH", tmp_path / "jobs.json")


def test_enqueue_returns_an_id_and_lands_pending():
    jid = registry.enqueue_job("cycle", {"app": "agentra"})
    [job] = registry.list_jobs()
    assert job["job_id"] == jid
    assert job["status"] == "pending"
    assert job["kind"] == "cycle"
    assert job["payload"] == {"app": "agentra"}
    assert job["attempts"] == 0


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        registry.enqueue_job("not_a_kind", {})


def test_claim_is_fifo_and_marks_the_job_claimed():
    a = registry.enqueue_job("cycle", {"n": 1})
    time.sleep(0.01)
    b = registry.enqueue_job("cycle", {"n": 2})

    first = registry.claim_next_job()
    assert first["job_id"] == a
    assert first["status"] == "claimed"
    assert first["attempts"] == 1
    assert registry.claim_next_job()["job_id"] == b
    assert registry.claim_next_job() is None  # nothing left pending


def test_report_moves_a_job_terminal_and_is_idempotent():
    jid = registry.enqueue_job("promote", {"app": "agentra"})
    registry.claim_next_job()

    registry.report_job(jid, "done", {"pr": 123})
    job = registry.list_jobs(status="done")[0]
    assert job["result"] == {"pr": 123}
    assert "expires_at" in job

    registry.report_job(jid, "done")  # no raise, no second entry
    assert len(registry.list_jobs(status="done")) == 1


def test_report_rejects_a_non_terminal_status():
    jid = registry.enqueue_job("cycle", {})
    with pytest.raises(ValueError):
        registry.report_job(jid, "claimed")


def test_dedup_key_returns_the_open_job_instead_of_a_duplicate():
    a = registry.enqueue_job("promote", {"app": "agentra"}, dedup_key="promote:agentra")
    b = registry.enqueue_job("promote", {"app": "agentra"}, dedup_key="promote:agentra")
    assert a == b
    assert len(registry.list_jobs()) == 1

    registry.claim_next_job()
    registry.report_job(a, "done")
    # once terminal, the dedup key is free again
    c = registry.enqueue_job("promote", {"app": "agentra"}, dedup_key="promote:agentra")
    assert c != a


def test_a_stale_claimed_job_is_requeued(monkeypatch):
    monkeypatch.setattr(registry, "STALE_PROCESSING_SECONDS", 0)
    jid = registry.enqueue_job("cycle", {})
    claimed = registry.claim_next_job()
    assert claimed["job_id"] == jid

    time.sleep(0.01)
    again = registry.claim_next_job()  # the stale claim is requeued then re-claimed
    assert again["job_id"] == jid
    assert again["attempts"] == 2

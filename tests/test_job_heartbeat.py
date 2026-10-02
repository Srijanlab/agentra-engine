"""Job lease renewal (heartbeat), heartbeat-based staleness, attempt cap, and the touch_job RPC."""

import time

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from agentra import registry, server
from agentra.registry import core, jobs
from agentra.registry import loops as _loops

TOKEN = "test-internal-token"


def _local(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_ddb", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", tmp_path)
    monkeypatch.setattr(registry, "_JOBS_PATH", tmp_path / "jobs.json")
    monkeypatch.setattr(registry, "_RUNS_PATH", tmp_path / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", tmp_path / "loops.json")
    monkeypatch.delenv("AGENTRA_STALE_HEARTBEAT_SECONDS", raising=False)
    monkeypatch.delenv("AGENTRA_JOB_MAX_ATTEMPTS", raising=False)


def _moto(monkeypatch):
    monkeypatch.setenv("AGENTRA_DYNAMODB_TABLE_PREFIX", "")
    monkeypatch.setenv("AGENTRA_AWS_REGION", "us-west-2")
    monkeypatch.delenv("AGENTRA_STALE_HEARTBEAT_SECONDS", raising=False)
    monkeypatch.delenv("AGENTRA_JOB_MAX_ATTEMPTS", raising=False)
    resource = boto3.resource("dynamodb", region_name="us-west-2")
    resource.create_table(
        TableName="jobs",
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "job_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "enqueued_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "by-status",
            "KeySchema": [
                {"AttributeName": "status", "KeyType": "HASH"},
                {"AttributeName": "enqueued_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setattr(core, "_ddb", resource)


@pytest.fixture(params=["local", "dynamo"])
def store(request, tmp_path, monkeypatch):
    if request.param == "local":
        _local(tmp_path, monkeypatch)
        yield "local"
    else:
        with mock_aws():
            _moto(monkeypatch)
            yield "dynamo"


def _job(job_id):
    return next(j for j in jobs.list_jobs() if j["job_id"] == job_id)


def _age_lease(job_id, seconds):
    old = time.time() - seconds
    jobs._write_job(job_id, {"claimed_at": old, "heartbeat_at": old})


def test_claim_sets_heartbeat_equal_to_claimed_at(store):
    jid = jobs.enqueue_job("cycle", {"app": "a"})
    claimed = jobs.claim_next_job()
    assert claimed["heartbeat_at"] == claimed["claimed_at"]
    stored = _job(jid)
    assert stored["heartbeat_at"] == pytest.approx(stored["claimed_at"])


def test_touch_job_extends_the_lease(store):
    jid = jobs.enqueue_job("cycle", {"app": "a"})
    jobs.claim_next_job()
    _age_lease(jid, 7200)
    assert jobs.touch_job(jid) is True
    job = _job(jid)
    assert job["heartbeat_at"] > job["claimed_at"]
    assert jobs.claim_next_job() is None
    assert _job(jid)["status"] == "claimed"


def test_touch_job_refuses_unknown_terminal_and_requeued_jobs(store):
    assert jobs.touch_job("nope") is False
    done = jobs.enqueue_job("cycle", {"app": "a"})
    jobs.claim_next_job()
    jobs.report_job(done, "done")
    assert jobs.touch_job(done) is False
    assert _job(done)["status"] == "done"

    stale = jobs.enqueue_job("cycle", {"app": "b"})
    jobs.claim_next_job()
    _age_lease(stale, 7200)
    jobs._requeue_stale_claims(time.time())
    assert _job(stale)["status"] == "pending"
    assert jobs.touch_job(stale) is False
    assert not _job(stale).get("heartbeat_at")


def test_release_job_frees_a_claimed_job_immediately(store):
    """Graceful shutdown: a container about to be replaced releases its
    in-flight job back to pending instead of leaving it claimed with no owner
    until the lease ceiling reclaims it."""
    jid = jobs.enqueue_job("cycle", {"app": "a"}, dedup_key="cycle:a")
    jobs.claim_next_job()
    assert jobs.release_job(jid) is True
    job = _job(jid)
    assert job["status"] == "pending"
    assert job["claimed_at"] is None
    # A fresh claim (a replacement container) picks it up right away, no
    # dependency on the lease ceiling having elapsed.
    reclaimed = jobs.claim_next_job()
    assert reclaimed["job_id"] == jid


def test_release_job_does_not_touch_the_attempt_count(store):
    jid = jobs.enqueue_job("cycle", {"app": "a"})
    jobs.claim_next_job()
    before = _job(jid)["attempts"]
    jobs.release_job(jid)
    jobs.claim_next_job()
    assert _job(jid)["attempts"] == before + 1


def test_release_job_is_a_noop_for_terminal_or_unknown_jobs(store):
    assert jobs.release_job("nope") is False
    done = jobs.enqueue_job("cycle", {"app": "a"})
    jobs.claim_next_job()
    jobs.report_job(done, "done")
    assert jobs.release_job(done) is False
    assert _job(done)["status"] == "done"


def test_release_job_is_a_noop_once_already_reclaimed(store):
    """A late release call (the old container finally unwinding after the
    lease ceiling already reclaimed and a new container started working it)
    must not stomp on the new attempt -- the caller passes the claimed_at it
    observed when it first claimed the job, so a mismatch (a newer claim)
    correctly leaves the fresh attempt alone."""
    jid = jobs.enqueue_job("cycle", {"app": "a"})
    original = jobs.claim_next_job()
    _age_lease(jid, 7200)
    jobs.claim_next_job()  # a different worker reclaims the stale lease
    assert jobs.release_job(jid, claimed_at=original["claimed_at"]) is False
    assert _job(jid)["status"] == "claimed"


def test_release_job_without_claimed_at_releases_unconditionally(store):
    """Backward-compatible bare call (no CAS guard) still releases whatever is
    currently claimed -- used when the caller has no prior claimed_at to check."""
    jid = jobs.enqueue_job("cycle", {"app": "a"})
    jobs.claim_next_job()
    assert jobs.release_job(jid) is True
    assert _job(jid)["status"] == "pending"


def test_stale_lease_is_requeued_and_reclaimable(store):
    jid = jobs.enqueue_job("cycle", {"app": "a"})
    jobs.claim_next_job()
    _age_lease(jid, 7200)
    reclaimed = jobs.claim_next_job()
    assert reclaimed["job_id"] == jid
    assert reclaimed["attempts"] == 2
    assert reclaimed["heartbeat_at"] == reclaimed["claimed_at"]


def test_old_claim_with_fresh_heartbeat_is_not_requeued(store):
    jid = jobs.enqueue_job("cycle", {"app": "a"})
    jobs.claim_next_job()
    jobs._write_job(jid, {"claimed_at": time.time() - 30 * 3600, "heartbeat_at": time.time() - 10})
    assert jobs.claim_next_job() is None
    assert _job(jid)["status"] == "claimed"


def test_stale_heartbeat_env_override(store, monkeypatch):
    jid = jobs.enqueue_job("cycle", {"app": "a"})
    jobs.claim_next_job()
    jobs._write_job(jid, {"heartbeat_at": time.time() - 120})
    assert jobs.claim_next_job() is None  # 120s < default 3600s
    monkeypatch.setenv("AGENTRA_STALE_HEARTBEAT_SECONDS", "60")
    assert jobs.claim_next_job()["job_id"] == jid


def test_stale_heartbeat_seconds_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("AGENTRA_STALE_HEARTBEAT_SECONDS", raising=False)
    assert core.stale_heartbeat_seconds() == core.STALE_PROCESSING_SECONDS == 3600
    monkeypatch.setenv("AGENTRA_STALE_HEARTBEAT_SECONDS", "junk")
    assert core.stale_heartbeat_seconds() == 3600
    monkeypatch.setenv("AGENTRA_STALE_HEARTBEAT_SECONDS", "90")
    assert core.stale_heartbeat_seconds() == 90


def test_attempt_cap_fails_a_poison_job_and_raises_a_gate(store, monkeypatch):
    monkeypatch.setenv("AGENTRA_JOB_MAX_ATTEMPTS", "2")
    notified = []
    from agentra.connectors import slack

    monkeypatch.setattr(slack, "notify_human_input_required", lambda **kw: notified.append(kw))
    monkeypatch.setattr(core, "get_slack_channel", lambda app: None)
    if store == "local":
        registry.record_run("rk1", app="a", status="running", started_at=time.time())
        payload = {"app": "a", "run_key": "rk1"}
    else:
        payload = {"app": "a"}
    jid = jobs.enqueue_job("cycle", payload)
    for expected_attempts in (1, 2):
        assert jobs.claim_next_job()["attempts"] == expected_attempts
        _age_lease(jid, 7200)
    assert jobs.claim_next_job() is None
    job = _job(jid)
    assert job["status"] == "failed"
    assert "HUMAN_INPUT_REQUIRED" in job["result"]["error"]
    assert len(notified) == 1 and notified[0]["app"] == "a"
    if store == "local":
        run = registry.get_run("rk1")
        assert run["status"] == "failed" and "HUMAN_INPUT_REQUIRED" in run["error"]


def test_touch_job_stamps_the_payload_run(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    registry.record_run("rk", app="a", status="running", started_at=1.0, updated_at=1.0)
    jid = jobs.enqueue_job("cycle", {"app": "a", "run_key": "rk"})
    jobs.claim_next_job()
    before = time.time()
    assert jobs.touch_job(jid) is True
    assert registry.get_run("rk")["updated_at"] >= before


def test_touch_job_ignores_a_differing_supplied_run_key(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    registry.record_run("rk", app="a", status="running", started_at=1.0, updated_at=1.0)
    registry.record_run("other", app="a", status="running", started_at=1.0, updated_at=1.0)
    jid = jobs.enqueue_job("cycle", {"app": "a", "run_key": "rk"})
    jobs.claim_next_job()
    before = time.time()
    assert jobs.touch_job(jid, run_key="other") is True
    assert registry.get_run("other")["updated_at"] == 1.0
    assert registry.get_run("rk")["updated_at"] >= before


def test_touch_job_ignores_supplied_run_key_without_payload_run_key(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    registry.record_run("other", app="a", status="running", started_at=1.0, updated_at=1.0)
    jid = jobs.enqueue_job("cycle", {"app": "a"})
    jobs.claim_next_job()
    assert jobs.touch_job(jid, run_key="other") is True
    assert registry.get_run("other")["updated_at"] == 1.0


def test_touch_job_never_creates_a_run(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    jid = jobs.enqueue_job("cycle", {"app": "a", "run_key": "ghost"})
    jobs.claim_next_job()
    assert jobs.touch_job(jid, run_key="ghost") is True
    assert jobs.touch_job(jid, run_key="phantom") is True
    assert registry.get_run("ghost") is None
    assert registry.get_run("phantom") is None


def test_record_run_stamps_updated_at_unless_given(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    registry.record_run("r1", app="a")
    assert registry.get_run("r1")["updated_at"] == pytest.approx(time.time(), abs=5)
    registry.record_run("r2", app="a", updated_at=5.0)
    assert registry.get_run("r2")["updated_at"] == 5.0


def test_reconcile_stale_runs_honours_the_heartbeat(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    old = time.time() - 5 * 3600
    registry.record_run("hb", app="a", status="running", started_at=old, updated_at=time.time() - 10)
    registry.record_run("dead", app="a", status="running", started_at=old, updated_at=old)
    monkeypatch.setenv("AGENTRA_STALE_HEARTBEAT_SECONDS", "600")
    assert registry.reconcile_stale_runs() == ["dead"]
    assert registry.get_run("hb")["status"] == "running"
    assert "over 10 minutes" in registry.get_run("dead")["error"]


def test_reconcile_stale_loops_needs_both_loop_and_run_stale(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    loop_id = registry.bind_loop("a", 7)
    registry.record_run("r1", app="a", loop_id=loop_id, status="running", started_at=1.0, updated_at=time.time() - 5)
    registry.roll_up_loop(loop_id, "r1", "running", 0.0)
    _loops._write_loop(loop_id, {"updated_at": time.time() - 7200})
    assert registry.reconcile_stale_loops() == []
    assert registry.get_loop(loop_id)["last_run_status"] == "running"
    registry.record_run("r1", updated_at=time.time() - 7200)
    assert registry.reconcile_stale_loops() == [loop_id]


@pytest.fixture
def client(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENTRA_INTERNAL_TOKEN", TOKEN)
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    monkeypatch.setattr(registry, "APPS_PATH", tmp_path / "apps.json")
    return TestClient(server.app)


def _rpc(client, body, token=TOKEN):
    return client.post("/internal/rpc", json=body, headers={"Authorization": f"Bearer {token}"})


def test_touch_job_rpc(client):
    jid = jobs.enqueue_job("cycle", {"app": "a"})
    jobs.claim_next_job()
    assert _rpc(client, {"target": "registry", "method": "touch_job", "args": [jid]}).json() == {"result": True}
    claimed = _rpc(client, {"target": "registry", "method": "list_jobs", "kwargs": {"status": "claimed"}}).json()["result"]
    assert claimed[0]["heartbeat_at"] >= claimed[0]["claimed_at"]
    missing = _rpc(client, {"target": "registry", "method": "touch_job", "args": ["nonexistent-job-id"]})
    assert missing.status_code == 200 and missing.json() == {"result": False}


def test_touch_job_rpc_requires_the_token(client):
    jid = jobs.enqueue_job("cycle", {"app": "a"})
    jobs.claim_next_job()
    _age_lease(jid, 100)
    before = _job(jid)["heartbeat_at"]
    body = {"target": "registry", "method": "touch_job", "args": [jid]}
    assert client.post("/internal/rpc", json=body).status_code == 401
    assert _rpc(client, body, token="wrong").status_code == 401
    assert _job(jid)["heartbeat_at"] == before


def test_heartbeat_endpoint(client):
    jid = jobs.enqueue_job("cycle", {"app": "a"})
    jobs.claim_next_job()
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post(f"/internal/jobs/{jid}/heartbeat", headers=auth).json() == {"renewed": True}
    assert client.post("/internal/jobs/nope/heartbeat", headers=auth).json() == {"renewed": False}
    assert client.post(f"/internal/jobs/{jid}/heartbeat").status_code == 401


def test_heartbeat_endpoint_never_creates_a_run(client):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    r = client.post("/internal/jobs/nope/heartbeat", json={"run_key": "nonexistent-run-xyz"}, headers=auth)
    assert r.status_code == 200 and r.json() == {"renewed": False}
    assert registry.get_run("nonexistent-run-xyz") is None
    jid = jobs.enqueue_job("cycle", {"app": "a"})
    jobs.claim_next_job()
    r = client.post(f"/internal/jobs/{jid}/heartbeat", json={"run_key": "nonexistent-run-xyz"}, headers=auth)
    assert r.json() == {"renewed": True}
    assert registry.get_run("nonexistent-run-xyz") is None


def _claim_two(app="a"):
    first = jobs.enqueue_job("cycle", {"app": app})
    second = jobs.enqueue_job("cycle", {"app": app})
    assert jobs.claim_next_job()["job_id"] == first
    assert jobs.claim_next_job()["job_id"] == second
    return first, second


def test_stale_job_is_requeued_despite_fresh_sibling_on_same_app(store):
    stale, fresh = _claim_two()
    _age_lease(stale, 7200)
    jobs._requeue_stale_claims(time.time())
    assert _job(stale)["status"] == "pending"
    assert _job(stale)["claimed_at"] is None
    assert _job(fresh)["status"] == "claimed"


def test_fresh_job_is_left_alone_by_requeue(store):
    fresh, other = _claim_two()
    before = _job(fresh)
    jobs._requeue_stale_claims(time.time())
    after = _job(fresh)
    assert after["status"] == "claimed"
    assert after["claimed_at"] == before["claimed_at"] and after["attempts"] == before["attempts"]
    assert _job(other)["status"] == "claimed"


def test_stale_claim_falls_back_to_claimed_at_without_heartbeat(store):
    jid = jobs.enqueue_job("cycle", {"app": "a"})
    jobs.claim_next_job()
    jobs._write_job(jid, {"claimed_at": time.time() - 7200, "heartbeat_at": None})
    jobs._requeue_stale_claims(time.time())
    assert _job(jid)["status"] == "pending"


def test_exhausted_stale_job_is_poison_failed_despite_fresh_sibling(store, monkeypatch):
    monkeypatch.setenv("AGENTRA_JOB_MAX_ATTEMPTS", "1")
    poisoned = []
    monkeypatch.setattr(jobs, "_fail_poison_job", lambda job: poisoned.append(job["job_id"]))
    stale, fresh = _claim_two()
    _age_lease(stale, 7200)
    jobs._requeue_stale_claims(time.time())
    assert poisoned == [stale]
    assert _job(stale)["status"] == "claimed"
    assert _job(fresh)["status"] == "claimed"


def test_exhausted_stale_job_ends_failed_with_human_gate(store, monkeypatch):
    monkeypatch.setenv("AGENTRA_JOB_MAX_ATTEMPTS", "1")
    from agentra.connectors import slack

    monkeypatch.setattr(slack, "notify_human_input_required", lambda **kw: None)
    monkeypatch.setattr(core, "get_slack_channel", lambda app: None)
    stale, fresh = _claim_two()
    _age_lease(stale, 7200)
    jobs._requeue_stale_claims(time.time())
    job = _job(stale)
    assert job["status"] == "failed" and "HUMAN_INPUT_REQUIRED" in job["result"]["error"]
    assert _job(fresh)["status"] == "claimed"

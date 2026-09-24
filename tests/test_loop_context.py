"""Per-loop durable context: registry, /internal endpoints, RPC whitelist, dashboard view."""

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from agentra import registry, server
from agentra.registry import _cache, _dynamo, core
from agentra.server import auth

TOKEN = "test-internal-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DASH = {"Authorization": "Bearer firebase-id-token"}
KEYS = {"objective", "current_step", "state", "decisions", "findings", "refs", "last_outcome", "updated_at"}
FULL = {
    "objective": "Ship X", "current_step": "implementation", "state": "in_progress",
    "decisions": ["use option A"], "findings": ["flaky test"],
    "refs": {"issue": 103, "pr": "7", "branch": "feat/x"}, "last_outcome": "tests failed",
}


def _create_loops_table(resource):
    resource.create_table(
        TableName="loops",
        KeySchema=[{"AttributeName": "loop_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "loop_id", "AttributeType": "S"},
            {"AttributeName": "app", "AttributeType": "S"},
            {"AttributeName": "updated_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "by-app-recency",
            "KeySchema": [{"AttributeName": "app", "KeyType": "HASH"}, {"AttributeName": "updated_at", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )


def _create_runs_table(resource):
    def gsi(name, hash_key):
        return {
            "IndexName": name,
            "KeySchema": [{"AttributeName": hash_key, "KeyType": "HASH"}, {"AttributeName": "started_at", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }

    resource.create_table(
        TableName="runs",
        KeySchema=[{"AttributeName": "run_key", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "run_key", "AttributeType": "S"},
            {"AttributeName": "shard", "AttributeType": "S"},
            {"AttributeName": "started_at", "AttributeType": "N"},
            {"AttributeName": "app", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[gsi("by-recency", "shard"), gsi("by-app-recency", "app")],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture(params=["local", "dynamodb"])
def client(request, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTRA_INTERNAL_TOKEN", TOKEN)
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    monkeypatch.setenv("AGENTRA_ALLOWED_EMAILS", "a@b.c")
    monkeypatch.setattr(auth, "_verify", lambda tok, proj: {"email": "a@b.c", "email_verified": True})
    home = tmp_path / "home"
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "_LOOPS_PATH", home / "loops.json")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    _cache.clear()
    if request.param == "local":
        monkeypatch.setattr(registry, "_ddb", None)
        yield TestClient(server.app)
    else:
        with mock_aws():
            monkeypatch.setenv("AGENTRA_DYNAMODB_TABLE_PREFIX", "")
            monkeypatch.setenv("AGENTRA_AWS_REGION", "us-west-2")
            resource = boto3.resource("dynamodb", region_name="us-west-2")
            _create_loops_table(resource)
            _create_runs_table(resource)
            monkeypatch.setattr(core, "_ddb", resource)
            _dynamo._table_cache.clear()
            yield TestClient(server.app)
            _dynamo._table_cache.clear()
    _cache.clear()


@pytest.fixture
def loop_id(client):
    return registry.bind_loop("app", 103, title="t")


def _get(client, loop_id, path="/internal/loops/{}/context"):
    return client.get(path.format(loop_id), headers=AUTH)


def _put(client, loop_id, body):
    return client.put(f"/internal/loops/{loop_id}/context", json=body, headers=AUTH)


def test_default_read(client, loop_id):
    r = _get(client, loop_id)
    assert r.status_code == 200
    assert r.json() == {
        "objective": None, "current_step": None, "state": None, "decisions": [], "findings": [],
        "refs": {"issue": None, "pr": None, "branch": None}, "last_outcome": None, "updated_at": None,
    }


def test_put_echoes_and_get_matches(client, loop_id):
    r = _put(client, loop_id, FULL)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == KEYS
    assert {k: body[k] for k in FULL} == FULL
    assert body["updated_at"] > 0
    assert _get(client, loop_id).json() == body


def test_partial_merge_preserves_omitted_fields(client, loop_id):
    first = _put(client, loop_id, FULL).json()
    second = _put(client, loop_id, {"state": "blocked"})
    assert second.status_code == 200
    body = second.json()
    assert body["state"] == "blocked"
    assert {k: body[k] for k in FULL if k != "state"} == {k: v for k, v in FULL.items() if k != "state"}
    assert body["updated_at"] >= first["updated_at"]
    assert _get(client, loop_id).json() == body


def test_lists_and_refs_are_replaced_wholesale(client, loop_id):
    _put(client, loop_id, FULL)
    body = _put(client, loop_id, {"decisions": ["only this"], "refs": {"pr": 9}}).json()
    assert body["decisions"] == ["only this"]
    assert body["findings"] == ["flaky test"]
    assert body["refs"] == {"issue": None, "pr": 9, "branch": None}


def test_explicit_null_clears_a_scalar(client, loop_id):
    _put(client, loop_id, FULL)
    assert _put(client, loop_id, {"objective": None}).json()["objective"] is None


@pytest.mark.parametrize("body", [
    {"bogus": 1},
    {"updated_at": 5.0},
    {"decisions": ["x"] * 101},
    {"findings": ["x"] * 101},
    {"objective": "x" * 4001},
    {"decisions": ["x" * 4001]},
    {"refs": {"branch": "x" * 4001}},
    {"refs": {"nope": 1}},
    {"decisions": None},
])
def test_invalid_bodies_422_and_leave_context_unchanged(client, loop_id, body):
    before = _put(client, loop_id, FULL).json()
    assert _put(client, loop_id, body).status_code == 422
    assert _get(client, loop_id).json() == before


def test_bounds_at_limit_are_accepted(client, loop_id):
    body = {"decisions": ["x" * 4000] * 100}
    assert _put(client, loop_id, body).status_code == 200


def test_unknown_loop_404_and_no_loop_created(client):
    assert _get(client, "does-not-exist").status_code == 404
    assert _put(client, "does-not-exist", {"state": "x"}).status_code == 404
    assert client.get("/loops/does-not-exist", headers=DASH).status_code == 404
    assert registry.get_loop("does-not-exist") is None


def test_put_does_not_touch_status_or_recency(client, loop_id):
    other = registry.bind_loop("app", 104, title="other")
    registry.roll_up_loop(loop_id, "r1", "completed", 0.1)
    registry.roll_up_loop(other, "r2", "completed", 0.1)
    before = client.get(f"/loops/{loop_id}", headers=DASH).json()
    order = [l["loop_id"] for l in client.get("/loops?app=app", headers=DASH).json()["loops"]]
    _put(client, loop_id, FULL)
    after = client.get(f"/loops/{loop_id}", headers=DASH).json()
    for key in ("status", "last_run_at", "run_count", "updated_at"):
        assert after[key] == before[key]
    assert [l["loop_id"] for l in client.get("/loops?app=app", headers=DASH).json()["loops"]] == order


def test_persists_across_a_fresh_read_of_the_store(client, loop_id):
    written = _put(client, loop_id, FULL).json()
    _cache.clear()
    assert registry.get_loop_context(loop_id) == written


@pytest.mark.parametrize("method", ["get", "put"])
def test_auth_failures(client, loop_id, monkeypatch, method):
    url = f"/internal/loops/{loop_id}/context"
    kwargs = {"json": {"state": "x"}} if method == "put" else {}
    call = getattr(client, method)
    assert call(url, **kwargs).status_code == 401
    wrong = call(url, headers={"Authorization": "Bearer nope"}, **kwargs)
    assert wrong.status_code == 401 and TOKEN not in wrong.text
    monkeypatch.delenv("AGENTRA_INTERNAL_TOKEN")
    unconfigured = call(url, headers=AUTH, **kwargs)
    assert unconfigured.status_code == 503 and TOKEN not in unconfigured.text


def _rpc(client, body):
    return client.post("/internal/rpc", json=body, headers=AUTH)


def test_rpc_whitelist_get_and_set(client, loop_id):
    r = _rpc(client, {"target": "registry", "method": "set_loop_context", "args": [loop_id], "kwargs": {"state": "waiting"}})
    assert r.status_code == 200
    assert r.json()["result"]["state"] == "waiting"
    got = _rpc(client, {"target": "registry", "method": "get_loop_context", "args": [loop_id]})
    assert got.status_code == 200
    assert got.json()["result"] == r.json()["result"]


def test_rpc_invalid_fields_422(client, loop_id):
    r = _rpc(client, {"target": "registry", "method": "set_loop_context", "args": [loop_id], "kwargs": {"bogus": 1}})
    assert r.status_code == 422


def test_dashboard_view_matches_internal_and_is_read_only(client, loop_id):
    _put(client, loop_id, FULL)
    dash = client.get(f"/loops/{loop_id}/context", headers=DASH)
    assert dash.status_code == 200
    assert dash.json() == _get(client, loop_id).json()
    assert client.get("/loops/does-not-exist/context", headers=DASH).status_code == 404
    for method in ("put", "post", "delete"):
        assert getattr(client, method)(f"/loops/{loop_id}/context", headers=DASH).status_code == 405


def test_dashboard_view_requires_sign_in(client, loop_id):
    r = client.get(f"/loops/{loop_id}/context")
    assert r.status_code == 401
    assert r.json()["error"] == "authentication_required"
    assert client.put(f"/loops/{loop_id}/context", json={"state": "x"}).status_code == 401
    assert client.get(f"/loops/{loop_id}/context", headers=DASH).status_code == 200

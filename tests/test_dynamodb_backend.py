"""DynamoDB-backed registry paths, mocked via moto (the cloud path had zero
test coverage under Firestore either -- this is a net-new addition, not a
port of existing tests). Covers Phase 1's PoC (agentra-system: pause/
llm_backend) plus the shared _dynamo.py helpers every later collection will
build on (merge_update, try_conditional_update).
"""

import boto3
import pytest
from moto import mock_aws

from agentra.registry import _dynamo, core


@pytest.fixture
def ddb_env(monkeypatch):
    """A real (moto-mocked) DynamoDB with the agentra-system table created,
    registry pointed at it, and _dynamo's table-object cache cleared so a
    fresh table shows up per test."""
    with mock_aws():
        monkeypatch.setenv("AGENTRA_DYNAMODB_TABLE_PREFIX", "")
        monkeypatch.setenv("AGENTRA_AWS_REGION", "us-west-2")
        resource = boto3.resource("dynamodb", region_name="us-west-2")
        resource.create_table(
            TableName="system",
            KeySchema=[{"AttributeName": "key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(core, "_ddb", resource)
        monkeypatch.setattr(core, "_llm_backend_cache", None)
        _dynamo._table_cache.clear()
        yield resource
        _dynamo._table_cache.clear()


def test_pause_resume_round_trip(ddb_env):
    assert core.is_paused() is None

    core.pause(reason="testing")
    state = core.is_paused()
    assert state is not None
    assert state["reason"] == "testing"
    assert isinstance(state["paused_at"], float)

    core.resume()
    assert core.is_paused() is None


def test_llm_backend_defaults_and_round_trips(ddb_env):
    assert core.get_llm_backend() == "claude"

    core.set_llm_backend("nim")
    assert core.get_llm_backend() == "nim"


def test_set_llm_backend_rejects_unknown_value(ddb_env):
    with pytest.raises(ValueError):
        core.set_llm_backend("not-a-real-backend")


def _scratch_table(resource):
    resource.create_table(
        TableName="scratch",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return resource.Table("scratch")


def test_merge_update_only_touches_given_fields(ddb_env):
    tbl = _scratch_table(ddb_env)
    _dynamo.put_item(tbl, {"pk": "run1", "status": "queued", "cost_usd": 0.0})

    _dynamo.merge_update(tbl, {"pk": "run1"}, {"status": "running"})

    item = _dynamo.get_item(tbl, {"pk": "run1"})
    assert item["status"] == "running"
    assert item["cost_usd"] == 0.0  # untouched


def test_merge_update_aliases_reserved_words(ddb_env):
    """"status" is a DynamoDB reserved word -- an unaliased UpdateExpression
    would fail outright."""
    tbl = _scratch_table(ddb_env)
    tbl.put_item(Item={"pk": "run1"})

    _dynamo.merge_update(tbl, {"pk": "run1"}, {"status": "failed", "error": "boom"})

    item = tbl.get_item(Key={"pk": "run1"})["Item"]
    assert item["status"] == "failed"
    assert item["error"] == "boom"


def test_try_conditional_update_succeeds_when_condition_matches(ddb_env):
    tbl = _scratch_table(ddb_env)
    tbl.put_item(Item={"pk": "req1", "status": "pending"})

    claimed = _dynamo.try_conditional_update(
        tbl, {"pk": "req1"}, {"status": "processing"}, condition_attr="status", condition_value="pending"
    )

    assert claimed is True
    assert tbl.get_item(Key={"pk": "req1"})["Item"]["status"] == "processing"


def test_try_conditional_update_fails_without_clobbering_on_conflict(ddb_env):
    tbl = _scratch_table(ddb_env)
    tbl.put_item(Item={"pk": "req1", "status": "processing"})

    claimed = _dynamo.try_conditional_update(
        tbl, {"pk": "req1"}, {"status": "processing"}, condition_attr="status", condition_value="pending"
    )

    assert claimed is False
    assert tbl.get_item(Key={"pk": "req1"})["Item"]["status"] == "processing"


def test_table_prefix_is_applied_and_memoized(ddb_env, monkeypatch):
    ddb_env.create_table(
        TableName="prefixed-apps",
        KeySchema=[{"AttributeName": "name", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "name", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setenv("AGENTRA_DYNAMODB_TABLE_PREFIX", "prefixed-")

    first = _dynamo.table("apps")
    second = _dynamo.table("apps")

    assert first.table_name == "prefixed-apps"
    assert first is second  # memoized

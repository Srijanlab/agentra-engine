"""server/gh_cache.py -- zero test coverage before this file (local/CLI mode
just calls the producer directly, so nothing exercised the cloud path under
Firestore either). Moto-backed since the whole point is real TTL/expiry and
native-DynamoDB-TTL-attribute behavior, not a hand-shaped fake.
"""

import boto3
import pytest
from moto import mock_aws

from agentra import registry
from agentra.registry import _dynamo
from agentra.server import gh_cache


@pytest.fixture
def ddb_gh_cache(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AGENTRA_DYNAMODB_TABLE_PREFIX", "")
        resource = boto3.resource("dynamodb", region_name="us-west-2")
        resource.create_table(
            TableName="gh-cache",
            KeySchema=[{"AttributeName": "key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(registry, "_ddb", resource)
        _dynamo._table_cache.clear()
        gh_cache._local.clear()
        yield resource
        _dynamo._table_cache.clear()
        gh_cache._local.clear()


async def _producer(calls, value="fresh"):
    calls.append(1)
    return value


def test_local_mode_always_calls_the_producer(monkeypatch):
    """No AGENTRA_DYNAMODB_TABLE_PREFIX configured -- local/CLI/tests, no
    quota pressure, no cache at all."""
    import asyncio

    monkeypatch.setattr(registry, "_ddb", None)
    calls = []

    result1 = asyncio.run(gh_cache.cached("k1", lambda: _producer(calls)))
    result2 = asyncio.run(gh_cache.cached("k1", lambda: _producer(calls)))

    assert result1 == result2 == "fresh"
    assert len(calls) == 2  # producer called every time, no caching


def test_cache_hit_within_ttl_skips_the_producer(ddb_gh_cache):
    import asyncio

    calls = []
    asyncio.run(gh_cache.cached("k1", lambda: _producer(calls), ttl=90))
    result = asyncio.run(gh_cache.cached("k1", lambda: _producer(calls), ttl=90))

    assert result == "fresh"
    assert len(calls) == 1  # second call hit the in-process cache


def test_cache_persists_across_a_cold_local_cache(ddb_gh_cache):
    """Simulates a different serverless instance: the in-process _local dict
    is empty, but the DynamoDB item is still there and within TTL."""
    import asyncio

    calls = []
    asyncio.run(gh_cache.cached("k1", lambda: _producer(calls), ttl=90))
    gh_cache._local.clear()  # new cold instance

    result = asyncio.run(gh_cache.cached("k1", lambda: _producer(calls), ttl=90))

    assert result == "fresh"
    assert len(calls) == 1  # DynamoDB hit, producer not called again


def test_expired_entry_is_not_reused(ddb_gh_cache):
    import asyncio

    calls = []
    asyncio.run(gh_cache.cached("k1", lambda: _producer(calls, "old"), ttl=90))
    gh_cache._local.clear()
    item = _dynamo.get_item(_dynamo.table("gh-cache"), {"key": "k1"})
    _dynamo.put_item(_dynamo.table("gh-cache"), {**item, "ts": item["ts"] - 200})  # force it stale

    result = asyncio.run(gh_cache.cached("k1", lambda: _producer(calls, "new"), ttl=90))

    assert result == "new"
    assert len(calls) == 2  # once for the initial write, once more since it had expired


def test_write_sets_a_native_ttl_expires_at_attribute(ddb_gh_cache):
    import asyncio

    asyncio.run(gh_cache.cached("k1", lambda: _producer([]), ttl=90))

    item = _dynamo.get_item(_dynamo.table("gh-cache"), {"key": "k1"})
    assert item["expires_at"] > item["ts"]


def test_invalidate_drops_both_the_local_and_dynamodb_entry(ddb_gh_cache):
    import asyncio

    asyncio.run(gh_cache.cached("k1", lambda: _producer([]), ttl=90))

    gh_cache.invalidate("k1")

    assert _dynamo.get_item(_dynamo.table("gh-cache"), {"key": "k1"}) is None
    assert "k1" not in gh_cache._local

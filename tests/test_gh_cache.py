"""server/gh_cache/ -- covers both durable backends (DynamoDB via moto, and
local JSON under AGENTRA_HOME) since they must offer the same TTL / stale-on-
error semantics."""

import asyncio

import boto3
import pytest
from fastapi import HTTPException
from moto import mock_aws

from agentra import registry
from agentra.registry import _dynamo
from agentra.server import gh_cache
from agentra.server.gh_cache import _inprocess


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
        _inprocess.clear()
        yield resource
        _dynamo._table_cache.clear()
        _inprocess.clear()


@pytest.fixture
def local_gh_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_ddb", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", tmp_path / "agentra_home")
    _inprocess.clear()
    yield
    _inprocess.clear()


async def _producer(calls, value="fresh"):
    calls.append(1)
    return value


def _failing_producer(calls):
    async def _inner():
        calls.append(1)
        raise RuntimeError("GitHub is down")
    return _inner


# --- cache hit within TTL, for both backends -----------------------------------

def test_cache_hit_within_ttl_skips_the_producer_ddb(ddb_gh_cache):
    calls = []
    asyncio.run(gh_cache.cached("k1", lambda: _producer(calls), ttl=90))
    result = asyncio.run(gh_cache.cached("k1", lambda: _producer(calls), ttl=90))

    assert result == "fresh"
    assert len(calls) == 1  # second call hit the in-process cache


def test_cache_hit_within_ttl_skips_the_producer_local(local_gh_cache):
    calls = []
    asyncio.run(gh_cache.cached("k1", lambda: _producer(calls), ttl=90))
    result = asyncio.run(gh_cache.cached("k1", lambda: _producer(calls), ttl=90))

    assert result == "fresh"
    assert len(calls) == 1


def test_cache_persists_across_a_cold_local_cache(ddb_gh_cache):
    """Simulates a different serverless instance: the in-process dict is empty,
    but the DynamoDB item is still there and within TTL."""
    calls = []
    asyncio.run(gh_cache.cached("k1", lambda: _producer(calls), ttl=90))
    _inprocess.clear()  # new cold instance

    result = asyncio.run(gh_cache.cached("k1", lambda: _producer(calls), ttl=90))

    assert result == "fresh"
    assert len(calls) == 1  # DynamoDB hit, producer not called again


def test_local_json_persists_across_a_cold_in_process_cache(local_gh_cache):
    """Off cloud, the on-disk JSON copy under AGENTRA_HOME plays the same role
    the DynamoDB item plays in cloud mode -- it must not be a no-op (issue #15)."""
    calls = []
    asyncio.run(gh_cache.cached("k1", lambda: _producer(calls), ttl=90))
    _inprocess.clear()

    result = asyncio.run(gh_cache.cached("k1", lambda: _producer(calls), ttl=90))

    assert result == "fresh"
    assert len(calls) == 1
    assert (registry.AGENTRA_HOME / "gh-cache").exists()


# --- TTL expiry triggers a refresh ----------------------------------------------

def test_expired_entry_triggers_a_refresh_ddb(ddb_gh_cache):
    calls = []
    asyncio.run(gh_cache.cached("k1", lambda: _producer(calls, "old"), ttl=90))
    _inprocess.clear()
    item = _dynamo.get_item(_dynamo.table("gh-cache"), {"key": "k1"})
    # force it stale
    _dynamo.put_item(
        _dynamo.table("gh-cache"), {**item, "ts": item["ts"] - 200, "expires_at": item["expires_at"] - 200}
    )

    result = asyncio.run(gh_cache.cached("k1", lambda: _producer(calls, "new"), ttl=90))

    assert result == "new"
    assert len(calls) == 2  # once for the initial write, once more since it had expired


def test_expired_entry_triggers_a_refresh_local(local_gh_cache):
    calls = []
    asyncio.run(gh_cache.cached("k1", lambda: _producer(calls, "old"), ttl=0.05))
    import time

    time.sleep(0.1)
    _inprocess.clear()

    result = asyncio.run(gh_cache.cached("k1", lambda: _producer(calls, "new"), ttl=90))

    assert result == "new"
    assert len(calls) == 2


def test_write_sets_a_native_ttl_expires_at_attribute(ddb_gh_cache):
    asyncio.run(gh_cache.cached("k1", lambda: _producer([]), ttl=90))

    item = _dynamo.get_item(_dynamo.table("gh-cache"), {"key": "k1"})
    assert item["expires_at"] > item["ts"]


# --- stale-on-error fallback -----------------------------------------------------

def test_stale_on_error_returns_last_known_value_ddb(ddb_gh_cache):
    calls = []
    asyncio.run(gh_cache.cached("k1", lambda: _producer(calls, "good"), ttl=0.01))
    import time

    time.sleep(0.02)
    _inprocess.clear()

    result = asyncio.run(gh_cache.cached("k1", _failing_producer(calls)))

    assert result == "good"  # producer raised, but a prior value existed
    assert len(calls) == 2  # initial populate + the failed refresh attempt


def test_stale_on_error_returns_last_known_value_local(local_gh_cache):
    calls = []
    asyncio.run(gh_cache.cached("k1", lambda: _producer(calls, "good"), ttl=0.01))
    import time

    time.sleep(0.02)
    _inprocess.clear()

    result = asyncio.run(gh_cache.cached("k1", _failing_producer(calls)))

    assert result == "good"


def test_error_with_no_prior_value_propagates(local_gh_cache):
    calls = []
    with pytest.raises(RuntimeError):
        asyncio.run(gh_cache.cached("k1", _failing_producer(calls)))
    assert len(calls) == 1


def test_an_unregistered_app_404_is_never_masked_because_it_never_reaches_the_cache(local_gh_cache):
    """routes/apps.py checks `name not in registry.list_apps()` and raises 404
    *before* calling cached() at all -- so this case can't be masked by a stale
    value no matter what cached()'s error handling does. HTTPException raised
    from inside a producer (e.g. a 409 for a missing local checkout), by
    contrast, is treated like any other producer failure and does fall back to
    a stale value when one exists -- gh_cache has no route-shape awareness."""
    calls = []

    async def _good():
        calls.append(1)
        return "good"

    async def _boom():
        calls.append(1)
        raise HTTPException(status_code=409, detail="checkout missing")

    asyncio.run(gh_cache.cached("k1", _good, ttl=0.01))
    import time

    time.sleep(0.02)
    _inprocess.clear()

    result = asyncio.run(gh_cache.cached("k1", _boom))
    assert result == "good"  # stale-on-error applies uniformly, regardless of exception type


# --- invalidation ------------------------------------------------------------------

def test_invalidate_drops_both_layers_ddb(ddb_gh_cache):
    asyncio.run(gh_cache.cached("k1", lambda: _producer([]), ttl=90))

    gh_cache.invalidate("k1")

    assert _dynamo.get_item(_dynamo.table("gh-cache"), {"key": "k1"}) is None
    assert _inprocess.get("k1") is None


def test_invalidate_drops_both_layers_local(local_gh_cache):
    asyncio.run(gh_cache.cached("k1", lambda: _producer([]), ttl=90))

    gh_cache.invalidate("k1")

    assert _inprocess.get("k1") is None
    from agentra.server.gh_cache import _local_store

    assert _local_store.get("k1") is None


def test_invalidate_app_drops_every_dashboard_key_for_that_app(local_gh_cache):
    calls = []
    asyncio.run(gh_cache.cached("app_detail:demo", lambda: _producer(calls, "detail"), ttl=90))
    asyncio.run(gh_cache.cached("backlog_board:demo", lambda: _producer(calls, "board"), ttl=90))
    asyncio.run(gh_cache.cached("ready_to_review:demo", lambda: _producer(calls, "rtr"), ttl=90))
    asyncio.run(gh_cache.cached("digest_batch", lambda: _producer(calls, "digest"), ttl=90))
    asyncio.run(gh_cache.cached("app_detail:other", lambda: _producer(calls, "other-detail"), ttl=90))

    gh_cache.invalidate_app("demo")

    assert _inprocess.get("app_detail:demo") is None
    assert _inprocess.get("backlog_board:demo") is None
    assert _inprocess.get("ready_to_review:demo") is None
    assert _inprocess.get("digest_batch") is None
    assert _inprocess.get("app_detail:other") is not None  # a different app's entry is untouched


def test_invalidate_app_is_best_effort_and_never_raises(local_gh_cache, monkeypatch):
    from agentra.server.gh_cache import _local_store

    def boom(*keys):
        raise OSError("disk is unhappy")

    monkeypatch.setattr(_local_store, "delete", boom)
    gh_cache.invalidate_app("demo")  # must not raise


# --- configurable TTL --------------------------------------------------------------

def test_default_ttl_reads_the_env_var_and_clamps(monkeypatch):
    from agentra.server.gh_cache import default_ttl

    monkeypatch.delenv("AGENTRA_GH_CACHE_TTL_SECONDS", raising=False)
    assert default_ttl() == 45.0

    monkeypatch.setenv("AGENTRA_GH_CACHE_TTL_SECONDS", "30")
    assert default_ttl() == 30.0

    monkeypatch.setenv("AGENTRA_GH_CACHE_TTL_SECONDS", "5")  # below the floor
    assert default_ttl() == 10.0

    monkeypatch.setenv("AGENTRA_GH_CACHE_TTL_SECONDS", "10000")  # above the ceiling
    assert default_ttl() == 300.0

    monkeypatch.setenv("AGENTRA_GH_CACHE_TTL_SECONDS", "not-a-number")
    assert default_ttl() == 45.0


def test_cached_uses_the_configured_default_ttl_when_none_is_passed(local_gh_cache, monkeypatch):
    monkeypatch.setenv("AGENTRA_GH_CACHE_TTL_SECONDS", "33")
    calls = []

    asyncio.run(gh_cache.cached("k1", lambda: _producer(calls, "one")))

    entry = _inprocess.get("k1")
    assert entry["expires_at"] - entry["ts"] == pytest.approx(33, abs=0.5)


# --- etag plumbing is opportunistic and optional ------------------------------------

def test_etag_is_stored_and_passed_to_the_producer_on_refresh(local_gh_cache):
    from agentra.server.gh_cache import FetchResult

    seen_etags = []

    async def _producer_with_etag(etag=None):
        seen_etags.append(etag)
        return FetchResult(value=f"v{len(seen_etags)}", etag="etag-123")

    asyncio.run(gh_cache.cached("k1", _producer_with_etag, ttl=0.01))
    import time

    time.sleep(0.02)
    _inprocess.clear()
    asyncio.run(gh_cache.cached("k1", _producer_with_etag, ttl=90))

    assert seen_etags == [None, "etag-123"]  # first fetch has nothing to send, refresh sends the prior etag


def test_absence_of_an_etag_does_not_change_behavior(local_gh_cache):
    calls = []
    result = asyncio.run(gh_cache.cached("k1", lambda: _producer(calls, "plain"), ttl=90))
    assert result == "plain"
    assert _inprocess.get("k1")["etag"] is None

"""DynamoDB-backed registry paths, mocked via moto (the cloud path had zero
test coverage under Firestore either -- this is a net-new addition, not a
port of existing tests). Covers Phase 1's PoC (agentra-system: pause/
llm_backend) plus the shared _dynamo.py helpers every later collection will
build on (merge_update, try_conditional_update).
"""

import time

import boto3
import pytest
from moto import mock_aws

from agentra import registry
from agentra.registry import _cache, _dynamo, core


@pytest.fixture
def ddb_env(monkeypatch):
    """A real (moto-mocked) DynamoDB with the agentra-system and agentra-apps
    tables created, registry pointed at it, and every in-process cache
    cleared so a fresh table shows up per test."""
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
        resource.create_table(
            TableName="apps",
            KeySchema=[{"AttributeName": "name", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "name", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        resource.create_table(
            TableName="slack-threads",
            KeySchema=[{"AttributeName": "thread_ts", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "thread_ts", "AttributeType": "S"},
                {"AttributeName": "app", "AttributeType": "S"},
                {"AttributeName": "issue_number", "AttributeType": "N"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "by-app-issue",
                "KeySchema": [
                    {"AttributeName": "app", "KeyType": "HASH"},
                    {"AttributeName": "issue_number", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        resource.create_table(
            TableName="requests",
            KeySchema=[
                {"AttributeName": "app", "KeyType": "HASH"},
                {"AttributeName": "request_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "app", "AttributeType": "S"},
                {"AttributeName": "request_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "by-status",
                "KeySchema": [
                    {"AttributeName": "app", "KeyType": "HASH"},
                    {"AttributeName": "status", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(core, "_ddb", resource)
        monkeypatch.setattr(core, "_llm_backend_cache", None)
        _dynamo._table_cache.clear()
        _cache.clear()
        yield resource
        _dynamo._table_cache.clear()
        _cache.clear()


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


def test_register_list_and_remove_app(ddb_env, tmp_path):
    assert core.list_apps() == {}

    repo_path = str(tmp_path / "myapp")
    core.register_app("myapp", repo_path, repo_url="https://github.com/acme/myapp.git", branch="main")

    apps = core.list_apps()
    assert apps == {
        "myapp": {"repo_path": repo_path, "repo_url": "https://github.com/acme/myapp.git", "branch": "main"}
    }

    assert core.remove_app("myapp") is True
    assert core.list_apps() == {}
    assert core.remove_app("myapp") is False  # already gone


def test_register_app_with_multi_repo_shape_round_trips(ddb_env):
    repos = [
        {"name": "backlog", "repo_url": "https://github.com/acme/backlog.git", "branch": "main", "role": "coordination"},
        {"name": "engine", "repo_url": "https://github.com/acme/engine.git", "branch": "main", "role": "code"},
    ]

    core.register_app("agentra", repos=repos)

    assert core.list_apps()["agentra"]["repos"] == repos


def test_set_slack_channel_merges_without_clobbering_other_fields(ddb_env):
    core.register_app("myapp", "/tmp/myapp", repo_url="https://github.com/acme/myapp.git", branch="main")

    core.set_slack_channel("myapp", "C0123456")

    app = core.list_apps()["myapp"]
    assert app["slack_channel_id"] == "C0123456"
    assert app["repo_url"] == "https://github.com/acme/myapp.git"  # untouched


def test_list_apps_is_cached_within_ttl(ddb_env):
    core.register_app("myapp", "/tmp/myapp", repo_url="https://github.com/acme/myapp.git", branch="main")
    core.list_apps()  # warms the cache

    ddb_env.Table("apps").delete_item(Key={"name": "myapp"})  # bypasses the registry + its cache-drop

    assert "myapp" in core.list_apps()  # still cached
    _cache.drop("apps")
    assert "myapp" not in core.list_apps()  # cache dropped, sees the real (now-empty) table


def test_cloud_mode_true_with_either_backend_configured(monkeypatch):
    monkeypatch.setattr(core, "_db", None)
    monkeypatch.setattr(core, "_ddb", None)
    assert registry.cloud_mode() is False

    monkeypatch.setattr(core, "_db", object())
    assert registry.cloud_mode() is True

    monkeypatch.setattr(core, "_db", None)
    monkeypatch.setattr(core, "_ddb", object())
    assert registry.cloud_mode() is True


def test_slack_thread_round_trip(ddb_env):
    assert core.resolve_slack_thread("T123") is None

    core.record_slack_thread("T123", app="myapp", issue_number=42)

    assert core.resolve_slack_thread("T123") == {"app": "myapp", "issue_number": 42}


def test_slack_thread_for_finds_by_app_and_issue(ddb_env):
    core.record_slack_thread("T111", app="myapp", issue_number=1)
    core.record_slack_thread("T222", app="myapp", issue_number=42)
    core.record_slack_thread("T333", app="otherapp", issue_number=42)

    assert core.slack_thread_for("myapp", 42) == "T222"
    assert core.slack_thread_for("myapp", 999) is None
    assert core.slack_thread_for("otherapp", 42) == "T333"


def _register_app_with_repo(tmp_path, monkeypatch, name: str = "myapp"):
    """A real repo dir (Memory writes local files under its .agentra/) plus a
    fake GitHub backend (record_feature_request/set_objective both call out
    to GitHub -- no real network in tests)."""
    from agentra.connectors import github_fake

    repo = tmp_path / name
    repo.mkdir()
    repo_url = f"https://github.com/acme/{name}.git"
    core.register_app(name, str(repo), repo_url=repo_url, branch="main")
    return repo, github_fake.install(monkeypatch=monkeypatch)


def test_submit_and_dispatch_applies_a_feature_request(ddb_env, tmp_path, monkeypatch):
    from agentra.registry import inbox

    _register_app_with_repo(tmp_path, monkeypatch)

    request_id = inbox.submit_request("myapp", "feature_request", "Add dark mode")

    tbl = _dynamo.table("requests")
    item = _dynamo.get_item(tbl, {"app": "myapp", "request_id": request_id})
    assert item["status"] == "pending"

    summary = inbox.dispatch_once()

    assert summary.processed == 1
    assert summary.errors == []
    item = _dynamo.get_item(tbl, {"app": "myapp", "request_id": request_id})
    assert item["status"] == "done"


def test_dispatch_once_resumes_a_stale_processing_claim(ddb_env, tmp_path, monkeypatch):
    from agentra.registry import inbox

    _register_app_with_repo(tmp_path, monkeypatch)
    tbl = _dynamo.table("requests")
    _dynamo.put_item(tbl, {
        "app": "myapp", "request_id": "req1", "id": "req1", "type": "feature_request",
        "title": None, "description": "stale one", "severity": None, "screenshot_url": None,
        "received_at": 0.0, "status": "processing", "claimed_at": 0.0,  # ancient claim
    })

    summary = inbox.dispatch_once()

    assert summary.resumed_stale == 1
    assert summary.processed == 1  # resumed, then picked up and completed in the same pass
    item = _dynamo.get_item(tbl, {"app": "myapp", "request_id": "req1"})
    assert item["status"] == "done"


def test_dispatch_once_leaves_a_fresh_processing_claim_alone(ddb_env, tmp_path, monkeypatch):
    from agentra.registry import inbox

    _register_app_with_repo(tmp_path, monkeypatch)
    tbl = _dynamo.table("requests")
    _dynamo.put_item(tbl, {
        "app": "myapp", "request_id": "req1", "id": "req1", "type": "feature_request",
        "title": None, "description": "in flight", "severity": None, "screenshot_url": None,
        "received_at": time.time(), "status": "processing", "claimed_at": time.time(),
    })

    summary = inbox.dispatch_once()

    assert summary.resumed_stale == 0
    assert summary.processed == 0
    item = _dynamo.get_item(tbl, {"app": "myapp", "request_id": "req1"})
    assert item["status"] == "processing"  # untouched -- another worker owns it

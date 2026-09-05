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
        resource.create_table(
            TableName="runs",
            KeySchema=[{"AttributeName": "run_key", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "run_key", "AttributeType": "S"},
                {"AttributeName": "shard", "AttributeType": "S"},
                {"AttributeName": "started_at", "AttributeType": "N"},
                {"AttributeName": "app", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-recency",
                    "KeySchema": [
                        {"AttributeName": "shard", "KeyType": "HASH"},
                        {"AttributeName": "started_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "by-app-recency",
                    "KeySchema": [
                        {"AttributeName": "app", "KeyType": "HASH"},
                        {"AttributeName": "started_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
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
                "KeySchema": [
                    {"AttributeName": "app", "KeyType": "HASH"},
                    {"AttributeName": "updated_at", "KeyType": "RANGE"},
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


def test_record_and_get_run_round_trips(ddb_env):
    from agentra.registry import runs

    assert runs.get_run("run1") is None

    runs.record_run("run1", app="myapp", source="scheduled", status="queued", started_at=100.0)

    run = runs.get_run("run1")
    assert run["app"] == "myapp"
    assert run["status"] == "queued"
    assert run["started_at"] == 100.0
    assert "shard" not in run  # internal indexing attribute never leaks out


def test_record_run_merges_without_clobbering(ddb_env):
    from agentra.registry import runs

    runs.record_run("run1", app="myapp", source="scheduled", status="queued", started_at=100.0)
    runs.record_run("run1", status="running")

    run = runs.get_run("run1")
    assert run["status"] == "running"
    assert run["app"] == "myapp"  # untouched


def test_list_runs_orders_by_recency_descending(ddb_env):
    from agentra.registry import runs

    runs.record_run("run1", app="myapp", source="scheduled", status="completed", started_at=100.0)
    runs.record_run("run2", app="myapp", source="scheduled", status="completed", started_at=300.0)
    runs.record_run("run3", app="myapp", source="scheduled", status="completed", started_at=200.0)

    ordered = [r["run_key"] for r in runs.list_runs(limit=10)]

    assert ordered == ["run2", "run3", "run1"]


def test_last_run_at_filters_by_app_and_source(ddb_env):
    from agentra.registry import runs

    runs.record_run("run1", app="myapp", source="scheduled", status="completed", started_at=100.0)
    runs.record_run("run2", app="myapp", source="on-demand", status="completed", started_at=300.0)
    runs.record_run("run3", app="otherapp", source="scheduled", status="completed", started_at=500.0)

    assert runs.last_run_at("myapp") == 300.0
    assert runs.last_run_at("myapp", source="scheduled") == 100.0
    assert runs.last_run_at("otherapp") == 500.0
    assert runs.last_run_at("nonexistent") is None


def test_reconcile_stale_runs_marks_orphaned_runs_failed(ddb_env):
    from agentra.registry import runs

    now = time.time()
    runs.record_run("stale1", app="myapp", source="scheduled", status="running", started_at=now - 7200, updated_at=now - 7200)
    runs.record_run("fresh1", app="myapp", source="scheduled", status="running", started_at=now - 10, updated_at=now - 10)

    marked = runs.reconcile_stale_runs()

    assert marked == ["stale1"]
    assert runs.get_run("stale1")["status"] == "failed"
    assert runs.get_run("fresh1")["status"] == "running"


def test_list_waiting_for_human(ddb_env):
    from agentra.registry import runs

    runs.record_run("run1", app="myapp", source="scheduled", status="waiting_for_human", started_at=100.0)
    runs.record_run("run2", app="myapp", source="scheduled", status="escalated", started_at=200.0)
    runs.record_run("run3", app="myapp", source="scheduled", status="completed", started_at=300.0)

    waiting = {r["run_key"] for r in runs.list_waiting_for_human()}

    assert waiting == {"run1", "run2"}


def test_reconcile_waiting_for_human_escalates_past_the_deadline(ddb_env):
    from agentra.registry import runs

    now = time.time()
    runs.record_run(
        "run1", app="myapp", source="scheduled", status="waiting_for_human", started_at=now - 100000,
        human_input={"waiting_since": now - 100000},
    )

    escalated = runs.reconcile_waiting_for_human()

    assert [r["run_key"] for r in escalated] == ["run1"]
    assert runs.get_run("run1")["status"] == "escalated"


def test_bind_loop_roll_up_and_status(ddb_env):
    from agentra.registry import loops

    loop_id = loops.bind_loop("myapp", 42, title="A feature", objective="ship it")
    loop = loops.get_loop(loop_id)
    assert loop["app"] == "myapp"
    assert loop["status"] == "active"
    assert loop["run_count"] == 0

    loops.roll_up_loop(loop_id, "run1", "completed", 0.5)
    loop = loops.get_loop(loop_id)
    assert loop["run_count"] == 1
    assert loop["total_cost_usd"] == 0.5
    assert loop["last_run_key"] == "run1"

    loops.set_loop_status(loop_id, "shipped")
    assert loops.get_loop(loop_id)["status"] == "shipped"


def test_bind_loop_is_idempotent(ddb_env):
    from agentra.registry import loops

    loop_id1 = loops.bind_loop("myapp", 42)
    loops.roll_up_loop(loop_id1, "run1", "completed", 1.0)
    loop_id2 = loops.bind_loop("myapp", 42)  # same issue -- refresh, not recreate

    assert loop_id1 == loop_id2
    assert loops.get_loop(loop_id1)["run_count"] == 1  # not reset back to 0


def test_get_loop_includes_its_runs_newest_first(ddb_env):
    from agentra.registry import loops, runs

    loop_id = loops.bind_loop("myapp", 42)
    runs.record_run("run1", app="myapp", source="scheduled", status="completed", started_at=100.0, loop_id=loop_id)
    runs.record_run("run2", app="myapp", source="scheduled", status="completed", started_at=200.0, loop_id=loop_id)
    runs.record_run("run3", app="myapp", source="scheduled", status="completed", started_at=50.0, loop_id="other-loop")

    loop = loops.get_loop(loop_id)

    assert [r["run_key"] for r in loop["runs"]] == ["run2", "run1"]


def test_list_loops_filters_by_app_via_the_gsi(ddb_env):
    from agentra.registry import loops

    loops.bind_loop("myapp", 1)
    loops.bind_loop("myapp", 2)
    loops.bind_loop("otherapp", 3)

    mine = loops.list_loops(app="myapp")

    assert {l["loop_id"] for l in mine} == {loops.bind_loop("myapp", 1), loops.bind_loop("myapp", 2)}


def test_list_loops_unfiltered_scans_and_sorts_by_recency(ddb_env):
    from agentra.registry import loops

    loops.bind_loop("myapp", 1)
    loops.bind_loop("otherapp", 2)

    all_loops = loops.list_loops()

    assert len(all_loops) == 2


def test_list_agent_steps_delegates_to_langfuse(ddb_env, monkeypatch):
    from agentra.registry import runs

    captured = {}

    def fake_list_recent_generations(app=None, limit=100):
        captured["app"] = app
        captured["limit"] = limit
        return [{"app": app, "agent": "implement_feature", "ok": True}]

    monkeypatch.setattr("agentra.langfuse_api.list_recent_generations", fake_list_recent_generations)

    steps = runs.list_agent_steps(app="myapp", limit=50)

    assert captured == {"app": "myapp", "limit": 50}
    assert steps == [{"app": "myapp", "agent": "implement_feature", "ok": True}]

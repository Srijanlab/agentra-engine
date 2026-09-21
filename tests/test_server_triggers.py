"""The engine's trigger endpoints record a run and enqueue a job for the loop
to claim -- the engine never executes a cycle / promotion / prod-debug itself.
"""

import base64
import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.connectors import github_fake
from agentra.memory import Memory


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _register_tmp_app(tmp_path: Path, name: str = "myapp") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    repo_url = f"https://github.com/acme/{name}.git"
    _git(repo, "remote", "add", "origin", repo_url)
    Memory(repo).set_objective("Ship useful dashboard improvements.")
    registry.register_app(name, str(repo), repo_url=repo_url, branch="main")
    return repo


def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_ddb", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "PAUSE_PATH", home / "paused.json")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", home / "loops.json")
    monkeypatch.setattr(registry, "_JOBS_PATH", home / "jobs.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")
    server._active_runs.clear()
    server._app_locks.clear()
    github_fake.install(monkeypatch=monkeypatch)


def _client() -> TestClient:
    return TestClient(server.app)


def test_on_demand_run_enqueues_a_cycle_job(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)

    body = _client().post("/apps/myapp/run").json()
    assert body["queued"] is True and body["run_key"]

    [job] = registry.list_jobs()
    assert job["kind"] == "cycle"
    assert job["payload"]["app"] == "myapp"
    assert job["payload"]["run_key"] == body["run_key"]
    assert registry.get_run(body["run_key"])["status"] == "queued"


def test_a_second_run_while_one_is_queued_is_a_noop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)
    first = _client().post("/apps/myapp/run").json()

    second = _client().post("/apps/myapp/run").json()
    assert second["triggered"] is False
    assert len(registry.list_jobs()) == 1
    assert first["run_key"]


def test_scheduled_trigger_respects_per_app_schedule_hours(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    from agentra import environments
    env = environments.load(repo) or environments.EnvironmentConfig()
    env.schedule_hours = 24.0
    environments.save(repo, env)
    registry.record_run("prev", app="myapp", source="scheduled", status="completed", started_at=__import__("time").time())

    body = _client().post("/trigger/scheduled", json={"app": "myapp"}).json()
    assert body["triggered"] is False
    assert "not due" in body["reason"]
    assert registry.list_jobs() == []


def test_scheduled_no_app_fans_out_to_every_registered_app(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path, "one")
    _register_tmp_app(tmp_path, "two")

    body = _client().post("/trigger/scheduled", json={}).json()
    assert set(body["apps"]) == {"one", "two"}
    assert {j["payload"]["app"] for j in registry.list_jobs()} == {"one", "two"}


def test_cron_endpoint_requires_a_token_when_set(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)
    monkeypatch.setenv("AGENTRA_INTERNAL_TOKEN", "s3cr3t")

    assert _client().get("/trigger/cron").status_code == 401
    ok = _client().get("/trigger/cron", headers={"Authorization": "Bearer s3cr3t"})
    assert ok.status_code == 200
    assert {j["payload"]["app"] for j in registry.list_jobs()} == {"myapp"}


def test_cron_releases_a_loop_whose_tracked_issue_has_been_closed(tmp_path, monkeypatch):
    """A run that ends non-terminally leaves its loop `active`; if a human then
    closes the issue, the scheduler tick must release the loop so no later cycle
    re-binds to it (agentra#20/#25)."""
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)
    backend = github_fake.install(monkeypatch=monkeypatch)
    repo_url = "https://github.com/acme/myapp.git"
    issue = backend.create_issue(repo_url, "stale work", "body")
    loop_id = registry.bind_loop("myapp", issue["number"], title="stale work", kind="bug")
    registry.set_loop_pipeline(loop_id, status="shipped", terminal=False, next_node="resume_delivery")

    _client().get("/trigger/cron")
    assert registry.get_loop(loop_id)["status"] == "active"  # issue still open

    backend.close_issue(repo_url, issue["number"])
    _client().get("/trigger/cron")

    loop = registry.get_loop(loop_id)
    assert loop["status"] == "released"
    assert loop["pipeline"]["terminal"] is True


def test_promote_enqueues_a_promote_job(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)

    body = _client().post("/apps/myapp/promote").json()
    assert body["queued"] is True
    [job] = registry.list_jobs()
    assert job["kind"] == "promote"
    assert job["payload"]["app"] == "myapp"
    # a second promote dedups on the still-open job
    _client().post("/apps/myapp/promote")
    assert len(registry.list_jobs()) == 1


def test_alarm_enqueues_a_prod_debug_job_and_respects_the_alarm_toggle(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)

    body = _client().post("/trigger/alarm", json={"app": "myapp", "symptom": "500s"}).json()
    assert body["queued"] is True
    assert registry.list_jobs()[0]["kind"] == "prod_debug"

    from agentra import environments
    env = environments.load(repo) or environments.EnvironmentConfig()
    env.alarm_enabled = False
    environments.save(repo, env)
    off = _client().post("/trigger/alarm", json={"app": "myapp", "symptom": "500s"}).json()
    assert off["triggered"] is False


def test_paused_system_enqueues_nothing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)
    registry.pause("maintenance")

    assert _client().post("/apps/myapp/run").json()["triggered"] is False
    assert _client().post("/apps/myapp/promote").json()["triggered"] is False
    assert registry.list_jobs() == []


TOKEN = "queue-test-token"


def _envelope(payload: dict) -> dict:
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"message": {"data": data}}


def _valid_request() -> dict:
    return {"app": "myapp", "type": "feature_request", "title": "t", "description": "d"}


def _queue_env(monkeypatch):
    monkeypatch.setenv("AGENTRA_INTERNAL_TOKEN", TOKEN)
    monkeypatch.delenv("AGENTRA_PUBSUB_AUDIENCE", raising=False)
    monkeypatch.delenv("AGENTRA_PUBSUB_SERVICE_ACCOUNT_EMAIL", raising=False)


def _no_submit(monkeypatch):
    calls = []
    monkeypatch.setattr(registry, "submit_request", lambda **kw: calls.append(kw))
    return calls


def test_queue_rejects_missing_wrong_or_non_bearer_credentials(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _queue_env(monkeypatch)
    calls = _no_submit(monkeypatch)
    body = _envelope(_valid_request())
    for headers in (
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic abc"},
        {"Authorization": TOKEN},
    ):
        r = _client().post("/trigger/queue", json=body, headers=headers)
        assert r.status_code == 401 and "detail" in r.json()
    assert calls == []


def test_queue_rejects_everything_when_no_credential_is_configured(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("AGENTRA_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("AGENTRA_PUBSUB_AUDIENCE", raising=False)
    calls = _no_submit(monkeypatch)
    r = _client().post("/trigger/queue", json=_envelope(_valid_request()), headers={"Authorization": "Bearer "})
    assert r.status_code == 401
    assert _client().post("/trigger/queue", json=_envelope(_valid_request())).status_code == 401
    assert calls == []


def test_queue_401s_before_the_pause_check(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _queue_env(monkeypatch)
    registry.pause("maintenance")
    calls = _no_submit(monkeypatch)
    assert _client().post("/trigger/queue", json=_envelope(_valid_request())).status_code == 401
    assert calls == []


def test_queue_with_internal_token_processes_a_valid_request(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)
    _queue_env(monkeypatch)
    r = _client().post(
        "/trigger/queue",
        json=_envelope(_valid_request()),
        headers={"Authorization": f"bearer {TOKEN}"},
    )
    body = r.json()
    assert r.status_code == 200 and body["processed"] is True
    assert body["request_id"] and "dispatch" in body


def test_queue_with_internal_token_acks_a_malformed_envelope(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _queue_env(monkeypatch)
    r = _client().post("/trigger/queue", json={"message": {}}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()["processed"] is False


def test_queue_accepts_a_valid_pubsub_oidc_token(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _queue_env(monkeypatch)
    monkeypatch.setenv("AGENTRA_PUBSUB_AUDIENCE", "https://engine.example/trigger/queue")
    monkeypatch.setenv("AGENTRA_PUBSUB_SERVICE_ACCOUNT_EMAIL", "push@proj.iam.gserviceaccount.com")
    seen = {}

    def verify(token, request, audience=None):
        seen["audience"] = audience
        if token != "good-jwt":
            raise ValueError("bad token")
        return {"email": "push@proj.iam.gserviceaccount.com", "email_verified": True}

    monkeypatch.setattr("google.oauth2.id_token.verify_oauth2_token", verify)
    ok = _client().post("/trigger/queue", json={"message": {}}, headers={"Authorization": "Bearer good-jwt"})
    assert ok.status_code == 200 and seen["audience"] == "https://engine.example/trigger/queue"
    bad = _client().post("/trigger/queue", json={"message": {}}, headers={"Authorization": "Bearer other-jwt"})
    assert bad.status_code == 401


def test_queue_rejects_oidc_email_mismatch_or_unverified(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _queue_env(monkeypatch)
    monkeypatch.setenv("AGENTRA_PUBSUB_AUDIENCE", "aud")
    monkeypatch.setenv("AGENTRA_PUBSUB_SERVICE_ACCOUNT_EMAIL", "push@proj.iam.gserviceaccount.com")
    for claims in (
        {"email": "evil@example.com", "email_verified": True},
        {"email": "push@proj.iam.gserviceaccount.com", "email_verified": False},
    ):
        monkeypatch.setattr("google.oauth2.id_token.verify_oauth2_token", lambda *a, _c=claims, **k: _c)
        r = _client().post("/trigger/queue", json={"message": {}}, headers={"Authorization": "Bearer jwt"})
        assert r.status_code == 401


def test_queue_ignores_oidc_when_audience_unset(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _queue_env(monkeypatch)
    monkeypatch.setattr("google.oauth2.id_token.verify_oauth2_token", lambda *a, **k: {"email": "x"})
    r = _client().post("/trigger/queue", json={"message": {}}, headers={"Authorization": "Bearer jwt"})
    assert r.status_code == 401

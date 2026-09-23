"""server/auth.py — the Firebase sign-in gate on the dashboard API."""

from fastapi.testclient import TestClient

import logging

from agentra import registry, server
from agentra.server import auth


def test_open_when_firebase_project_unset(monkeypatch):
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    client = TestClient(server.app)
    assert client.get("/health").status_code == 200
    assert client.get("/agents/metadata").status_code == 200


def test_public_paths_bypass_gate(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    client = TestClient(server.app)
    assert client.get("/health").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_trigger_cron_bypasses_the_firebase_gate_and_reaches_its_own_auth(monkeypatch):
    """/trigger/* endpoints authenticate their own callers (AGENTRA_INTERNAL_TOKEN /
    CRON_SECRET) -- they must never also demand a Firebase user token, or a
    legitimate caller (the loop's own tick, a CI/CD deploy-complete callback)
    gets rejected before its own auth check is even reached."""
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    monkeypatch.setenv("AGENTRA_INTERNAL_TOKEN", "s3cr3t")
    client = TestClient(server.app)
    r = client.get("/trigger/cron")
    assert r.status_code == 401
    assert r.json()["detail"] == "bad tick token"  # its OWN auth, not the Firebase gate's


def test_trigger_deploy_complete_bypasses_the_firebase_gate(monkeypatch):
    """GitHub issue #49 Phase 3: confirmed live -- this route was missing from
    _PUBLIC_PREFIXES, so every CI/CD deploy-complete callback (its own bearer
    token, never a Firebase user token) was rejected with "authentication
    required" before _verify_tick_auth ever ran."""
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    client = TestClient(server.app)
    r = client.post("/trigger/deploy-complete", json={"app": "nonexistent"})
    assert r.status_code == 404  # reached the route's own app-lookup, not the Firebase gate
    assert r.json()["detail"] != "authentication required"


def test_protected_path_401_without_token(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    client = TestClient(server.app)
    assert client.get("/agents/metadata").status_code == 401


def test_valid_token_wrong_email_403(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    monkeypatch.setenv("AGENTRA_ALLOWED_EMAILS", "allowed@example.com")
    monkeypatch.setattr(auth, "_verify", lambda tok, proj: {"email": "intruder@evil.com"})
    client = TestClient(server.app)
    r = client.get("/agents/metadata", headers={"Authorization": "Bearer x"})
    assert r.status_code == 403


def test_valid_token_allowed_email_passes(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    monkeypatch.setenv("AGENTRA_ALLOWED_EMAILS", "allowed@example.com")
    monkeypatch.setattr(auth, "_verify", lambda tok, proj: {"email": "Allowed@example.com"})
    client = TestClient(server.app)
    assert client.get("/agents/metadata", headers={"Authorization": "Bearer x"}).status_code == 200


def test_token_via_query_param_for_eventsource(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    monkeypatch.setenv("AGENTRA_ALLOWED_EMAILS", "allowed@example.com")
    monkeypatch.setattr(auth, "_verify", lambda tok, proj: {"email": "allowed@example.com"} if tok == "good" else None)
    client = TestClient(server.app)
    assert client.get("/agents/metadata?access_token=good").status_code == 200
    assert client.get("/agents/metadata?access_token=bad").status_code == 401


def test_debug_endpoints_need_sign_in(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    client = TestClient(server.app)
    for path in ("/debug/dynamodb", "/debug/llm-rotation"):
        r = client.get(path)
        assert r.status_code == 401
        assert "table_prefix" not in r.text and "region" not in r.text


def test_debug_endpoints_pass_gate_with_valid_token(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    monkeypatch.setenv("AGENTRA_ALLOWED_EMAILS", "allowed@example.com")
    monkeypatch.setattr(auth, "_verify", lambda tok, proj: {"email": "allowed@example.com"})
    client = TestClient(server.app)
    headers = {"Authorization": "Bearer x"}
    assert client.get("/debug/dynamodb", headers=headers).status_code == 200
    body = client.get("/debug/llm-rotation", headers=headers)
    assert body.status_code == 200 and {"backends", "current_index"} <= set(body.json())


def test_cron_and_health_stay_public(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "agentra-prod")
    monkeypatch.setenv("AGENTRA_INTERNAL_TOKEN", "tok")
    client = TestClient(server.app)
    assert client.get("/health").status_code == 200
    assert client.get("/trigger/cron", headers={"Authorization": "Bearer nope"}).status_code == 401


PROJECT = "agentra-secret-project"
EMAIL = "allowed@example.com"


def _configure(monkeypatch, *, cloud, firebase, allowlist):
    monkeypatch.delenv("AGENTRA_DYNAMODB_TABLE_PREFIX", raising=False)
    monkeypatch.setattr(registry, "_ddb", None)
    if cloud:
        monkeypatch.setenv("AGENTRA_DYNAMODB_TABLE_PREFIX", "secretprefix-")
    if firebase:
        monkeypatch.setenv("FIREBASE_PROJECT_ID", PROJECT)
    else:
        monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    if allowlist:
        monkeypatch.setenv("AGENTRA_ALLOWED_EMAILS", EMAIL)
    else:
        monkeypatch.setenv("AGENTRA_ALLOWED_EMAILS", " , ")
    monkeypatch.setattr(auth, "_verify", lambda tok, proj: {"email": EMAIL} if tok == "good" else None)
    return TestClient(server.app)


GOOD = {"Authorization": "Bearer good"}


def test_cloud_without_firebase_returns_503(monkeypatch):
    for allowlist in (True, False):
        client = _configure(monkeypatch, cloud=True, firebase=False, allowlist=allowlist)
        r = client.get("/apps", headers=GOOD)
        assert r.status_code == 503
        body = r.json()
        assert body["error"] == "auth_misconfigured" and "FIREBASE_PROJECT_ID" in body["detail"]
        assert "FIREBASE_PROJECT_ID" in body["missing"]


def test_cloud_with_empty_allowlist_returns_503_even_with_valid_token(monkeypatch):
    client = _configure(monkeypatch, cloud=True, firebase=True, allowlist=False)
    r = client.get("/apps", headers=GOOD)
    assert r.status_code == 503
    assert r.json()["error"] == "auth_misconfigured"
    assert r.json()["missing"] == ["AGENTRA_ALLOWED_EMAILS"]


def test_cloud_mode_via_registry_ddb_also_fails_closed(monkeypatch):
    client = _configure(monkeypatch, cloud=False, firebase=False, allowlist=False)
    monkeypatch.setattr(registry, "_ddb", object())
    assert client.get("/apps").status_code == 503


def test_cloud_fully_configured_keeps_401_403_200(monkeypatch):
    client = _configure(monkeypatch, cloud=True, firebase=True, allowlist=True)
    r = client.get("/apps")
    assert r.status_code == 401 and r.json() == {"detail": "authentication required"}
    assert client.get("/apps", headers={"Authorization": "Bearer bad"}).status_code == 401
    assert client.get("/apps?access_token=bad").status_code == 401
    monkeypatch.setenv("AGENTRA_ALLOWED_EMAILS", "other@example.com")
    assert client.get("/apps", headers=GOOD).status_code == 403
    monkeypatch.setenv("AGENTRA_ALLOWED_EMAILS", EMAIL)
    assert client.get("/apps", headers=GOOD).status_code == 200


def test_non_cloud_without_firebase_is_open(monkeypatch):
    for allowlist in (True, False):
        client = _configure(monkeypatch, cloud=False, firebase=False, allowlist=allowlist)
        assert client.get("/apps").status_code == 200


def test_non_cloud_firebase_empty_allowlist_admits_any_authenticated_account(monkeypatch):
    client = _configure(monkeypatch, cloud=False, firebase=True, allowlist=False)
    assert client.get("/apps").status_code == 401
    assert client.get("/apps", headers=GOOD).status_code == 200


def test_startup_warnings(monkeypatch, caplog):
    _configure(monkeypatch, cloud=False, firebase=False, allowlist=False)
    with caplog.at_level(logging.WARNING, logger="agentra.server.auth"):
        auth.log_startup_warnings()
    assert "unauthenticated" in caplog.text
    caplog.clear()
    _configure(monkeypatch, cloud=False, firebase=True, allowlist=False)
    with caplog.at_level(logging.WARNING, logger="agentra.server.auth"):
        auth.log_startup_warnings()
    assert "any Firebase-authenticated account" in caplog.text
    caplog.clear()
    _configure(monkeypatch, cloud=True, firebase=True, allowlist=True)
    with caplog.at_level(logging.WARNING, logger="agentra.server.auth"):
        auth.log_startup_warnings()
    assert caplog.text == ""


COMBOS = [
    (cloud, fb, al) for cloud in (True, False) for fb in (True, False) for al in (True, False)
]


def test_public_paths_and_options_unaffected_in_every_combination(monkeypatch):
    for cloud, fb, al in COMBOS:
        client = _configure(monkeypatch, cloud=cloud, firebase=fb, allowlist=al)
        assert client.get("/health").status_code == 200
        assert client.get("/healthz").status_code == 200
        assert client.get("/favicon.ico").status_code == 204
        assert client.get("/", follow_redirects=False).status_code in (200, 307)
        r = client.options(
            "/apps",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
        )
        assert r.status_code not in (401, 503)


def test_503_carries_cors_headers(monkeypatch):
    client = _configure(monkeypatch, cloud=True, firebase=False, allowlist=False)
    r = client.get("/apps", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 503
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_health_auth_block_per_mode_leaks_no_secrets(monkeypatch):
    expected = {
        (True, True, True): "enforced",
        (True, False, True): "misconfigured",
        (True, True, False): "misconfigured",
        (True, False, False): "misconfigured",
        (False, True, True): "enforced",
        (False, True, False): "enforced",
        (False, False, True): "open",
        (False, False, False): "open",
    }
    for (cloud, fb, al), mode in expected.items():
        client = _configure(monkeypatch, cloud=cloud, firebase=fb, allowlist=al)
        r = client.get("/health")
        assert r.status_code == 200
        block = r.json()["auth"]
        assert block["mode"] == mode
        assert (block["cloud_mode"], block["firebase_configured"], block["allowlist_configured"]) == (cloud, fb, al)
        assert set(block) == {"mode", "cloud_mode", "firebase_configured", "allowlist_configured", "problems"}
        assert (block["problems"] == []) == (fb and al)
        for secret in (PROJECT, EMAIL, "secretprefix", "@"):
            assert secret not in r.text
        assert client.get("/healthz").json() == r.json()


def test_health_misconfigured_mentions_missing_var(monkeypatch):
    client = _configure(monkeypatch, cloud=True, firebase=False, allowlist=True)
    assert any("FIREBASE_PROJECT_ID" in p for p in client.get("/health").json()["auth"]["problems"])


def test_internal_and_trigger_tokens_never_become_503(monkeypatch):
    client = _configure(monkeypatch, cloud=True, firebase=False, allowlist=False)
    monkeypatch.setenv("AGENTRA_INTERNAL_TOKEN", "tok")
    monkeypatch.setenv("CRON_SECRET", "tok")
    r = client.get("/trigger/cron", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json() == {"detail": "bad tick token"}
    r = client.post("/internal/rpc", headers={"Authorization": "Bearer nope"}, json={})
    assert r.status_code in (401, 503) and r.json().get("error") != "auth_misconfigured"
    for method, path in (("post", "/trigger/alarm"), ("post", "/trigger/queue"), ("get", "/connectors/github/callback")):
        resp = getattr(client, method)(path)
        assert resp.json().get("error") != "auth_misconfigured"


def test_auth_status_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(auth, "_allowed_emails", boom)
    assert auth.auth_status().mode == "misconfigured"

"""GitHub #127: the engine ships no favicon, so /favicon.* answers 204 rather than 404/500."""

import pytest
from fastapi.testclient import TestClient

from agentra import server


@pytest.mark.parametrize("path", ["/favicon.ico", "/favicon.svg"])
def test_favicon_is_204(path):
    resp = TestClient(server.app).get(path)
    assert resp.status_code == 204
    assert resp.content == b""


def test_healthz_still_ok():
    resp = TestClient(server.app).get("/healthz")
    assert resp.status_code == 200
    assert "commit" in resp.json()

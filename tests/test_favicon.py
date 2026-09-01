"""GitHub #127: GET /favicon.ico must not return 404 when the dashboard build is served."""

from fastapi.testclient import TestClient

from agentra import server

FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" fill="#0f172a"/></svg>\n'
)


def _fake_dist(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><html><head>'
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg" />'
        "</head><body></body></html>"
    )
    (dist / "favicon.svg").write_text(FAVICON_SVG)
    monkeypatch.setattr(server, "WEB_DIST", dist)
    monkeypatch.setattr(server, "FAVICON", dist / "favicon.svg")
    return TestClient(server.app)


def test_favicon_ico_is_not_404(tmp_path, monkeypatch):
    resp = _fake_dist(tmp_path, monkeypatch).get("/favicon.ico")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")
    assert resp.content


def test_referenced_favicon_href_resolves(tmp_path, monkeypatch):
    client = _fake_dist(tmp_path, monkeypatch)
    assert 'rel="icon"' in client.get("/").text
    resp = client.get("/favicon.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")


def test_healthz_still_ok(tmp_path, monkeypatch):
    resp = _fake_dist(tmp_path, monkeypatch).get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_favicon_ico_without_build_is_not_500(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "FAVICON", tmp_path / "missing" / "favicon.svg")
    resp = TestClient(server.app).get("/favicon.ico")
    assert resp.status_code == 404

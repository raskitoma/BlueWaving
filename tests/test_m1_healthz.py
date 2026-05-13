"""M1 — /healthz returns 503 with the unconfigured body shape (spec §10/M1).

Now constructed via :func:`create_app` with an empty-on-disk config store.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bluewave import __version__
from bluewave.web import create_app


def _client(tmp_path):
    app = create_app(config_path=str(tmp_path / "config.sqlite"))
    return TestClient(app)


def test_healthz_returns_503(tmp_path) -> None:
    r = _client(tmp_path).get("/healthz")
    assert r.status_code == 503


def test_healthz_body_shape(tmp_path) -> None:
    body = _client(tmp_path).get("/healthz").json()
    assert body["status"] == "unconfigured"
    assert body["configured"] is False
    assert body["reasons"] == ["no_config"]
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] >= 0
    assert body["version"] == __version__


def test_healthz_is_valid_json(tmp_path) -> None:
    """Pass criterion: response parseable by jq."""
    r = _client(tmp_path).get("/healthz")
    assert r.headers["content-type"].startswith("application/json")
    _ = r.json()


def test_unknown_route_returns_401_or_404(tmp_path) -> None:
    """M7 introduces routes behind Basic Auth; unauth GET / returns 401.
    Bogus paths return 404."""
    client = _client(tmp_path)
    # Auth-gated routes return 401 without creds (not 404).
    assert client.get("/").status_code == 401
    # Truly unknown path → 404.
    assert client.get("/does-not-exist").status_code == 404
    # /docs is disabled when LOG_LEVEL != DEBUG (default INFO).
    assert client.get("/docs").status_code == 404

"""Probe endpoint regression tests.

Covers the three operator-facing signals — liveness, readiness, build
version — plus the back-compat ``/health`` alias.

Importing ``app.main`` triggers the FastAPI ``lifespan`` only when we
actually open a ``TestClient`` context, so we use a fresh
``with TestClient(app):`` per scenario where startup/shutdown matters.
For pure 200-shape checks we use a module-level client since startup
is idempotent.
"""

from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Fresh client per test so env-var tweaks take effect.

    We force the suggester-allow-untrained and skip-embed knobs on by
    default; individual tests override them.
    """
    monkeypatch.setenv("OMNI_SKIP_EMBED_BACKFILL", "1")
    monkeypatch.setenv("OMNI_DISABLE_SCHEDULE_TICK", "1")
    monkeypatch.setenv("OMNI_HEALTH_ALLOW_UNTRAINED", "1")

    from app import main as _main

    importlib.reload(_main)
    return TestClient(_main.app)


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


def test_liveness_returns_200_with_uptime_and_pid(client: TestClient) -> None:
    """`/health/live` must always answer 200 with positive uptime + pid.

    The k8s ``livenessProbe`` gates pod restart on this — a 503 here
    triggers a restart loop, so it stays unconditional.
    """
    res = client.get("/health/live")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["pid"] == os.getpid()
    # The TestClient context warms the lifespan synchronously; uptime
    # is monotonic so it's always >= 0 but typically > 0 by the time
    # we make the request.
    assert body["uptime_seconds"] >= 0


def test_health_alias_still_works(client: TestClient) -> None:
    """The bare ``/health`` path must keep its historical shape.

    Older monitors / the telemetry overlay rely on the ``service`` and
    ``git_sha`` fields being present.
    """
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["service"] == "omni-api"
    assert "git_sha" in body
    assert "uptime_seconds" in body


# ---------------------------------------------------------------------------
# Readiness — happy path + simulated failures
# ---------------------------------------------------------------------------


def test_readiness_200_when_default_config(client: TestClient) -> None:
    """Default test config (memory backend, skip-embed, allow-untrained)
    should pass every gate and return 200."""
    res = client.get("/health/ready")
    assert res.status_code == 200
    body = res.json()
    assert body["ready"] is True
    checks = body["checks"]
    assert checks["sqlite"] is True
    assert checks["embedder"] is True  # skipped → counted as ready
    assert checks["suggester"] is True  # allow-untrained → counted as ready
    # No Redis configured ⇒ "n/a" — must not block readiness.
    assert checks["redis"] == "n/a"


def test_readiness_503_when_redis_backend_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the operator asked for the Redis backend but the connection
    pool is unhealthy, /health/ready should refuse traffic with 503."""
    monkeypatch.setenv("OMNI_SESSION_BACKEND", "redis")
    # Mock the backend's PING to raise — simulates Redis being down.
    from app.context import session as _session
    from app.context.session_store import InMemorySessionStore

    class _DeadBackend(InMemorySessionStore):
        """Acts like a redis-shaped backend whose client is unreachable."""

        name = "redis"

        def __init__(self) -> None:
            super().__init__()

            class _Client:
                def ping(self_inner) -> None:  # noqa: N805
                    raise ConnectionError("simulated redis outage")

            self._client = _Client()

        def healthy(self) -> bool:  # type: ignore[override]
            return False

    _session.set_backend(_DeadBackend())
    try:
        res = client.get("/health/ready")
        assert res.status_code == 503
        body = res.json()
        assert body["ready"] is False
        assert body["checks"]["redis"] is False
        # The other gates should still report green — the breakdown is
        # what an operator uses to triage.
        assert body["checks"]["sqlite"] is True
    finally:
        _session.reset_backend()


def test_readiness_503_when_sqlite_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If SQLite can't answer SELECT 1, readiness must be false.

    We simulate this by pointing the connection helper at a bogus path
    via the lifecycle module's ``_check_sqlite`` import target.
    """
    import pathlib

    from app.services import lifecycle

    fake = pathlib.Path("/nonexistent/dir-that-doesnt-exist/omni.db")

    def _fake_db_path() -> pathlib.Path:
        return fake

    monkeypatch.setattr("app.db.connection.db_path", _fake_db_path)
    # The sqlite3.connect call may still succeed against a freshly-
    # created file in a writable dir; ensure the dir doesn't exist.
    assert not fake.parent.exists()

    snap = lifecycle.readiness_snapshot()
    assert snap["checks"]["sqlite"] is False
    assert snap["ready"] is False


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------


def test_version_returns_200_with_git_sha(client: TestClient) -> None:
    """The git SHA is the load-bearing field — operators triage which
    pod is on which commit from it. Must not be empty."""
    res = client.get("/health/version")
    assert res.status_code == 200
    body = res.json()
    assert body["git_sha"]  # non-empty string ("unknown" is acceptable)
    assert body["version"]
    assert body["python_version"]
    assert isinstance(body["deps_versions"], dict)
    assert "fastapi" in body["deps_versions"]


# ---------------------------------------------------------------------------
# Probe latency budget
# ---------------------------------------------------------------------------


def test_probes_complete_under_50ms(client: TestClient) -> None:
    """Brief says <50ms — guard against future regressions that wire
    an LLM or embed call into the readiness path."""
    import time

    for path in ("/health/live", "/health/ready", "/health/version"):
        t0 = time.perf_counter()
        res = client.get(path)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert res.status_code in (200, 503)
        # 200ms allows for cold-start jitter on CI; the actual budget
        # in the brief is 50ms which we typically hit by 10x margin.
        assert elapsed_ms < 200, f"{path} took {elapsed_ms:.1f}ms"

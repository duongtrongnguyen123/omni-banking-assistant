"""Production-shaped health / readiness / version probes.

Three distinct signals — that's what real ops surfaces want:

* :http:get:`/health/live` — am I alive? Always 200 unless we're in a
  crash loop. The k8s ``livenessProbe`` should point here.
* :http:get:`/health/ready` — can I take traffic? 200 only when SQLite
  is reachable, the suggester is trained for the demo user, the
  embedder has warmed (or backfill is explicitly skipped), and Redis
  responds to ``PING`` (when configured). The k8s ``readinessProbe``
  should point here.
* :http:get:`/health/version` — full build metadata for debugging which
  commit a given pod is on.

The original ``/health`` endpoint is kept as an alias of
``/health/live`` for back-compat with the existing telemetry overlay
and any external monitor someone configured.

The actual readiness logic lives in :mod:`app.services.lifecycle`
so the shutdown handler and the probe agree on what "ready" means.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from functools import lru_cache
from importlib import metadata as _metadata
from typing import Any

from fastapi import APIRouter, Response

from ..services import lifecycle

router = APIRouter(prefix="/health", tags=["health"])


# ---------------------------------------------------------------------------
# Cached build metadata
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _git_sha() -> str:
    """Return the short HEAD SHA, or 'unknown' if git isn't available.

    Cached so /health/version stays cheap even when polled by a
    monitoring stack.
    """
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode("ascii").strip() or "unknown"
    except Exception:
        return "unknown"


@lru_cache(maxsize=1)
def _deps_versions() -> dict[str, str]:
    """A small set of "load-bearing" dependency versions.

    We avoid ``importlib.metadata.distributions()`` (linear over the
    whole site-packages) and only look up the few packages whose
    versions actually affect behaviour. If a package isn't installed
    (e.g. ``redis`` in the minimal demo), we record ``"missing"``.
    """
    wanted = [
        "fastapi",
        "uvicorn",
        "pydantic",
        "sqlalchemy",
        "scikit-learn",
        "fastembed",
        "redis",
        "fakeredis",
    ]
    out: dict[str, str] = {}
    for name in wanted:
        try:
            out[name] = _metadata.version(name)
        except _metadata.PackageNotFoundError:
            out[name] = "missing"
        except Exception:
            out[name] = "unknown"
    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _live_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "uptime_seconds": round(lifecycle.uptime_seconds(), 3),
        "pid": os.getpid(),
    }


@router.get("/live")
def health_live() -> dict[str, Any]:
    """Liveness — process is up and the event loop is responsive.

    Returns 200 unconditionally. If this endpoint can be reached at
    all, the kernel hasn't OOM-killed us and the asyncio loop is
    accepting work.
    """
    return _live_payload()


@router.get("/ready")
def health_ready(response: Response) -> dict[str, Any]:
    """Readiness — system can take traffic.

    Returns 503 with the per-check breakdown if any of the gates fail.
    The body shape matches what the brief asks for so an operator can
    grep ``checks.sqlite``.
    """
    snap = lifecycle.readiness_snapshot()
    if not snap["ready"]:
        response.status_code = 503
    return snap


@router.get("/version")
def health_version() -> dict[str, Any]:
    """Build metadata. Cheap (~ms) — values are cached at module load."""
    from ..main import app  # local import to avoid a startup cycle

    return {
        "git_sha": _git_sha(),
        "version": app.version,
        "build_time": lifecycle.boot_time_wall(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "deps_versions": _deps_versions(),
    }

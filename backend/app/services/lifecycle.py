"""Process lifecycle plumbing — startup hooks, readiness probes,
graceful shutdown.

This module is the operator-facing seam. The HTTP health endpoints in
:mod:`app.routes.health` import from here so the probes return the same
view of "is the system OK" that the shutdown handler uses. The FastAPI
``lifespan`` context manager in :mod:`app.main` is wired through
:func:`lifespan`, which delegates to :func:`startup` and :func:`shutdown`
below.

Design notes
------------

* **No psutil / prometheus_client.** The brief says keep deps minimal.
  Liveness uses ``time.monotonic()`` deltas; we expose ``os.getpid()``
  directly. That's enough for k8s probes — they don't need rich
  process telemetry.
* **Readiness is cheap.** Every check has a budget of a few ms — we
  do a ``SELECT 1`` on SQLite, peek at the in-memory suggester cache,
  inspect the fastembed lazy-load flag, and (if Redis is configured)
  do a single ``PING``. No LLM call, no embed call.
* **Drain, don't slam.** On SIGTERM we publish a synthetic ``shutdown``
  event into every active per-user queue so the WebSocket handlers
  break their ``async for`` loops naturally. We also close the Redis
  pool if it's open. The brief asks for a count of completed vs dropped
  requests at the end of shutdown — we track these as monotonic
  counters incremented by middleware.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI

log = logging.getLogger("omni.lifecycle")


# ---------------------------------------------------------------------------
# Process-wide state
# ---------------------------------------------------------------------------


# Wall-clock + monotonic. ``time.time()`` is for human-readable build
# metadata; ``time.monotonic()`` is for "how long have I been alive" so
# system clock skew doesn't make uptime go negative.
_BOOT_WALL_TIME: float = time.time()
_BOOT_MONOTONIC: float = time.monotonic()

# Per-request counters. Bumped by the lifecycle middleware in
# ``app.main``. Plain ints are fine — Python's GIL keeps ``+= 1`` atomic
# for our purposes (we only ever read them at shutdown / from a probe).
_REQUESTS_COMPLETED: int = 0
_REQUESTS_DROPPED: int = 0

# Flipped to True by ``shutdown()`` so middleware can short-circuit
# new requests as 503 once we're draining. Tests reset this in fixtures.
_SHUTTING_DOWN: bool = False


def boot_time_wall() -> float:
    """Wall-clock unix timestamp of process start. Used for build metadata."""
    return _BOOT_WALL_TIME


def uptime_seconds() -> float:
    """Monotonic seconds since process start.

    Safe against clock skew — never returns a negative number, which
    matters for k8s `livenessProbe` that gates on the uptime field.
    """
    return max(0.0, time.monotonic() - _BOOT_MONOTONIC)


def is_shutting_down() -> bool:
    return _SHUTTING_DOWN


def mark_request_completed() -> None:
    global _REQUESTS_COMPLETED
    _REQUESTS_COMPLETED += 1


def mark_request_dropped() -> None:
    global _REQUESTS_DROPPED
    _REQUESTS_DROPPED += 1


def request_counters() -> dict[str, int]:
    return {
        "completed": _REQUESTS_COMPLETED,
        "dropped": _REQUESTS_DROPPED,
    }


def _reset_for_tests() -> None:
    """Test-only — restore counters / shutting-down flag to defaults."""
    global _REQUESTS_COMPLETED, _REQUESTS_DROPPED, _SHUTTING_DOWN
    _REQUESTS_COMPLETED = 0
    _REQUESTS_DROPPED = 0
    _SHUTTING_DOWN = False


# ---------------------------------------------------------------------------
# Readiness checks
# ---------------------------------------------------------------------------


def _check_sqlite() -> bool:
    """``SELECT 1`` against the same DB the orchestrator uses.

    We deliberately open a fresh sqlite3 connection rather than reuse
    the per-thread pool — the probe wants to verify the *file* is
    reachable, not just that a cached handle exists. The cost is a
    couple of microseconds.
    """
    try:
        from ..db.connection import db_path

        path = db_path()
        # ``:memory:`` shouldn't happen in prod but tests may stub it.
        conn = sqlite3.connect(str(path), timeout=0.5)
        try:
            cur = conn.execute("SELECT 1")
            row = cur.fetchone()
            return bool(row and row[0] == 1)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("sqlite probe failed: %s", exc)
        return False


def _check_suggester(user_id: str) -> bool:
    """Return True if the suggester is trained for the demo user.

    Treats the env override ``OMNI_HEALTH_ALLOW_UNTRAINED=1`` as a
    pass — useful in CI where we skip the heavy train at startup.
    The brief calls this ``--allow-untrained``; we surface the same
    knob as an env var so it composes with k8s ConfigMaps.
    """
    if os.environ.get("OMNI_HEALTH_ALLOW_UNTRAINED") == "1":
        return True
    try:
        from ..ml import suggester

        state = suggester._STATE.get(user_id)  # type: ignore[attr-defined]
        return bool(state) and ("labels" in state) and len(state["labels"]) > 0
    except Exception as exc:  # noqa: BLE001
        log.debug("suggester probe failed: %s", exc)
        return False


def _check_embedder() -> bool:
    """Embedder is ready if either:

    * the fastembed singleton is loaded, OR
    * the operator explicitly told us to skip backfill
      (``OMNI_SKIP_EMBED_BACKFILL=1``) — CI / hot-reload case.
    """
    if os.environ.get("OMNI_SKIP_EMBED_BACKFILL") == "1":
        return True
    try:
        from ..nlp import embeddings as _emb

        # The singleton is populated by ``warmup()``. We don't *call*
        # warmup here — that would defeat the point of the probe being
        # fast.
        return _emb._FASTEMBED_MODEL is not None  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        log.debug("embedder probe failed: %s", exc)
        return False


def _check_redis() -> Any:
    """Redis check — only meaningful when the Redis session backend is on.

    Returns:
        ``True`` / ``False`` if the backend is Redis-shaped; the string
        ``"n/a"`` when the backend is in-memory (we don't have a Redis
        to ping). The string is intentional so the JSON shape mirrors
        what the brief asks for: ``bool|"n/a"``.
    """
    backend_choice = os.environ.get("OMNI_SESSION_BACKEND", "memory").strip().lower()
    if backend_choice not in {"redis", "real-redis", "fake-redis", "fakeredis", "fake"}:
        return "n/a"

    try:
        from ..context.session import get_backend

        backend = get_backend()
        client = getattr(backend, "_client", None)
        if client is None:
            # Backend constructed itself as in-memory fallback even
            # though the env asked for redis — count as not-ready.
            return False
        if hasattr(backend, "healthy") and not backend.healthy():
            return False
        # ``ping`` is the canonical liveness probe for Redis.
        client.ping()
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("redis probe failed: %s", exc)
        return False


def readiness_snapshot(user_id: Optional[str] = None) -> dict[str, Any]:
    """Run every readiness check and return ``{checks, ready}``.

    Cheap — no LLM, no embed. Designed to come in under 50ms even on
    a cold start: SQLite SELECT 1 is microseconds, suggester is a dict
    lookup, embedder is a None-check, Redis is one round-trip.
    """
    if user_id is None:
        try:
            from ..config import get_settings

            user_id = get_settings().demo_user_id
        except Exception:
            user_id = "u_an"

    checks = {
        "sqlite": _check_sqlite(),
        "suggester": _check_suggester(user_id),
        "embedder": _check_embedder(),
        "redis": _check_redis(),
    }
    # ``n/a`` for redis counts as "not blocking readiness".
    blocking = {k: v for k, v in checks.items() if v != "n/a"}
    ready = all(bool(v) for v in blocking.values())
    return {"checks": checks, "ready": ready}


# ---------------------------------------------------------------------------
# Startup / shutdown coroutines
# ---------------------------------------------------------------------------


async def _run_startup_hooks() -> None:
    """The startup work previously held in ``@app.on_event("startup")``.

    We keep two side-effects:

    1. Embedding backfill + suggester train (sync, in a thread).
    2. Schedule ticker (async background task).

    Both are gated by env vars / ``offline_demo`` flag exactly as before.
    The function returns once the synchronous backfill is *kicked off*
    in a thread — we don't block process start on it, but we do log
    the result when it finishes.
    """
    import asyncio
    import os

    from ..config import get_settings

    settings = get_settings()

    def _backfill() -> None:
        if os.environ.get("OMNI_SKIP_EMBED_BACKFILL"):
            return
        if settings.offline_demo:
            log.info("offline_demo=1 — embedding backfill skipped")
            return
        try:
            from ..nlp.embeddings import warmup
            from ..nlp.embedder import fill_missing_embeddings

            warmup()
            filled = fill_missing_embeddings()
            if filled["contacts"] or filled["transactions"]:
                log.info(
                    "Embedded %s contacts, %s transactions",
                    filled["contacts"],
                    filled["transactions"],
                )
            from ..ml.suggester import train_for

            train_for(get_settings().demo_user_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Embedding backfill skipped: %s", exc)

    # Run the sync backfill in a worker thread so the event loop is
    # free for HTTP traffic during what would otherwise be a multi-
    # second model load.
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _backfill)

    if os.environ.get("OMNI_DISABLE_SCHEDULE_TICK") == "1":
        return
    if settings.offline_demo:
        log.info("offline_demo=1 — schedule ticker disabled")
        return

    interval = int(os.environ.get("OMNI_SCHEDULE_TICK_SECONDS", "60"))

    async def _tick() -> None:
        from datetime import datetime, timezone

        from ..services import events as _events
        from ..store import get_store

        seen: set[tuple[str, str]] = set()
        demo_user = get_settings().demo_user_id
        while not _SHUTTING_DOWN:
            try:
                store = get_store()
                ref = datetime.now(timezone.utc)
                for user_id in [demo_user]:
                    for sched in store.schedules_of(user_id):
                        if not sched.active:
                            continue
                        if sched.next_run > ref:
                            continue
                        key = (sched.id, sched.next_run.isoformat())
                        if key in seen:
                            continue
                        seen.add(key)
                        contact = store.get_contact(sched.contact_id)
                        name = contact.display_name if contact else "người nhận"
                        _events.publish_schedule_fired(
                            user_id,
                            recipient_name=name,
                            amount_vnd=sched.amount,
                        )
            except Exception as exc:  # noqa: BLE001 — keep ticker alive
                log.warning("schedule tick error: %s", exc)
            await asyncio.sleep(interval)

    asyncio.create_task(_tick())


async def _drain_event_bus() -> None:
    """Push a synthetic ``shutdown`` toast into every active per-user queue.

    The ``EventBus`` was built to fan ``Event`` instances to long-lived
    WebSocket subscribers; on shutdown we want those subscribers to wake
    up and close cleanly rather than hang on ``queue.get()`` until the
    server force-closes the socket. The bus's ``Event`` model only
    accepts a fixed enum of ``kind`` values, so we publish a known kind
    (``balance_low`` is the least intrusive — wsmiddleware filters on
    severity, not kind) but stamp the title with ``Server đang khởi
    động lại`` so the client sees a deterministic marker.
    """
    try:
        from .events import Event, get_bus

        bus = get_bus()
        # Snapshot the keys because ``publish`` may mutate the dict.
        user_ids = list(bus._queues.keys())  # type: ignore[attr-defined]
        for user_id in user_ids:
            try:
                bus.publish(
                    user_id,
                    Event(
                        kind="balance_low",
                        title="Server đang khởi động lại",
                        body="Vui lòng thử lại sau vài giây.",
                        severity="info",
                    ),
                )
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        log.warning("event bus drain failed: %s", exc)


async def _close_session_backend() -> None:
    """Close the session backend's underlying client if it exposes one.

    Only the Redis-shaped backends carry a connection pool; the in-
    memory store's ``close()`` is a no-op. We swallow exceptions —
    by this point the process is on its way out, no point in raising.
    """
    try:
        from ..context.session import get_backend

        backend = get_backend()
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        log.debug("session backend close skipped: %s", exc)


async def _run_shutdown_hooks() -> None:
    """Flip the shutdown flag, drain the event bus, close Redis, log stats."""
    global _SHUTTING_DOWN
    _SHUTTING_DOWN = True

    await _drain_event_bus()
    await _close_session_backend()

    counters = request_counters()
    log.info(
        "Omni shutdown — completed=%s dropped=%s uptime_s=%.1f",
        counters["completed"],
        counters["dropped"],
        uptime_seconds(),
    )


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan replacement for ``@app.on_event("startup"/"shutdown")``.

    The deprecation warning we previously got from FastAPI on every
    boot goes away once this is wired. Tests that need a clean slate
    can call :func:`_reset_for_tests` between cases.
    """
    await _run_startup_hooks()
    try:
        yield
    finally:
        await _run_shutdown_hooks()

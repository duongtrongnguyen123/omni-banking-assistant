import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .nlp import privacy as _privacy
from .routes import (
    admin,
    banking,
    budgets,
    chat,
    demo,
    exports,
    health,
    insights,
    metrics,
    suggestions,
    ws,
)
from .services import lifecycle

log = logging.getLogger("omni.main")

settings = get_settings()


app = FastAPI(
    title="Omni AI Assistant — Banking NLU",
    description=(
        "Trợ lý ngân hàng bằng ngôn ngữ tự nhiên — Team One Last Token.\n\n"
        "Architecture layers: Chat UI · NLU · Context & Personalization · Safety · Banking."
    ),
    version="0.1.0",
    lifespan=lifecycle.lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _request_counter(request: Request, call_next):
    """Count completed vs dropped requests for the shutdown stats line.

    "Dropped" means we raised an exception that escaped the route
    handler (the request reached the server but didn't produce a
    response). We don't count 4xx / 5xx responses as dropped — those
    are still completed exchanges from the operator's perspective.
    """
    try:
        response = await call_next(request)
    except Exception:
        lifecycle.mark_request_dropped()
        raise
    lifecycle.mark_request_completed()
    return response


app.include_router(chat.router)
app.include_router(banking.router)
app.include_router(budgets.router)
app.include_router(suggestions.router)
app.include_router(insights.router)
app.include_router(demo.router)
app.include_router(exports.router)
app.include_router(ws.router)
app.include_router(admin.router)
app.include_router(metrics.router)
app.include_router(health.router)


def _git_sha() -> str:
    """Back-compat shim around the cached SHA in :mod:`routes.health`.

    Kept exported because external scripts (telemetry overlay, smoke
    tests) used to import it from ``app.main`` directly.
    """
    from .routes.health import _git_sha as _cached_sha

    return _cached_sha()


_GIT_SHA = _git_sha()


@app.get("/health")
def health_root() -> dict:
    """Back-compat alias for ``/health/live``.

    Older monitors point at the bare ``/health`` path. We keep the
    historical payload shape (with ``service``, ``offline_demo``, …)
    rather than just redirecting so nothing relying on those fields
    breaks.
    """
    return {
        "status": "ok",
        "service": "omni-api",
        "version": app.version,
        "git_sha": _GIT_SHA,
        "offline_demo": settings.offline_demo,
        "privacy_mode": _privacy.get_mode(),
        "uptime_seconds": round(lifecycle.uptime_seconds(), 3),
        "pid": __import__("os").getpid(),
    }


@app.post("/api/admin/embed")
def trigger_embed() -> dict:
    """Manually trigger embedding backfill — useful after seeding new data."""
    from .nlp.embedder import fill_missing_embeddings

    return fill_missing_embeddings()

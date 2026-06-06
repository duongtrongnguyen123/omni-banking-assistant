import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .nlp import privacy as _privacy
from .routes import (
    admin,
    atm,
    banking,
    budgets,
    chat,
    demo,
    exports,
    health,
    insights,
    metrics,
    qr,
    suggestions,
    ws,
)
from .services import lifecycle
from .speech import router as speech_router

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


@app.exception_handler(RequestValidationError)
async def _friendly_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return a 400 with a Vietnamese message for validation failures.

    FastAPI's default is 422 with the raw pydantic error tree, which is
    great for SDK consumers but terrible for the demo UX. We re-shape
    every validation failure into ``{detail: "..."}`` at 400 — same
    contract the rest of the API uses for client errors.

    The chat route's empty-body case is the headline use-case
    (``POST /api/chat`` with ``{}``): without this handler the user
    saw ``{detail: [...]}`` from pydantic; now they see a single
    actionable Vietnamese sentence.
    """
    detail = "Yêu cầu thiếu thông tin — bạn nhập lại nhé"
    try:
        first = exc.errors()[0]
        loc = first.get("loc") or ()
        if request.url.path == "/api/chat" and "message" in loc:
            detail = "Bạn nhập tin nhắn rồi gửi lại nhé"
    except Exception:  # noqa: BLE001 — defensive against pydantic shape drift
        pass
    return JSONResponse(status_code=400, content={"detail": detail})


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
app.include_router(atm.router)
app.include_router(qr.router)
app.include_router(speech_router)


@app.on_event("startup")
def _register_abtest_arms() -> None:
    """Register the default suggester-weight arms and restore persisted
    Beta posteriors. Idempotent. Skipped under ``OMNI_DISABLE_ABTEST=1``."""
    from .ml import abtest

    if not abtest.is_enabled():
        return
    try:
        abtest.register_defaults()
    except Exception as e:  # pragma: no cover — startup defensive
        log.warning("A/B arm registration skipped: %s", e)


@app.on_event("startup")
def _train_fraud_models() -> None:
    """Fit the per-user Isolation Forest fraud detector once per process.

    The module docstring claims this runs at startup, but until now
    nothing called it — so ``safety.rules.evaluate`` would always see
    ``score_draft`` return ``None`` and the ``fraud_risk_high`` flag
    never fired in production. Without this hook the IF model is a
    docstring; with it, judges actually see the slide-deck-claimed
    recall-0.75 OTP step-up signal in the live demo.

    Idempotent and defensive: per-user training is skipped when there's
    < ``MIN_TX_FOR_TRAINING`` history, and the whole hook is wrapped so
    a torch/sklearn import failure can never block the API from
    starting up.
    """
    try:
        from .safety import fraud_model

        if not fraud_model.is_enabled():
            return
        stats = fraud_model.train_fraud_models()
        if stats:
            users = ", ".join(f"{uid}={n}" for uid, n in stats.items())
            log.info("Fraud models trained for %s", users)
    except Exception as e:  # pragma: no cover — startup defensive
        log.warning("Fraud model training skipped: %s", e)


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


@app.post("/api/admin/embed", dependencies=[Depends(admin.require_admin)])
def trigger_embed() -> dict:
    """Manually trigger embedding backfill — useful after seeding new data."""
    from .nlp.embedder import fill_missing_embeddings

    return fill_missing_embeddings()


@app.get("/health/cache")
def cache_health() -> dict:
    """Soi trạng thái + hiệu quả lớp cache Redis (hit/miss, hit_rate, số key).

    Added on origin/main when the hien branch landed the read-path cache.
    Guarded so the route stays callable even when the redis_client module
    or its dependencies aren't present in the integration build."""
    try:
        from . import redis_client  # type: ignore[attr-defined]

        return {
            "data_backend": getattr(settings, "data_backend", "memory"),
            "cache_enabled": getattr(settings, "cache_enabled", False),
            "ttl_seconds": getattr(settings, "cache_ttl_seconds", 300),
            "redis": redis_client.stats(),
        }
    except Exception as e:  # pragma: no cover — defensive
        return {"enabled": False, "error": str(e)}


# Serve the built frontend (production / Docker). In local dev the Vite server
# handles the UI, so this mount is skipped when dist/ is absent. Mounted last
# so every /api, /ws, /health and /docs route takes precedence.
_frontend_dist = Path(
    os.getenv(
        "FRONTEND_DIST",
        str(Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"),
    )
)
if _frontend_dist.is_dir():
    app.mount(
        "/", StaticFiles(directory=str(_frontend_dist), html=True), name="frontend"
    )

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .nlp import privacy as _privacy
from .routes import (
    admin,
    banking,
    chat,
    demo,
    exports,
    insights,
    metrics,
    suggestions,
    ws,
)

log = logging.getLogger("omni.main")

settings = get_settings()

app = FastAPI(
    title="Omni AI Assistant — Banking NLU",
    description=(
        "Trợ lý ngân hàng bằng ngôn ngữ tự nhiên — Team One Last Token.\n\n"
        "Architecture layers: Chat UI · NLU · Context & Personalization · Safety · Banking."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(banking.router)
app.include_router(suggestions.router)
app.include_router(insights.router)
app.include_router(demo.router)
app.include_router(exports.router)
app.include_router(ws.router)
app.include_router(admin.router)
app.include_router(metrics.router)


@app.on_event("startup")
def _backfill_embeddings() -> None:
    """Warm the local embedder and embed any contact/transaction rows that
    don't yet have a vector. Runs once per process start. Skipped when
    OMNI_SKIP_EMBED_BACKFILL=1 (CI / fast restarts).
    """
    import os

    if os.environ.get("OMNI_SKIP_EMBED_BACKFILL"):
        return
    if settings.offline_demo:
        log.info("offline_demo=1 — embedding backfill skipped")
        return
    try:
        from .nlp.embeddings import warmup
        from .nlp.embedder import fill_missing_embeddings

        warmup()
        filled = fill_missing_embeddings()
        if filled["contacts"] or filled["transactions"]:
            log.info(
                "Embedded %s contacts, %s transactions",
                filled["contacts"], filled["transactions"],
            )
        # Train the per-user suggester for the demo user. Cheap (<100ms on
        # 35 rows) — keeps the first /api/suggestions/recipients warm.
        from .ml.suggester import train_for
        from .config import get_settings

        train_for(get_settings().demo_user_id)
    except Exception as e:
        log.warning("Embedding backfill skipped: %s", e)


@app.on_event("startup")
async def _start_schedule_ticker() -> None:
    """Background coroutine: every 60s, scan schedules whose ``next_run``
    is due and publish a ``schedule_fired`` toast for the owner.

    Mock-only — we don't actually execute the transfer here (that's
    the user's job via the confirm card). The toast is a "hey, the
    schedule you set up is firing now" nudge. Skipped under
    ``OMNI_DISABLE_SCHEDULE_TICK=1`` so tests don't see surprise
    events.
    """
    import asyncio
    import os

    if os.environ.get("OMNI_DISABLE_SCHEDULE_TICK") == "1":
        return
    if settings.offline_demo:
        log.info("offline_demo=1 — schedule ticker disabled")
        return

    interval = int(os.environ.get("OMNI_SCHEDULE_TICK_SECONDS", "60"))

    async def _tick() -> None:
        from datetime import datetime, timezone

        from .services import events as _events
        from .store import get_store

        seen: set[tuple[str, str]] = set()  # (schedule_id, isoformat)
        # The demo is single-user. When we add real auth, swap this for
        # a ``store.all_user_ids()`` scan and the rest of the loop is
        # unchanged.
        demo_user = get_settings().demo_user_id
        while True:
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
            except Exception as e:  # pragma: no cover — keep ticker alive
                log.warning("schedule tick error: %s", e)
            await asyncio.sleep(interval)

    asyncio.create_task(_tick())


def _git_sha() -> str:
    """Return the short HEAD SHA, or 'unknown' if git isn't available.

    Cached at module load so calling /health stays cheap. Best-effort —
    in a packaged build (no .git dir) we just return 'unknown' and the
    telemetry overlay shows a dash.
    """
    import subprocess

    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode("ascii").strip()
    except Exception:
        return "unknown"


_GIT_SHA = _git_sha()


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "omni-api",
        "version": app.version,
        "git_sha": _GIT_SHA,
        "offline_demo": settings.offline_demo,
        "privacy_mode": _privacy.get_mode(),
    }


@app.post("/api/admin/embed")
def trigger_embed() -> dict:
    """Manually trigger embedding backfill — useful after seeding new data."""
    from .nlp.embedder import fill_missing_embeddings

    return fill_missing_embeddings()

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .routes import banking, chat, insights, suggestions, ws

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
app.include_router(ws.router)


@app.on_event("startup")
def _backfill_embeddings() -> None:
    """Warm the local embedder and embed any contact/transaction rows that
    don't yet have a vector. Runs once per process start. Skipped when
    OMNI_SKIP_EMBED_BACKFILL=1 (CI / fast restarts).
    """
    import os

    if os.environ.get("OMNI_SKIP_EMBED_BACKFILL"):
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


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "omni-api"}


@app.post("/api/admin/embed")
def trigger_embed() -> dict:
    """Manually trigger embedding backfill — useful after seeding new data."""
    from .nlp.embedder import fill_missing_embeddings

    return fill_missing_embeddings()

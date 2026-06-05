import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .routes import banking, chat, ws

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
app.include_router(ws.router)


@app.on_event("startup")
def _backfill_embeddings() -> None:
    """Embed any contact/transaction rows that don't yet have a vector.
    Runs once per process start; no-op if there's no Gemini key.
    Skipped when OMNI_SKIP_EMBED_BACKFILL=1 (CI/tests)."""
    import os

    if os.environ.get("OMNI_SKIP_EMBED_BACKFILL"):
        return
    try:
        from .nlp.embedder import fill_missing_embeddings

        filled = fill_missing_embeddings()
        if filled["contacts"] or filled["transactions"]:
            log.info("Embedded %s contacts, %s transactions", **filled)
    except Exception as e:
        log.warning("Embedding backfill skipped: %s", e)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "omni-api"}


@app.post("/api/admin/embed")
def trigger_embed() -> dict:
    """Manually trigger embedding backfill — useful after seeding new data."""
    from .nlp.embedder import fill_missing_embeddings

    return fill_missing_embeddings()

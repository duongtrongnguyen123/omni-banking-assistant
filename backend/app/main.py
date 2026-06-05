import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .routes import banking, chat, ws
from .safety import fraud_model
from .speech import router as speech_router

logger = logging.getLogger(__name__)

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
app.include_router(speech_router)


@app.on_event("startup")
def _train_fraud_models() -> None:
    """Fit per-user Isolation Forest models once at startup.

    Fast (sub-second on demo seed). Failures are non-fatal — the rule
    engine treats the model as a soft dependency and falls back to the
    legacy z-score check when no model is loaded.
    """
    if not fraud_model.is_enabled():
        return
    try:
        summary = fraud_model.train_fraud_models()
        if summary:
            logger.info("Fraud model ready for users: %s", list(summary))
    except Exception:  # pragma: no cover — never block startup on this
        logger.exception("Fraud model training failed; continuing without it.")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "omni-api"}

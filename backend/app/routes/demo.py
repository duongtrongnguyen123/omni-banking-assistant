"""Demo resilience routes — scenario recorder + replayer.

These endpoints are deliberately *not* surfaced in the production UI.
They power three pitch-day safety nets:

1. ``POST /api/demo/record/start`` — begin capturing every chat turn
   for the current user. The recorder hooks ``handle_message`` via a
   thin wrapper so no orchestrator code changes are needed.
2. ``POST /api/demo/record/stop`` — flush + return the JSONL.
3. ``POST /api/demo/replay`` — drive a previously-captured script
   through ``handle_message`` at a configurable cadence so the live UI
   animates as if a human were typing.

Output format: one JSON object per line, ``{"ts": ..., "user": "...",
"omni": {...OmniResponse}}``. This is the same shape consumed by the
``Replay`` button in the frontend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..models.schemas import OmniResponse
from ..services.orchestrator import handle_message
from .deps import current_user

log = logging.getLogger("omni.routes.demo")

router = APIRouter(prefix="/api/demo", tags=["demo"])


# ---------------------------------------------------------------------------
# Recorder state (process-global, per-user)
# ---------------------------------------------------------------------------


class _Recording:
    __slots__ = ("started_at", "turns")

    def __init__(self) -> None:
        self.started_at: float = time.time()
        # JSONL-shaped: list[dict] with keys {ts, user, omni}.
        self.turns: list[dict] = []

    def add(self, user_text: str, resp: OmniResponse) -> None:
        self.turns.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "user": user_text,
                # Use model_dump(mode="json") so datetimes serialize.
                "omni": resp.model_dump(mode="json"),
            }
        )


_recordings: dict[str, _Recording] = {}


def record_turn(user_id: str, user_text: str, resp: OmniResponse) -> None:
    """Append a turn to the active recording for `user_id`, if any.

    Called by ``handle_message`` (see ``orchestrator``) so any code path
    that produces an OmniResponse — REST, WebSocket, replay itself —
    contributes to the JSONL. Safe to call when no recording is active.
    """
    rec = _recordings.get(user_id)
    if rec is None:
        return
    try:
        rec.add(user_text, resp)
    except Exception as e:  # pragma: no cover — recorder must never break chat
        log.warning("recorder failed for %s: %s", user_id, e)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class StartResponse(BaseModel):
    recording: bool = True
    started_at: float
    note: str = "Send chat messages via /api/chat — they'll be captured."


class StopResponse(BaseModel):
    recording: bool = False
    turns: int
    duration_ms: int
    # JSONL string — one line per turn.
    jsonl: str
    # Structured turns too, so the frontend can also save as JSON.
    script: list[dict]


class ReplayRequest(BaseModel):
    script: list[dict] = Field(
        default_factory=list,
        description=(
            "List of turns to replay. Each turn must have a 'user' string. "
            "The 'omni' field, if present, is ignored — we re-run NLU."
        ),
    )
    cadence_ms: int = Field(default=800, ge=0, le=10_000)


class ReplayResponse(BaseModel):
    played: int
    duration_ms: int
    transcript: list[dict]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/record/start", response_model=StartResponse)
def record_start(user_id: str = Depends(current_user)) -> StartResponse:
    rec = _Recording()
    _recordings[user_id] = rec
    log.info("recording started for %s", user_id)
    return StartResponse(started_at=rec.started_at)


@router.post("/record/stop", response_model=StopResponse)
def record_stop(user_id: str = Depends(current_user)) -> StopResponse:
    rec = _recordings.pop(user_id, None)
    if rec is None:
        return StopResponse(turns=0, duration_ms=0, jsonl="", script=[])
    duration_ms = int((time.time() - rec.started_at) * 1000)
    jsonl = "\n".join(json.dumps(t, ensure_ascii=False) for t in rec.turns)
    return StopResponse(
        turns=len(rec.turns),
        duration_ms=duration_ms,
        jsonl=jsonl,
        script=rec.turns,
    )


@router.get("/record/status")
def record_status(user_id: str = Depends(current_user)) -> dict:
    rec = _recordings.get(user_id)
    if rec is None:
        return {"recording": False, "turns": 0}
    return {
        "recording": True,
        "turns": len(rec.turns),
        "started_at": rec.started_at,
    }


@router.post("/replay", response_model=ReplayResponse)
async def replay(
    req: ReplayRequest, user_id: str = Depends(current_user)
) -> ReplayResponse:
    """Replay a recorded JSONL script as if the user typed each turn.

    The cadence (default 800ms) gives the frontend animations time to
    play before the next turn lands. Each replayed message goes through
    ``handle_message`` exactly like a real chat turn, so safety, NLU,
    and draft state behave identically.
    """
    started = time.time()
    transcript: list[dict] = []
    for i, turn in enumerate(req.script):
        user_text: Optional[str] = turn.get("user") if isinstance(turn, dict) else None
        if not user_text:
            continue
        if i > 0 and req.cadence_ms > 0:
            await asyncio.sleep(req.cadence_ms / 1000.0)
        try:
            resp = handle_message(user_id, str(user_text))
            transcript.append(
                {
                    "user": user_text,
                    "omni_text": resp.text,
                    "intent": resp.intent,
                }
            )
        except Exception as e:  # pragma: no cover — defensive
            log.warning("replay step %s failed: %s", i, e)
            transcript.append({"user": user_text, "error": str(e)})
    duration_ms = int((time.time() - started) * 1000)
    return ReplayResponse(
        played=len(transcript), duration_ms=duration_ms, transcript=transcript
    )


__all__ = ["router", "record_turn"]

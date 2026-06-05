from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..ml.suggester import suggest, train_for
from .deps import current_user

router = APIRouter(prefix="/api/suggestions", tags=["suggestions"])


@router.get("/recipients")
def recipients(
    when: Optional[str] = Query(
        None,
        description="ISO-8601 timestamp. Defaults to the server's current local time.",
    ),
    limit: int = Query(5, ge=1, le=20),
    user_id: str = Depends(current_user),
) -> list[dict]:
    """Top-K next-transfer suggestions for the caller, given a point in time."""
    dt = datetime.fromisoformat(when).astimezone() if when else None
    return suggest(user_id, dt, limit)


@router.post("/train")
def train(user_id: str = Depends(current_user)) -> dict:
    """Force-retrain the model on the caller's latest history. Called by
    the orchestrator after every executed transfer so the suggestion list
    reflects the most recent behaviour."""
    return train_for(user_id) or {"trained_on": 0, "reason": "not enough data"}

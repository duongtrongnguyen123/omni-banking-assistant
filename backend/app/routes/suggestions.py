from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..ml.suggester import train_for
from ..services.suggester import suggest_for
from .deps import current_user

router = APIRouter(prefix="/api/suggestions", tags=["suggestions"])


@router.get("/recipients")
def recipients(
    when: Optional[str] = Query(
        None,
        description="ISO-8601 timestamp. Defaults to the server's current local time.",
    ),
    limit: int = Query(5, ge=1, le=200),
    all: bool = Query(
        False,
        description=(
            "Include every contact the user has — unseen ones get score 0 "
            "and sort below model-ranked rows. Powers the Danh bạ picker."
        ),
    ),
    user_id: str = Depends(current_user),
) -> list[dict]:
    """Top-K next-transfer suggestions for the caller, given a point in time.

    Routed through the A/B framework — the arm picked for ``user_id``
    overrides the production auto-weight heuristic. When the A/B is
    disabled (``OMNI_DISABLE_ABTEST=1``) the underlying ``suggest`` runs
    with its standard auto-weights.
    """
    dt = datetime.fromisoformat(when).astimezone() if when else None
    _arm, results = suggest_for(user_id, when=dt, k=limit, include_all=all)
    return results


@router.post("/train")
def train(user_id: str = Depends(current_user)) -> dict:
    """Force-retrain the model on the caller's latest history. Called by
    the orchestrator after every executed transfer so the suggestion list
    reflects the most recent behaviour."""
    return train_for(user_id) or {"trained_on": 0, "reason": "not enough data"}

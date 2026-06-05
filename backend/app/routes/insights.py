"""Proactive insights — exposes the `app.ml.insights` helpers as REST.

Currently a single aggregate endpoint; we can split this later if the
frontend wants to lazy-load each section independently.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..ml.insights import summary
from .deps import current_user

router = APIRouter(prefix="/api/insights", tags=["insights"])


@router.get("/summary")
def insights_summary(user_id: str = Depends(current_user)) -> dict:
    return summary(user_id)

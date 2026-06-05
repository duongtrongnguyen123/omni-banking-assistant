"""Proactive insights — exposes the `app.ml.insights` helpers as REST.

Currently a single aggregate endpoint; we can split this later if the
frontend wants to lazy-load each section independently.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..config import get_settings
from ..ml.insights import summary
from .deps import current_user

log = logging.getLogger("omni.routes.insights")

router = APIRouter(prefix="/api/insights", tags=["insights"])


# Canned demo data — last-resort fallback when ``offline_demo=1`` AND the
# regular ``summary()`` raises. Shape matches ``ml.insights.summary``
# exactly. Numbers tuned to look believable on the curated seed.
_CANNED_SUMMARY: dict = {
    "month_over_month": [
        {"category": "ăn uống", "this_month": 2_400_000, "last_month": 1_900_000, "delta_pct": 26.3},
        {"category": "gia đình", "this_month": 5_000_000, "last_month": 5_000_000, "delta_pct": 0.0},
        {"category": "tiện ích", "this_month": 1_100_000, "last_month": 980_000, "delta_pct": 12.2},
    ],
    "anomalies": [],
    "subscriptions": [
        {"description": "Netflix", "typical_amount": 260_000, "months_seen": 4},
        {"description": "Spotify", "typical_amount": 59_000, "months_seen": 5},
    ],
}


@router.get("/summary")
def insights_summary(user_id: str = Depends(current_user)) -> dict:
    try:
        return summary(user_id)
    except Exception as e:  # pragma: no cover — guarded by offline-mode tests
        if get_settings().offline_demo:
            log.warning("insights summary failed in offline mode, serving canned: %s", e)
            return _CANNED_SUMMARY
        raise

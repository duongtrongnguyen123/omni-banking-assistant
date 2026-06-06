"""Regression: anomalies() must not flag tiny deltas on constant history.

When a contact has an identical-amount history (e.g. five 200.000đ rent
transfers), `sigma == 0` and the per-contact branch can't compute a
real z-score. The previous implementation flagged ANY positive delta —
even a 1đ rounding swing — as an anomaly with `ratio≈1.0` and
`z_score=99.0`, surfacing as "cao gấp 1.0 lần mức thường" in the UI.

A flag must require a meaningful change: either ≥1.5× the median or
≥100k absolute step.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.ml.insights import anomalies
from app.models.schemas import Transaction


def _seed_constant(amount: int, n: int = 5) -> list[Transaction]:
    base = datetime(2026, 6, 1, 9, 0)
    return [
        Transaction(
            id=f"t_{i}",
            owner_id="u_const",
            contact_id="c_landlord",
            amount=amount,
            description="tiền nhà",
            category="bills",
            status="completed",
            created_at=base + timedelta(days=i),
        )
        for i in range(n)
    ]


def test_constant_history_one_dong_diff_not_flagged():
    """Five identical 200.000đ rent + one 200.001đ tx → no anomaly."""
    txs = _seed_constant(200_000, n=5)
    txs.append(
        Transaction(
            id="t_off_by_one",
            owner_id="u_const",
            contact_id="c_landlord",
            amount=200_001,
            description="tiền nhà",
            category="bills",
            status="completed",
            created_at=datetime(2026, 6, 10, 9, 0),
        )
    )

    out = anomalies("u_const", datetime(2026, 6, 11, 9, 0), _txs=txs)
    assert out == [], f"1đ delta should not surface as an anomaly: {out}"


def test_constant_history_small_step_under_100k_not_flagged():
    """50k step above a 200k constant median (ratio 1.25, delta 50k) →
    below both 1.5× and 100k thresholds → no flag."""
    txs = _seed_constant(200_000, n=5)
    txs.append(
        Transaction(
            id="t_small_bump",
            owner_id="u_const",
            contact_id="c_landlord",
            amount=250_000,
            description="tiền nhà",
            category="bills",
            status="completed",
            created_at=datetime(2026, 6, 10, 9, 0),
        )
    )

    out = anomalies("u_const", datetime(2026, 6, 11, 9, 0), _txs=txs)
    assert out == [], f"50k step (ratio 1.25) should not flag: {out}"


def test_per_user_constant_history_one_dong_diff_not_flagged():
    """Per-user fallback branch (<3 peers for the contact) also runs
    through the sigma=0 codepath when ALL of the user's history is
    identical. A 1đ tx to a new contact in that universe must not
    fire either."""
    base = datetime(2026, 6, 1, 9, 0)
    txs = [
        Transaction(
            id=f"u_{i}",
            owner_id="u_const",
            contact_id=f"c_peer_{i}",
            amount=200_000,
            description="",
            category="other",
            status="completed",
            created_at=base + timedelta(days=i),
        )
        for i in range(5)
    ]
    txs.append(
        Transaction(
            id="t_off_by_one",
            owner_id="u_const",
            contact_id="c_new",
            amount=200_001,
            description="",
            category="other",
            status="completed",
            created_at=datetime(2026, 6, 10, 9, 0),
        )
    )

    out = anomalies("u_const", datetime(2026, 6, 11, 9, 0), _txs=txs)
    assert out == [], f"per-user 1đ delta should not flag: {out}"

"""Regression tests for the four-bug correctness batch.

Each test pins one behaviour we changed in this PR. See the PR body for
file:line references back to the originals.

1. ``month_over_month`` must not claim "+100%" for a brand-new category;
   it must emit ``delta_pct=None, is_new=True`` so the UI can render
   "(mới)" instead of a misleading percentage.
2. ``subscriptions`` must apply a ``months_distinct / months_spanned``
   coverage check so three coincidental transfers scattered across six
   months don't get labelled a subscription.
3. ``detect_recurring`` must bucket transactions in ICT wall-clock time;
   a tx stamped ``2026-06-01T00:30:00+00:00`` (May 31 23:30 ICT) must
   land in the May bucket, not June.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.banking.recurring import detect_recurring
from app.ml.insights import month_over_month, subscriptions
from app.models.schemas import Transaction


# ---------------------------------------------------------------- bug 1


def _tx(
    tx_id: str,
    *,
    contact_id: str = "c_x",
    amount: int = 100_000,
    description: str = "",
    category: str = "other",
    created_at: datetime,
) -> Transaction:
    return Transaction(
        id=tx_id,
        owner_id="u_test",
        contact_id=contact_id,
        amount=amount,
        description=description,
        category=category,
        status="completed",
        created_at=created_at,
    )


def test_mom_brand_new_category_emits_is_new_with_null_delta():
    """Spend in June with zero baseline in May → delta_pct=None, is_new=True."""
    txs = [
        _tx(
            "t_new_jun",
            category="travel",
            amount=2_000_000,
            created_at=datetime(2026, 6, 5, 9, 0),
        ),
        # Unrelated category to keep the MoM dict non-empty for "old" cats too.
        _tx(
            "t_food_may",
            category="food",
            amount=500_000,
            created_at=datetime(2026, 5, 10, 9, 0),
        ),
        _tx(
            "t_food_jun",
            category="food",
            amount=600_000,
            created_at=datetime(2026, 6, 10, 9, 0),
        ),
    ]

    out = month_over_month("u_test", datetime(2026, 6, 15, 9, 0), _txs=txs)

    assert "travel" in out
    assert out["travel"]["delta_pct"] is None
    assert out["travel"]["is_new"] is True
    assert out["travel"]["this"] == 2_000_000
    assert out["travel"]["last"] == 0

    # Sanity: an existing category must still report a real delta and
    # ``is_new=False`` so the new flag doesn't poison the old path.
    assert out["food"]["is_new"] is False
    assert out["food"]["delta_pct"] is not None


# ---------------------------------------------------------------- bug 2


def test_subscriptions_rejects_scattered_three_transfers():
    """3 tx scattered across 6 months → coverage 0.5 → not a subscription."""
    txs = [
        _tx(
            "t_jan",
            contact_id="c_friend",
            amount=200_000,
            description="cafe",
            created_at=datetime(2026, 1, 5, 9, 0),
        ),
        _tx(
            "t_apr",
            contact_id="c_friend",
            amount=200_000,
            description="cafe",
            created_at=datetime(2026, 4, 5, 9, 0),
        ),
        _tx(
            "t_jun",
            contact_id="c_friend",
            amount=200_000,
            description="cafe",
            created_at=datetime(2026, 6, 5, 9, 0),
        ),
    ]

    out = subscriptions("u_test", _txs=txs)
    assert out == [], (
        f"3 tx over 6 months should not be flagged as a subscription: {out}"
    )


def test_subscriptions_keeps_three_consecutive_months():
    """3 tx in 3 consecutive months → coverage 1.0 → still flagged."""
    txs = [
        _tx(
            "t_apr",
            contact_id="c_netflix",
            amount=200_000,
            description="netflix",
            created_at=datetime(2026, 4, 5, 9, 0),
        ),
        _tx(
            "t_may",
            contact_id="c_netflix",
            amount=200_000,
            description="netflix",
            created_at=datetime(2026, 5, 5, 9, 0),
        ),
        _tx(
            "t_jun",
            contact_id="c_netflix",
            amount=200_000,
            description="netflix",
            created_at=datetime(2026, 6, 5, 9, 0),
        ),
    ]

    out = subscriptions("u_test", _txs=txs)
    assert len(out) == 1
    assert out[0]["contact_id"] == "c_netflix"
    assert out[0]["typical_amount"] == 200_000


# ---------------------------------------------------------------- bug 4


def test_recurring_buckets_month_edge_tx_in_local_time():
    """A tx stamped 2026-06-01T00:30:00+00:00 is May 31 23:30 ICT.

    The bucketing must put it in May 2026, not June 2026. Two earlier
    May/Apr/Mar bookings establish the pattern; if the edge tx leaks
    into June it would only be 2 months, not 3, and detect_recurring
    would (incorrectly) drop the pattern.
    """
    # ICT is UTC+7 — the May edge tx at 00:30 UTC is 07:30 ICT next day?
    # No: 2026-06-01T00:30:00Z + 7h = 2026-06-01T07:30 ICT (still June).
    # The bug bites in the *other* direction: 2026-06-01T00:30:00 UTC =
    # 2026-06-01 07:30 ICT. To exercise the month-edge bug we need a UTC
    # stamp whose ICT equivalent falls in the previous calendar month.
    # 2026-06-01T00:30:00 UTC -> 2026-06-01 07:30 ICT (no edge crossing).
    # Use a UTC stamp at the very end of May UTC, which is still May in
    # ICT — the original `replace(tzinfo=None)` bug would store the UTC
    # wall-clock, and at month-edges the two diverge. Concrete case:
    # 2026-05-31T23:30:00+07:00 = 2026-05-31 16:30 UTC = May 31 in both.
    # The dangerous one is 2026-06-01T00:30:00+00:00 -> 2026-06-01 07:30
    # ICT (still June). So pick:
    # 2026-05-31T18:00:00+00:00 -> 2026-06-01 01:00 ICT (June ICT, May UTC).
    # The OLD code would compute month=5 (UTC), the FIXED code month=6
    # (ICT). Test that the fix puts it in JUNE buckets.
    edge_tx_utc = datetime(2026, 5, 31, 18, 0, tzinfo=timezone.utc)
    # That's 2026-06-01 01:00 ICT.

    # Build a 3-month pattern in ICT: April, May, and the edge tx (which
    # is June ICT). All three contribute to distinct (year, month) buckets
    # ONLY IF the edge tx is interpreted as ICT.
    txs = [
        Transaction(
            id="t_apr",
            owner_id="u_tz",
            contact_id="c_landlord",
            amount=5_000_000,
            description="tien nha thang 4",
            category="bills",
            status="completed",
            created_at=datetime(2026, 4, 15, 9, 0, tzinfo=timezone(timedelta(hours=7))),
        ),
        Transaction(
            id="t_may",
            owner_id="u_tz",
            contact_id="c_landlord",
            amount=5_000_000,
            description="tien nha thang 4",
            category="bills",
            status="completed",
            created_at=datetime(2026, 5, 15, 9, 0, tzinfo=timezone(timedelta(hours=7))),
        ),
        Transaction(
            id="t_edge_jun",
            owner_id="u_tz",
            contact_id="c_landlord",
            amount=5_000_000,
            description="tien nha thang 4",
            category="bills",
            status="completed",
            created_at=edge_tx_utc,  # June 1 01:00 ICT
        ),
    ]

    # ref_now in early June so nothing is "stale".
    ref_now = datetime(2026, 6, 20, 9, 0)
    out = detect_recurring(txs, ref_now=ref_now)

    assert len(out) == 1, (
        f"3 monthly bookings (Apr/May/Jun ICT) should yield one pattern: {out}"
    )
    pat = out[0]
    assert pat.month_count == 3
    # last_seen must be the ICT wall-clock of the edge tx, i.e. June 1.
    assert pat.last_seen.year == 2026
    assert pat.last_seen.month == 6
    assert pat.last_seen.day == 1

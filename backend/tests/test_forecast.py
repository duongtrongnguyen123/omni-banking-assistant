"""Regression coverage for ``ml.insights.forecast`` + the ``forecast_card``
slot on ``OmniResponse``.

Three layers under test:

  * ``forecast()`` math — daily rate, projected_total, projected_eom_balance
    arithmetic, plus the early-month bail and zero-spend bail.
  * Threshold flags — over_budget (≥1.20×), under_budget (≤0.80×),
    overdraft_risk (projected_eom_balance < 0). Boundary cases checked
    on both sides.
  * Wire-up — the ``forecast_card`` on ``OmniResponse`` carries the same
    dict ``forecast()`` returned, and is ``None`` when ``forecast()``
    bailed (so the UI doesn't render an empty card).

We construct ``Transaction`` rows by hand and call ``forecast`` with
``_txs`` so we never touch SQLite. That keeps the math tests fast,
deterministic, and independent of the seed data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from app.ml.insights import forecast
from app.models.schemas import Account, Transaction, User


USER = "u_an"
TZ = timezone(timedelta(hours=7))


# ---------------------------------------------------------------------------
# Fixtures — synthesise a Transaction stream + monkey-patch the Store user
# so the balance lookup inside forecast() returns a known number.
# ---------------------------------------------------------------------------


def _tx(amount: int, day_of_month: int, *, month: int = 6, year: int = 2026) -> Transaction:
    return Transaction(
        id=f"t_{day_of_month}_{amount}",
        owner_id=USER,
        contact_id="c_test",
        amount=amount,
        description="test",
        category="other",
        status="completed",
        created_at=datetime(year, month, day_of_month, 12, 0, 0, tzinfo=TZ),
    )


@pytest.fixture(autouse=True)
def _patch_balance(monkeypatch):
    """Patch get_store().get_user_or_none so forecast()'s
    current_balance always sees the same number — keeps the
    projected_eom_balance arithmetic independent of which test ran first."""
    from app import store as store_module

    fake_user = User(
        id=USER,
        display_name="Test",
        phone="000",
        accounts=[
            Account(
                id="a_test",
                bank="Omni",
                number="0000",
                balance=10_000_000,
                currency="VND",
                primary=True,
            ),
        ],
    )

    class _FakeStore:
        def get_user_or_none(self, _user_id: str):
            return fake_user

    monkeypatch.setattr(store_module, "get_store", lambda: _FakeStore())
    yield


# ---------------------------------------------------------------------------
# 1. Bails — early month + zero spend
# ---------------------------------------------------------------------------


def test_forecast_bails_when_less_than_three_days_elapsed():
    """Day 2 of the month is too thin a sample for an honest projection."""
    when = datetime(2026, 6, 2, 12, tzinfo=TZ)
    out = forecast(USER, when, _txs=[_tx(100_000, 1)])
    assert out is None


def test_forecast_bails_when_no_spend_this_month():
    """Plenty of days elapsed, but every tx is in a different month."""
    when = datetime(2026, 6, 15, 12, tzinfo=TZ)
    out = forecast(USER, when, _txs=[_tx(1_000_000, 10, month=5)])
    assert out is None


# ---------------------------------------------------------------------------
# 2. Math — daily rate, projection, EoM balance
# ---------------------------------------------------------------------------


def test_daily_rate_is_spent_divided_by_days_elapsed():
    when = datetime(2026, 6, 10, 12, tzinfo=TZ)  # day 10 of 30
    txs = [
        _tx(500_000, 3),
        _tx(500_000, 5),
        _tx(500_000, 9),
    ]
    out = forecast(USER, when, _txs=txs)
    assert out is not None
    assert out["days_elapsed"] == 10
    assert out["days_in_month"] == 30
    assert out["spent_so_far"] == 1_500_000
    # 1_500_000 // 10 = 150_000
    assert out["daily_rate"] == 150_000
    # 150_000 * 30 = 4_500_000
    assert out["projected_total"] == 4_500_000
    # 4_500_000 - 1_500_000 already spent
    assert out["projected_remaining_spend"] == 3_000_000
    # current_balance (10M) − 3M remaining
    assert out["projected_eom_balance"] == 7_000_000


def test_pace_vs_last_month_none_when_last_month_zero():
    when = datetime(2026, 6, 10, 12, tzinfo=TZ)
    out = forecast(USER, when, _txs=[_tx(1_000_000, 5)])
    assert out is not None
    assert out["last_month_total"] == 0
    assert out["pace_vs_last_month"] is None
    assert out["over_budget"] is False
    assert out["under_budget"] is False


# ---------------------------------------------------------------------------
# 3. Threshold flags — over / under / overdraft, on both sides of bound
# ---------------------------------------------------------------------------


def test_over_budget_fires_at_boundary_120_percent():
    when = datetime(2026, 6, 10, 12, tzinfo=TZ)
    # Last month total = 1M; daily 100k × 30 days = 3M = 3.0× → over
    txs = [_tx(100_000, d) for d in range(1, 11)]  # 1M over 10 days
    txs.append(_tx(1_000_000, 5, month=5))         # 1M in May
    out = forecast(USER, when, _txs=txs)
    assert out is not None
    assert out["pace_vs_last_month"] == 3.0
    assert out["over_budget"] is True
    assert out["under_budget"] is False


def test_under_budget_fires_at_boundary_80_percent():
    when = datetime(2026, 6, 10, 12, tzinfo=TZ)
    # June: 240k over 10 days → projected 720k.
    # May: 1M → pace 0.72 ≤ 0.8 → under
    txs = [_tx(24_000, d) for d in range(1, 11)]
    txs.append(_tx(1_000_000, 5, month=5))
    out = forecast(USER, when, _txs=txs)
    assert out is not None
    assert out["pace_vs_last_month"] == 0.72
    assert out["under_budget"] is True
    assert out["over_budget"] is False


def test_in_band_pace_does_not_trip_either_flag():
    when = datetime(2026, 6, 10, 12, tzinfo=TZ)
    # June: 333k over 10 days → projected 999k.
    # May: 1M → pace ~1.0 → neither over nor under
    txs = [_tx(33_300, d) for d in range(1, 11)]
    txs.append(_tx(1_000_000, 5, month=5))
    out = forecast(USER, when, _txs=txs)
    assert out is not None
    assert 0.80 < (out["pace_vs_last_month"] or 0) < 1.20
    assert out["over_budget"] is False
    assert out["under_budget"] is False


def test_overdraft_risk_fires_when_eom_balance_negative():
    """current_balance is patched to 10M. If projected_remaining_spend
    exceeds 10M, eom_balance goes negative → overdraft_risk True."""
    when = datetime(2026, 6, 10, 12, tzinfo=TZ)
    # 1.5M/day × 20 remaining days = 30M projected remaining; far above
    # the 10M balance.
    txs = [_tx(1_500_000, d) for d in range(1, 11)]
    out = forecast(USER, when, _txs=txs)
    assert out is not None
    assert out["projected_remaining_spend"] > 10_000_000
    assert out["projected_eom_balance"] < 0
    assert out["overdraft_risk"] is True


def test_overdraft_risk_off_when_balance_covers_projection():
    when = datetime(2026, 6, 10, 12, tzinfo=TZ)
    # 100k/day × 30 days = 3M projected total. Far below the 10M balance.
    txs = [_tx(100_000, d) for d in range(1, 11)]
    out = forecast(USER, when, _txs=txs)
    assert out is not None
    assert out["projected_eom_balance"] > 0
    assert out["overdraft_risk"] is False


# ---------------------------------------------------------------------------
# 4. OmniResponse.forecast_card wire-up
# ---------------------------------------------------------------------------


def test_forecast_card_propagates_summary_dict(monkeypatch):
    """When the insights handler runs, the response's forecast_card
    field carries the same dict forecast() returned."""
    from app.models.schemas import NLUResult, ExtractedEntities
    from app.services import insights_handler

    fake_forecast = {
        "days_elapsed": 10, "days_in_month": 30,
        "spent_so_far": 1_500_000, "daily_rate": 150_000,
        "projected_total": 4_500_000, "projected_remaining_spend": 3_000_000,
        "current_balance": 10_000_000, "projected_eom_balance": 7_000_000,
        "last_month_total": 5_000_000, "pace_vs_last_month": 0.9,
        "over_budget": False, "under_budget": False, "overdraft_risk": False,
    }
    from app.ml import insights as insights_mod

    monkeypatch.setattr(
        insights_mod,
        "summary",
        lambda _user_id: {
            "mom": {}, "anomalies": [], "subscriptions": [],
            "forecast": fake_forecast,
            "generated_at": "",
        },
    )
    nlu = NLUResult(
        intent="insights",
        confidence=0.9,
        entities=ExtractedEntities(),
        raw_text="Đến cuối tháng còn lại bao nhiêu?",
    )
    resp = insights_handler.handle_insights(USER, nlu, history_msgs=None)
    assert resp.forecast_card == fake_forecast


def test_forecast_card_is_none_when_summary_has_no_forecast(monkeypatch):
    """forecast() bailed (early month or zero spend); the OmniResponse
    forecast_card field must be None so the UI doesn't render an empty
    card."""
    from app.models.schemas import NLUResult, ExtractedEntities
    from app.services import insights_handler

    from app.ml import insights as insights_mod

    monkeypatch.setattr(
        insights_mod,
        "summary",
        lambda _user_id: {
            "mom": {}, "anomalies": [], "subscriptions": [],
            "forecast": None,
            "generated_at": "",
        },
    )
    nlu = NLUResult(
        intent="insights",
        confidence=0.9,
        entities=ExtractedEntities(),
        raw_text="Phân tích chi tiêu của mình",
    )
    resp = insights_handler.handle_insights(USER, nlu, history_msgs=None)
    assert resp.forecast_card is None

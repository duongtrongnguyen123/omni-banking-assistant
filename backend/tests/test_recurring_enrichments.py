"""Regression coverage for the recurring-handler enrichments.

The handler decorates each detected ``RecurringPattern`` with five
extra fields (see feat/recurring-enrichments-v2):

  * ``suggested_cron``           ("0 9 {typical_day} * *")
  * ``suggested_cron_label``     (via ``_cron_label``)
  * ``is_already_scheduled``     same-contact + amount-within-±20%
  * ``is_missed``                expected-day-passed + no matching tx
  * ``days_overdue``             today − expected_date when missed

Patterns are then re-sorted: missed first (longest overdue → most
recent), on-track rows preserve detector order.

Tests monkeypatch ``store.transactions_of`` / ``contacts_of`` /
``schedules_of``, the detector, and the orchestrator's ``now()`` so we
can drive each branch deterministically without touching SQLite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.banking.recurring import RecurringPattern
from app.models.schemas import (
    Account,
    Contact,
    ExtractedEntities,
    NLUResult,
    Schedule,
    Transaction,
    User,
)


USER = "u_an"
TZ = timezone(timedelta(hours=7))


# ---------------------------------------------------------------------------
# Test rig — minimal fakes for the store + orchestrator
# ---------------------------------------------------------------------------


def _contact(cid: str, name: str) -> Contact:
    return Contact(
        id=cid,
        owner_id=USER,
        display_name=name,
        bank="Vietcombank",
        account_number="0000000000",
        account_masked="*000",
        aliases=[],
        frequent=True,
    )


def _user_with_account() -> User:
    return User(
        id=USER,
        display_name="Test",
        phone="0",
        accounts=[
            Account(
                id="acc",
                bank="Omni",
                number="0000",
                balance=10_000_000,
                currency="VND",
                primary=True,
            )
        ],
    )


def _tx(contact_id: str, amount: int, day: int,
         desc: str, *, month: int = 6, year: int = 2026) -> Transaction:
    return Transaction(
        id=f"t_{contact_id}_{day}_{amount}_{desc}",
        owner_id=USER,
        contact_id=contact_id,
        amount=amount,
        description=desc,
        category="other",
        status="completed",
        created_at=datetime(year, month, day, 12, 0, 0, tzinfo=TZ),
    )


def _pattern(contact_id: str, typical_day: int, amount: int, desc: str,
             *, confidence: float = 0.8, last_seen_month: int = 5) -> RecurringPattern:
    last_seen = datetime(2026, last_seen_month, typical_day, 12, tzinfo=TZ)
    return RecurringPattern(
        contact_id=contact_id,
        description=desc,
        typical_amount=amount,
        typical_day=typical_day,
        occurrence_count=4,
        month_count=4,
        first_seen=last_seen - timedelta(days=120),
        last_seen=last_seen,
        next_run=last_seen + timedelta(days=30),
        confidence=confidence,
    )


def _schedule(contact_id: str, amount: int, *, active: bool = True) -> Schedule:
    return Schedule(
        id=f"s_{contact_id}_{amount}",
        owner_id=USER,
        contact_id=contact_id,
        source_account_id="acc",
        amount=amount,
        description="auto",
        cron="0 9 1 * *",
        next_run=datetime(2026, 7, 1, 9, tzinfo=TZ),
        active=active,
    )


@pytest.fixture
def rig(monkeypatch):
    """Returns a builder; call ``rig(...)`` inside a test to install
    the synthetic store + detector + now() before invoking
    ``_handle_recurring``."""

    def _install(
        *,
        patterns: list[RecurringPattern],
        contacts: list[Contact],
        txs: list[Transaction],
        schedules: list[Schedule],
        when: datetime,
    ):
        from app.services import orchestrator as orch
        from app.banking import recurring as recmod

        class _FakeStore:
            def transactions_of(self, _user_id: str):
                return list(txs)

            def contacts_of(self, _user_id: str):
                return list(contacts)

            def schedules_of(self, _user_id: str):
                return list(schedules)

            def get_user(self, _user_id: str):
                return _user_with_account()

            def primary_account(self, _user_id: str):
                return _user_with_account().accounts[0]

        monkeypatch.setattr(orch, "get_store", lambda: _FakeStore())
        # detect_recurring is imported into both modules — patch both so
        # the orchestrator picks up the fake regardless of which symbol
        # it dereferences.
        monkeypatch.setattr(orch, "detect_recurring", lambda _txs: list(patterns))
        monkeypatch.setattr(recmod, "detect_recurring", lambda _txs: list(patterns))
        monkeypatch.setattr(orch, "now", lambda: when)

    return _install


def _call(nlu_text: str = "Mình có khoản nào trả đều?"):
    from app.services.orchestrator import _handle_recurring

    nlu = NLUResult(
        intent="recurring",
        confidence=0.9,
        entities=ExtractedEntities(),
        raw_text=nlu_text,
    )
    return _handle_recurring(USER, nlu, history_msgs=None)


# ---------------------------------------------------------------------------
# suggested_cron + label
# ---------------------------------------------------------------------------


def test_suggested_cron_format_matches_typical_day(rig):
    rig(
        patterns=[_pattern("c1", typical_day=15, amount=500_000, desc="Trả góp")],
        contacts=[_contact("c1", "Nguyễn Thị Lan")],
        txs=[],
        schedules=[],
        when=datetime(2026, 6, 1, 12, tzinfo=TZ),  # day 1, nothing missed yet
    )
    resp = _call()
    assert resp.recurring_patterns is not None
    p = resp.recurring_patterns[0]
    assert p["suggested_cron"] == "0 9 15 * *"
    assert "ngày 15" in p["suggested_cron_label"]


# ---------------------------------------------------------------------------
# is_already_scheduled — exact, within-tolerance, outside-tolerance, inactive
# ---------------------------------------------------------------------------


def test_already_scheduled_exact_match(rig):
    rig(
        patterns=[_pattern("c1", typical_day=15, amount=500_000, desc="X")],
        contacts=[_contact("c1", "X")],
        txs=[],
        schedules=[_schedule("c1", 500_000)],
        when=datetime(2026, 6, 1, 12, tzinfo=TZ),
    )
    assert _call().recurring_patterns[0]["is_already_scheduled"] is True


def test_already_scheduled_within_20_percent(rig):
    """Pattern says ~500k; an existing schedule at 580k (16% high)
    counts as already-covered."""
    rig(
        patterns=[_pattern("c1", typical_day=15, amount=500_000, desc="X")],
        contacts=[_contact("c1", "X")],
        txs=[],
        schedules=[_schedule("c1", 580_000)],
        when=datetime(2026, 6, 1, 12, tzinfo=TZ),
    )
    assert _call().recurring_patterns[0]["is_already_scheduled"] is True


def test_already_scheduled_outside_tolerance_does_not_match(rig):
    """700k (40% high) is outside the ±20% band → no match."""
    rig(
        patterns=[_pattern("c1", typical_day=15, amount=500_000, desc="X")],
        contacts=[_contact("c1", "X")],
        txs=[],
        schedules=[_schedule("c1", 700_000)],
        when=datetime(2026, 6, 1, 12, tzinfo=TZ),
    )
    assert _call().recurring_patterns[0]["is_already_scheduled"] is False


def test_inactive_schedule_does_not_count(rig):
    rig(
        patterns=[_pattern("c1", typical_day=15, amount=500_000, desc="X")],
        contacts=[_contact("c1", "X")],
        txs=[],
        schedules=[_schedule("c1", 500_000, active=False)],
        when=datetime(2026, 6, 1, 12, tzinfo=TZ),
    )
    assert _call().recurring_patterns[0]["is_already_scheduled"] is False


# ---------------------------------------------------------------------------
# is_missed semantics
# ---------------------------------------------------------------------------


def test_not_missed_when_expected_day_is_in_future(rig):
    """typical_day = 15, today = day 5 of month → expected_date is the
    future → not missed."""
    rig(
        patterns=[_pattern("c1", typical_day=15, amount=500_000, desc="X")],
        contacts=[_contact("c1", "X")],
        txs=[],
        schedules=[],
        when=datetime(2026, 6, 5, 12, tzinfo=TZ),
    )
    p = _call().recurring_patterns[0]
    assert p["is_missed"] is False
    assert p["days_overdue"] == 0


def test_missed_when_no_matching_tx_this_month(rig):
    """typical_day = 5, today = day 20, NO matching tx in June → missed."""
    rig(
        patterns=[_pattern("c1", typical_day=5, amount=500_000, desc="Trả góp")],
        contacts=[_contact("c1", "X")],
        txs=[
            # Last May payment exists, but nothing in June.
            _tx("c1", 500_000, 5, "Trả góp", month=5),
        ],
        schedules=[],
        when=datetime(2026, 6, 20, 12, tzinfo=TZ),
    )
    p = _call().recurring_patterns[0]
    assert p["is_missed"] is True
    assert p["days_overdue"] == 15


def test_not_missed_when_matching_tx_exists_this_month(rig):
    """Expected-day passed, but a tx with the matching contact +
    normalized description WAS recorded in June → not missed."""
    rig(
        patterns=[_pattern("c1", typical_day=5, amount=500_000, desc="Trả góp")],
        contacts=[_contact("c1", "X")],
        txs=[_tx("c1", 510_000, 6, "Trả góp")],  # paid on June 6
        schedules=[],
        when=datetime(2026, 6, 20, 12, tzinfo=TZ),
    )
    p = _call().recurring_patterns[0]
    assert p["is_missed"] is False
    assert p["days_overdue"] == 0


def test_not_missed_when_only_different_contact_paid_this_month(rig):
    """Pattern is for c1; a c2 transfer with the same description does
    NOT count as paying the pattern."""
    rig(
        patterns=[_pattern("c1", typical_day=5, amount=500_000, desc="Trả góp")],
        contacts=[_contact("c1", "X"), _contact("c2", "Y")],
        txs=[_tx("c2", 500_000, 10, "Trả góp")],
        schedules=[],
        when=datetime(2026, 6, 20, 12, tzinfo=TZ),
    )
    assert _call().recurring_patterns[0]["is_missed"] is True


def test_typical_day_31_clamps_in_february(rig):
    """Pattern says day 31 but it's February — clamp to month length
    so we don't crash on a date(2026, 2, 31)."""
    rig(
        patterns=[_pattern("c1", typical_day=31, amount=500_000, desc="X")],
        contacts=[_contact("c1", "X")],
        txs=[],
        schedules=[],
        when=datetime(2026, 2, 27, 12, tzinfo=TZ),
    )
    # Feb 27 < clamped-day Feb 28 → not missed; no exception.
    p = _call().recurring_patterns[0]
    assert p["is_missed"] is False


# ---------------------------------------------------------------------------
# Sort order: missed first, then days_overdue desc; on-track preserve order
# ---------------------------------------------------------------------------


def test_missed_patterns_sort_first(rig):
    """Detector returns A (not missed) then B (missed). The handler
    must re-sort so B comes first."""
    rig(
        patterns=[
            _pattern("c1", typical_day=25, amount=500_000, desc="future"),
            _pattern("c2", typical_day=5, amount=500_000, desc="overdue"),
        ],
        contacts=[_contact("c1", "Future"), _contact("c2", "Overdue")],
        txs=[],  # nothing paid this month
        schedules=[],
        when=datetime(2026, 6, 20, 12, tzinfo=TZ),
    )
    out = _call().recurring_patterns
    assert out[0]["recipient_name"] == "Overdue"
    assert out[0]["is_missed"] is True
    assert out[1]["recipient_name"] == "Future"
    assert out[1]["is_missed"] is False


def test_two_missed_sort_by_days_overdue_desc(rig):
    rig(
        patterns=[
            _pattern("c1", typical_day=15, amount=500_000, desc="recent"),
            _pattern("c2", typical_day=2, amount=500_000, desc="older"),
        ],
        contacts=[_contact("c1", "Recent"), _contact("c2", "Older")],
        txs=[],
        schedules=[],
        when=datetime(2026, 6, 20, 12, tzinfo=TZ),
    )
    out = _call().recurring_patterns
    # Older (day-2, ~18 overdue) before Recent (day-15, ~5 overdue).
    assert out[0]["recipient_name"] == "Older"
    assert out[0]["days_overdue"] > out[1]["days_overdue"]

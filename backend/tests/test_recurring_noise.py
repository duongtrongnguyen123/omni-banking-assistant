"""Regression: default chat transfer descriptions must not look recurring.

``execute_transfer`` writes "Đã chuyển" (or leaves the description
blank) for chat-side transfers where the user didn't supply a note.
If those defaults aren't in the noise blacklist, three+ months of
unrelated transfers to the same contact get falsely tagged as a
recurring line — the user is then prompted to schedule a payment that
doesn't actually repeat.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.banking.recurring import _NOISE_DESCRIPTIONS, detect_recurring
from app.models.schemas import Transaction


@pytest.mark.parametrize(
    "default_desc",
    ["Đã chuyển", "đã chuyển", "Chuyển tiền", "Chuyển khoản", "transfer", ""],
)
def test_default_chat_descriptions_do_not_trigger_recurring(default_desc):
    """Three transfers to mẹ across three different months with the
    default "Đã chuyển" description must NOT be flagged recurring."""
    txs = [
        Transaction(
            id=f"t_{i}",
            owner_id="u_recur",
            contact_id="c_me",
            amount=500_000,
            description=default_desc,
            category="other",
            status="completed",
            created_at=datetime(2026, m, 5, 9, 0),
        )
        for i, m in enumerate([3, 4, 5])
    ]
    patterns = detect_recurring(txs, ref_now=datetime(2026, 5, 20))
    assert patterns == [], (
        f"Default chat description {default_desc!r} should be noise but "
        f"got patterns: {patterns}"
    )


def test_noise_blacklist_contains_chat_defaults():
    # Folded forms — accent-stripped + lowercase, as used by ``_normalize``.
    for token in ("da chuyen", "chuyen tien", "chuyen khoan", "transfer"):
        assert token in _NOISE_DESCRIPTIONS


def test_real_recurring_with_distinct_label_still_detected():
    """Sanity guard: the noise expansion must not blanket-suppress real
    recurring lines that carry a distinctive description like
    "Tiền điện T03"."""
    txs = [
        Transaction(
            id=f"t_e_{i}",
            owner_id="u_recur",
            contact_id="c_evn",
            amount=350_000,
            description="Tiền điện",
            category="utilities",
            status="completed",
            created_at=datetime(2026, m, 10, 9, 0),
        )
        for i, m in enumerate([3, 4, 5])
    ]
    patterns = detect_recurring(txs, ref_now=datetime(2026, 5, 20))
    assert len(patterns) == 1
    assert patterns[0].contact_id == "c_evn"

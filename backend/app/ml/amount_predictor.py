"""Predict the most likely transfer amount for a (user, contact) pair.

Used by the orchestrator when the user issues a transfer command without
specifying an amount (e.g. "chuyển cho mẹ"). Rather than asking "bạn muốn
chuyển bao nhiêu?", we pre-fill the draft with a confident suggestion drawn
from past behaviour so the confirm card reads as a one-tap repeat.

The strategy is deliberately simple, deterministic, and stdlib-only — no
new pip dependencies, no model files. It runs over the same transaction
list the rest of the app already loads from `Store`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Optional

from ..store import get_store, now

# Look-back window for the "similar-date" heuristic. Six months covers
# monthly bills/allowances (lương về → chuyển mẹ) without dragging in
# stale lifestyle changes.
_SIMILAR_WINDOW = timedelta(days=183)
_DAY_OF_MONTH_TOLERANCE = 3


def _round_to_nice(amount: int) -> int:
    """Snap to a tidy round number so the suggestion reads as a habit, not
    a noisy historical fluke (e.g. 2_017_345 → 2_000_000). We round to the
    largest "natural" step ≤ amount/10."""
    if amount <= 0:
        return amount
    # Pick a step proportional to magnitude: 100k under 1M, 100k under 10M,
    # 500k otherwise. Keeps small social transfers (50k cà phê) intact while
    # smoothing large recurring ones.
    if amount < 1_000_000:
        step = 10_000
    elif amount < 10_000_000:
        step = 100_000
    else:
        step = 500_000
    return int(round(amount / step) * step)


def predict_amount(
    user_id: str,
    contact_id: str,
    when: Optional[datetime] = None,
) -> Optional[dict]:
    """Return {amount: int, confidence: float, rationale: str} or None.

    Strategy (ordered):
      1. If a *similar-date* transaction for this contact exists in the
         last 6 months (day-of-month within ±3 days), use the median of
         those amounts. High confidence.
      2. Otherwise, use the median of all past transactions for this
         contact. Medium confidence.
      3. If fewer than 2 past tx, return None.

    The rationale is a short Vietnamese phrase summarising the choice so
    the confirm card can surface it verbatim.
    """
    if not user_id or not contact_id:
        return None

    reference = when or now()
    store = get_store()
    # OPT-3 (bench): push the contact + status filter into SQL. The
    # previous implementation materialised all 520k contest transactions
    # and threw away ~99.9% of them — ~16s wasted per call.
    txs = [
        t
        for t in store.transactions_of(
            user_id, contact_id=contact_id, status="completed",
        )
        if t.amount > 0
    ]

    if len(txs) < 2:
        return None

    # Strategy 1: similar-date window.
    horizon = reference - _SIMILAR_WINDOW
    target_dom = reference.day
    similar = [
        t
        for t in txs
        if t.created_at >= horizon
        and abs(t.created_at.day - target_dom) <= _DAY_OF_MONTH_TOLERANCE
    ]

    if len(similar) >= 2:
        med = int(median(t.amount for t in similar))
        rounded = _round_to_nice(med)
        rationale = (
            f"theo {len(similar)} lần bạn từng chuyển vào quanh ngày "
            f"{target_dom} hàng tháng"
        )
        return {
            "amount": rounded,
            "confidence": 0.85,
            "rationale": rationale,
        }

    # Strategy 2: overall median for this contact.
    med = int(median(t.amount for t in txs))
    rounded = _round_to_nice(med)
    rationale = f"theo mức bạn thường chuyển ({len(txs)} lần gần đây)"
    return {
        "amount": rounded,
        "confidence": 0.6,
        "rationale": rationale,
    }


__all__ = ["predict_amount"]

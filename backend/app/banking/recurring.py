"""Recurring-payment detector — mines transaction history for monthly cadence.

Used by the ``recurring`` intent ("Mình có khoản nào trả định kỳ không?")
and to seed schedule suggestions. Pure function over a list of Transactions
plus a reference timestamp — no DB or session coupling.

Algorithm (bucket-by-month, not by raw interval):

1. Group completed outgoing tx by ``(contact_id, normalized_description)``.
   Normalisation strips accents, lowercases, and collapses whitespace.
   Descriptions in the noise blacklist or shorter than 3 chars are dropped
   — the simulation dataset contains thousands of ``ok`` / ``asdf`` / ``<3``
   rows that would otherwise dominate the output.

2. For each group, project dates onto distinct ``(year, month)`` buckets.
   A pattern is *monthly recurring* when it appears in at least
   ``min_months`` distinct months AND at most one month-gap exists between
   the earliest and latest occurrence.

   Why month-buckets instead of raw 27-33 day intervals: the data is bursty
   — multiple same-day transfers ("test", duplicate sends) compress raw
   intervals to 0d. The month-bucket view treats the day as a typical
   payment date rather than a strict cron tick.

3. Output a ``RecurringPattern`` per group with:
     - ``typical_amount`` (median across all occurrences)
     - ``typical_day`` (median day-of-month)
     - ``next_run``      (month after ``last_seen`` at ``typical_day``)
     - ``confidence``    in [0, 1]; rewards month-count, penalises amount
                          variance and missing months.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ..models.schemas import Transaction

# Bucketing must happen in the user's wall-clock timezone, not UTC, or a
# tx stamped ``2026-06-01T00:30:00+00:00`` (May 31 23:30 ICT) lands in
# June instead of May and breaks the month-bucket detector at month edges.
_LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _to_local_naive(dt: datetime) -> datetime:
    """Return ``dt`` rebased to ICT wall-clock then stripped of tzinfo.

    Naive inputs are assumed to already be in ICT (legacy seed data) and
    passed through unchanged.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(_LOCAL_TZ).replace(tzinfo=None)

# Single-token chat clutter from the simulated dataset. Anything in this set
# (after lowercase + strip) is dropped before pattern detection runs.
_NOISE_DESCRIPTIONS: frozenset[str] = frozenset({
    "", "asdf", "ok", "hi", "hey", "hihi", "hehe", "<3", "...", ".", ",",
    "123", "1234", "12345", "test", "done", "ne", "nha", "qwe", "a",
    "aaaaa", "t ck", "ck", ":)", ":(", "<", ">",
    # Default chat-side descriptions. ``execute_transfer`` writes
    # "Đã chuyển" (or leaves it blank) when the user didn't supply an
    # explicit note, so three unrelated chat transfers to the same
    # contact must NOT get flagged as recurring on description alone.
    "da chuyen", "chuyen tien", "chuyen khoan", "chuyenkhoan", "transfer",
})

_MIN_DESC_LEN = 3


class RecurringPattern(BaseModel):
    """A monthly recurring outgoing payment inferred from history."""

    contact_id: str
    description: str            # canonical surface form (latest occurrence)
    typical_amount: int         # median, VND
    typical_day: int            # 1..31
    occurrence_count: int       # total tx in the pattern
    month_count: int            # distinct (year, month) buckets covered
    first_seen: datetime
    last_seen: datetime
    next_run: datetime          # inferred next occurrence
    confidence: float           # 0..1
    # Resolved by the orchestrator when surfacing to the UI — kept Optional so
    # the detector stays a pure function of (tx, ref_now) and tests don't have
    # to plumb contact lookups through.
    recipient_name: Optional[str] = None
    recipient_bank: Optional[str] = None


def _normalize(desc: str) -> str:
    """Lower + accent-fold + whitespace-collapse. Used as the grouping key."""
    if not desc:
        return ""
    s = unicodedata.normalize("NFKD", desc)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("đ", "d").replace("Đ", "D").lower()
    return " ".join(s.split())


def _safe_day_in_month(year: int, month: int, day: int) -> datetime:
    """``datetime(year, month, day)`` clamped to the last valid day —
    avoids Feb 30, Apr 31. Mirrors ``banking.service._safe_day_in_month``
    but free of the hour parameter we don't need here."""
    if month == 12:
        next_month_start = datetime(year + 1, 1, 1)
    else:
        next_month_start = datetime(year, month + 1, 1)
    days_in_month = (next_month_start - datetime(year, month, 1)).days
    return datetime(year, month, min(day, days_in_month), 9, 0)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def detect_recurring(
    transactions: list[Transaction],
    *,
    ref_now: Optional[datetime] = None,
    min_months: int = 3,
    min_occurrences: int = 3,
    max_gap_months: int = 1,
) -> list[RecurringPattern]:
    """Return monthly recurring patterns sorted by confidence desc.

    ``ref_now`` defaults to the most recent transaction date — keeps the
    detector deterministic in tests and demos where the dataset is stale.
    Patterns whose inferred ``next_run`` is more than 60 days before
    ``ref_now`` are dropped as stale (the payment line was discontinued).
    """
    completed = [t for t in transactions if t.status == "completed"]
    if not completed:
        return []
    if ref_now is None:
        ref_now = _to_local_naive(max(t.created_at for t in completed))
    else:
        ref_now = _to_local_naive(ref_now)

    # Pre-compute local wall-clock timestamps once so every downstream
    # ``.year`` / ``.month`` / ``.day`` read sees ICT, not UTC.
    local_by_tx: dict[str, datetime] = {
        t.id: _to_local_naive(t.created_at) for t in completed
    }

    groups: dict[tuple[str, str], list[Transaction]] = defaultdict(list)
    for t in completed:
        norm = _normalize(t.description)
        if len(norm) < _MIN_DESC_LEN or norm in _NOISE_DESCRIPTIONS:
            continue
        groups[(t.contact_id, norm)].append(t)

    patterns: list[RecurringPattern] = []
    for (contact_id, _norm), members in groups.items():
        if len(members) < min_occurrences:
            continue

        months = sorted({
            (local_by_tx[t.id].year, local_by_tx[t.id].month) for t in members
        })
        if len(months) < min_months:
            continue

        # Reject patterns with a gap larger than ``max_gap_months`` — e.g.
        # one tx in Jan and another in Jun is two months, not a recurring line.
        gap_ok = True
        for a, b in zip(months, months[1:]):
            gap = (b[0] - a[0]) * 12 + (b[1] - a[1])
            if gap > max_gap_months + 1:
                gap_ok = False
                break
        if not gap_ok:
            continue

        amounts = [t.amount for t in members]
        days = [local_by_tx[t.id].day for t in members]
        sorted_by_date = sorted(members, key=lambda t: local_by_tx[t.id])
        last_seen = local_by_tx[sorted_by_date[-1].id]
        first_seen = local_by_tx[sorted_by_date[0].id]

        typical_amount = int(median(amounts))
        typical_day = int(median(days))
        next_year, next_month = _next_month(last_seen.year, last_seen.month)
        next_run = _safe_day_in_month(next_year, next_month, typical_day)

        # Stale: schedule line went silent — skip suggesting it.
        if (ref_now - next_run).days > 60:
            continue

        # Confidence: more months = stronger signal; tight amount distribution
        # boosts it; coverage (months present vs months spanned) does too.
        months_spanned = (
            (months[-1][0] - months[0][0]) * 12
            + (months[-1][1] - months[0][1])
            + 1
        )
        coverage = len(months) / months_spanned if months_spanned else 1.0
        amount_mean = sum(amounts) / len(amounts)
        if amount_mean > 0:
            variance = sum((a - amount_mean) ** 2 for a in amounts) / len(amounts)
            cv = (variance ** 0.5) / amount_mean
            amount_score = max(0.0, 1.0 - min(cv, 1.0))
        else:
            amount_score = 0.0
        month_score = min(1.0, len(months) / 6.0)
        confidence = round(
            0.5 * month_score + 0.3 * coverage + 0.2 * amount_score, 3
        )

        # Canonical surface form = most recent description (preserves the
        # user-visible casing/diacritics even though we grouped on normalized).
        canonical_desc = sorted_by_date[-1].description.strip() or _norm

        patterns.append(
            RecurringPattern(
                contact_id=contact_id,
                description=canonical_desc,
                typical_amount=typical_amount,
                typical_day=typical_day,
                occurrence_count=len(members),
                month_count=len(months),
                first_seen=first_seen,
                last_seen=last_seen,
                next_run=next_run,
                confidence=confidence,
            )
        )

    patterns.sort(key=lambda p: (-p.confidence, -p.month_count, p.contact_id))
    return patterns

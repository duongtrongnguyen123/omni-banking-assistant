"""Proactive spending insights — month-over-month, anomalies, subscriptions.

Pure-stdlib analytics module. Reads transactions via the existing in-memory
`Store` (see `app/store.py` — narrow API, intentionally no SQL hooks), then
slices them into three "cross-sell ready" views per the slide deck:

1. `month_over_month` — category-level spend delta vs the previous calendar
   month. Drives a "tháng này mình tiêu nhiều hơn ở khoản X" nudge.
2. `anomalies` — z-score outliers in the recent window, per-contact baseline.
   Drives "có vẻ giao dịch này cao bất thường" callouts.
3. `subscriptions` — amount-grouped recurring patterns (different from the
   description-grouped `recurring.py` helper). Tighter for fixed-price
   monthly services like Netflix / Spotify / điện-nước.

No new pip deps — uses stdlib statistics + math only. numpy/sklearn are
available but overkill for ~hundreds of tx.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from ..store import get_store


# ----- helpers --------------------------------------------------------------


def _month_bounds(ref: datetime) -> tuple[datetime, datetime]:
    """Return [start_of_month, start_of_next_month) for `ref`."""
    start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _prev_month_bounds(ref: datetime) -> tuple[datetime, datetime]:
    start_this, _ = _month_bounds(ref)
    last_day_prev = start_this - timedelta(seconds=1)
    return _month_bounds(last_day_prev)


def _contact_name(contact_id: str) -> str:
    c = get_store().get_contact(contact_id)
    return c.display_name if c else contact_id


def _completed_tx(user_id: str):
    """Only count completed (settled) outgoing tx — pending/cancelled would
    skew both totals and anomaly baselines."""
    return [t for t in get_store().transactions_of(user_id) if t.status == "completed"]


# ----- 1. month-over-month --------------------------------------------------


def month_over_month(user_id: str, when: datetime) -> dict:
    """Compare this-month vs last-month totals per category.

    Returns ``{category: {this: int, last: int, delta_pct: float}}``.

    `delta_pct` is the relative change vs last month, expressed as a percent.
    Special cases:
      - last == 0 and this  > 0  -> 100.0 (treat as "new spend in this cat")
      - last == 0 and this == 0 -> 0.0  (not reported anyway)
    """
    this_start, this_end = _month_bounds(when)
    last_start, last_end = _prev_month_bounds(when)

    txs = _completed_tx(user_id)

    by_cat_this: dict[str, int] = defaultdict(int)
    by_cat_last: dict[str, int] = defaultdict(int)

    for t in txs:
        if this_start <= t.created_at < this_end:
            by_cat_this[t.category] += t.amount
        elif last_start <= t.created_at < last_end:
            by_cat_last[t.category] += t.amount

    categories = set(by_cat_this) | set(by_cat_last)
    result: dict[str, dict] = {}
    for cat in categories:
        this = by_cat_this.get(cat, 0)
        last = by_cat_last.get(cat, 0)
        if last == 0 and this == 0:
            continue
        if last == 0:
            delta_pct = 100.0
        else:
            delta_pct = round((this - last) / last * 100.0, 1)
        result[cat] = {"this": this, "last": last, "delta_pct": delta_pct}
    return result


# ----- 2. anomalies ---------------------------------------------------------


def anomalies(
    user_id: str, when: datetime, window_days: int = 30
) -> list[dict]:
    """Flag transactions in the last `window_days` whose amount exceeds
    z-score 2.5 vs the user's per-contact mean.

    "Per-contact" is the right baseline here: a 2M tx is normal if you
    routinely send 2M to mẹ but anomalous to a friend you usually send 200k.

    Falls back to the per-user mean for contacts with <3 historical tx
    (statistically meaningless std otherwise). Returns up to 10 most
    surprising items, sorted by z-score desc.
    """
    txs = _completed_tx(user_id)
    if not txs:
        return []

    window_start = when - timedelta(days=window_days)

    # Build per-contact baseline from ALL history (not just window).
    by_contact: dict[str, list[int]] = defaultdict(list)
    for t in txs:
        by_contact[t.contact_id].append(t.amount)

    # Per-user fallback baseline.
    all_amounts = [t.amount for t in txs]
    user_mean = statistics.fmean(all_amounts)
    user_std = statistics.pstdev(all_amounts) if len(all_amounts) > 1 else 0.0

    scored: list[dict] = []
    for t in txs:
        if t.created_at < window_start:
            continue
        peers = by_contact[t.contact_id]
        if len(peers) >= 3:
            mu = statistics.fmean(peers)
            sigma = statistics.pstdev(peers)
            baseline = "per-contact"
        else:
            mu = user_mean
            sigma = user_std
            baseline = "per-user"

        if sigma <= 0:
            # Constant history — flag only if this tx differs at all and is
            # the FIRST oddity. Use a soft z proxy.
            if t.amount > mu:
                z = float("inf")
            else:
                continue
        else:
            z = (t.amount - mu) / sigma

        if z < 2.5 or not math.isfinite(z) and z != float("inf"):
            # Filter out everything below threshold; keep +inf for novelty.
            if z != float("inf"):
                continue

        ratio = (t.amount / mu) if mu > 0 else float("inf")
        scored.append(
            {
                "tx_id": t.id,
                "amount": t.amount,
                "contact_name": _contact_name(t.contact_id),
                "z_score": round(z, 2) if math.isfinite(z) else 99.0,
                "reason": (
                    f"cao gấp {ratio:.1f} lần mức thường ({baseline})"
                    if math.isfinite(ratio)
                    else "giao dịch lớn đầu tiên cho người này"
                ),
            }
        )

    scored.sort(key=lambda x: x["z_score"], reverse=True)
    return scored[:10]


# ----- 3. subscriptions -----------------------------------------------------


def subscriptions(user_id: str, min_occurrences: int = 3) -> list[dict]:
    """Pattern-mine recurring small amounts that look like subscriptions.

    Groups by (contact, amount-bucket) where bucket = amount rounded to
    nearest 10k VND, then re-checks each candidate group is within ±10% of
    the typical amount and cadence is roughly monthly (≈20-40 days between
    consecutive charges).

    Different from `banking/recurring.py` (if/when added — description-grouped
    aggregator): this one is amount-anchored and intentionally narrow so it
    catches Netflix / Spotify / điện-nước style fixed-price charges and
    skips one-off transfers that happen to share a description.
    """
    txs = sorted(_completed_tx(user_id), key=lambda t: t.created_at)
    if len(txs) < min_occurrences:
        return []

    # Bucket key = (contact_id, amount rounded to nearest 10k).
    buckets: dict[tuple[str, int], list] = defaultdict(list)
    for t in txs:
        bucket_amount = int(round(t.amount / 10_000) * 10_000) or t.amount
        buckets[(t.contact_id, bucket_amount)].append(t)

    results: list[dict] = []
    for (contact_id, _bucket), group in buckets.items():
        if len(group) < min_occurrences:
            continue

        amounts = [t.amount for t in group]
        typical = int(statistics.median(amounts))
        if typical <= 0:
            continue

        # ±10% tightness check — drops one-off scatter from the same contact.
        in_band = [a for a in amounts if abs(a - typical) / typical <= 0.10]
        if len(in_band) < min_occurrences:
            continue

        # Cadence: median gap between consecutive charges in days.
        gaps = [
            (group[i].created_at - group[i - 1].created_at).days
            for i in range(1, len(group))
        ]
        if not gaps:
            continue
        median_gap = statistics.median(gaps)
        if not (20 <= median_gap <= 40):
            continue

        last_seen = max(t.created_at for t in group)
        results.append(
            {
                "contact": _contact_name(contact_id),
                "contact_id": contact_id,
                "typical_amount": typical,
                "occurrences": len(group),
                "last_seen": last_seen.isoformat(),
                "median_gap_days": int(median_gap),
            }
        )

    results.sort(key=lambda r: (-r["occurrences"], -r["typical_amount"]))
    return results


# ----- 4. month-end forecast ------------------------------------------------


def forecast(user_id: str, when: datetime) -> Optional[dict]:
    """Project month-end spend & remaining balance from the running rate.

    Returns ``None`` when the data is too thin to project — ≤3 days into
    the month (daily rate is noise) or zero spend so far. Output fields:
      - ``days_elapsed`` / ``days_in_month``
      - ``spent_so_far``
      - ``daily_rate`` (spent_so_far / days_elapsed, integer VND)
      - ``projected_total`` (daily_rate × days_in_month)
      - ``projected_remaining_spend`` (projected_total − spent_so_far, ≥0)
      - ``current_balance`` (sum across all accounts)
      - ``projected_eom_balance`` (current − projected_remaining_spend)
      - ``last_month_total`` (sanity baseline; 0 if unknown)
      - ``pace_vs_last_month`` projected_total / last_month_total; None
                               when last_month_total == 0
    """
    from ..store import get_store

    this_start, this_end = _month_bounds(when)
    days_in_month = (this_end - this_start).days
    days_elapsed = max(1, (when - this_start).days + 1)
    if days_elapsed < 3:
        return None

    txs = _completed_tx(user_id)
    spent_so_far = sum(
        t.amount for t in txs if this_start <= t.created_at < this_end
    )
    if spent_so_far <= 0:
        return None

    last_start, last_end = _prev_month_bounds(when)
    last_month_total = sum(
        t.amount for t in txs if last_start <= t.created_at < last_end
    )

    daily_rate = spent_so_far // days_elapsed
    projected_total = daily_rate * days_in_month
    projected_remaining_spend = max(0, projected_total - spent_so_far)

    store = get_store()
    user = store.get_user_or_none(user_id)
    current_balance = sum(a.balance for a in user.accounts) if user else 0
    projected_eom_balance = current_balance - projected_remaining_spend

    pace_ratio: Optional[float] = None
    over_budget = False
    under_budget = False
    if last_month_total > 0:
        pace_ratio = round(projected_total / last_month_total, 2)
        # ±20% band around last month's total — tight enough to flag a real
        # change of habit, loose enough that month-length variance (29 vs 31
        # days) doesn't fire on its own.
        over_budget = pace_ratio >= 1.20
        under_budget = pace_ratio <= 0.80

    overdraft_risk = projected_eom_balance < 0

    return {
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "spent_so_far": int(spent_so_far),
        "daily_rate": int(daily_rate),
        "projected_total": int(projected_total),
        "projected_remaining_spend": int(projected_remaining_spend),
        "current_balance": int(current_balance),
        "projected_eom_balance": int(projected_eom_balance),
        "last_month_total": int(last_month_total),
        "pace_vs_last_month": pace_ratio,
        "over_budget": over_budget,
        "under_budget": under_budget,
        "overdraft_risk": overdraft_risk,
    }


# ----- aggregate ------------------------------------------------------------


def summary(user_id: str, when: Optional[datetime] = None) -> dict:
    """Convenience wrapper used by the /api/insights/summary route."""
    if when is None:
        from ..store import now as _now

        when = _now()
    return {
        "mom": month_over_month(user_id, when),
        "anomalies": anomalies(user_id, when),
        "subscriptions": subscriptions(user_id),
        "forecast": forecast(user_id, when),
        "generated_at": when.isoformat(),
    }

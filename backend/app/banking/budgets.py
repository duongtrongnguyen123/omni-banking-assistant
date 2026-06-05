"""Budget status aggregation.

Read-only computation: for every budget the user has set, sum this
month's outgoing transactions that match the budget's category and
compare against the limit. The Vietnamese label is resolved via the
same mapping the NLU layer uses so the UI displays a consistent
string regardless of which surface form ("ăn uống" / "an uong") the
user typed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..models.schemas import BudgetStatus, Transaction
from ..nlp.budget_entities import _BUDGET_CATEGORIES
from ..store import get_store

# Build a code → label map once at import time. The first entry per
# code wins, which keeps the labels stable even when the keyword table
# grows.
_CODE_TO_LABEL: dict[str, str] = {}
for _kw, _code, _label in _BUDGET_CATEGORIES:
    _CODE_TO_LABEL.setdefault(_code, _label)


def label_for(code: str) -> str:
    """Vietnamese display label for an internal category code.

    Falls back to a capitalised version of the code so unknown
    categories still render something readable. The orchestrator only
    ever surfaces codes that came out of the categorizer or the
    budget_entities mapping, so the fallback rarely fires in practice.
    """
    return _CODE_TO_LABEL.get(code, code.replace("_", " ").capitalize())


def _month_window(ref: datetime) -> tuple[datetime, datetime]:
    start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _spent_this_month(
    txs: list[Transaction], category: str, ref_now: datetime
) -> int:
    """Sum positive (outgoing) tx amounts for ``category`` in the
    current month. Uncategorised "omni" transfers are excluded so
    user-defined budgets track real spending categories — chat
    transfers will still appear under whatever code the categorizer
    assigns them at execute time.
    """
    start, end = _month_window(ref_now)
    total = 0
    for t in txs:
        if t.category != category:
            continue
        if t.status != "completed":
            continue
        if t.created_at < start or t.created_at >= end:
            continue
        if t.amount > 0:
            total += t.amount
    return total


def compute_statuses(
    user_id: str, ref_now: Optional[datetime] = None
) -> list[BudgetStatus]:
    """Return one BudgetStatus per budget the user has set."""
    from ..store import now as _now

    ref = ref_now or _now()
    store = get_store()
    budgets = store.budgets_of(user_id)
    if not budgets:
        return []

    txs = store.transactions_of(user_id)
    out: list[BudgetStatus] = []
    for b in budgets:
        spent = _spent_this_month(txs, b.category, ref)
        remaining = b.monthly_limit_vnd - spent
        ratio = spent / b.monthly_limit_vnd if b.monthly_limit_vnd > 0 else 0.0
        out.append(
            BudgetStatus(
                category=b.category,
                category_label=label_for(b.category),
                monthly_limit_vnd=b.monthly_limit_vnd,
                spent_vnd=spent,
                remaining_vnd=remaining,
                ratio=round(ratio, 3),
            )
        )
    return out


def compute_status_for(
    user_id: str, category: str, ref_now: Optional[datetime] = None
) -> Optional[BudgetStatus]:
    for s in compute_statuses(user_id, ref_now=ref_now):
        if s.category == category:
            return s
    return None


__all__ = ["compute_statuses", "compute_status_for", "label_for"]

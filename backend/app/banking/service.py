"""Mock banking operations. Stands in for a real core-banking integration."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean
from typing import Optional

from ..models.schemas import Contact, Schedule, Transaction
from ..store import get_store, new_id, now


def execute_transfer(
    *,
    user_id: str,
    recipient: Contact,
    amount: int,
    description: str = "",
) -> Transaction:
    store = get_store()
    acc = store.primary_account(user_id)
    if amount > acc.balance:
        raise ValueError("insufficient_balance")
    store.update_balance(user_id, acc.id, -amount)
    tx = Transaction(
        id=new_id("t"),
        owner_id=user_id,
        contact_id=recipient.id,
        amount=amount,
        description=description or "Chuyển khoản",
        category="omni",
        status="completed",
        created_at=now(),
    )
    return store.add_transaction(tx)


def get_balance(user_id: str) -> dict:
    store = get_store()
    user = store.get_user(user_id)
    return {
        "display_name": user.display_name,
        "accounts": [a.model_dump() for a in user.accounts],
        "total": sum(a.balance for a in user.accounts),
    }


def _month_window(ref: datetime) -> tuple[datetime, datetime]:
    start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def get_history(
    *,
    user_id: str,
    contact_id: Optional[str] = None,
    period: str = "this_month",
) -> dict:
    store = get_store()
    txs = store.transactions_of(user_id)
    if not txs:
        return {"period": period, "count": 0, "total": 0, "items": []}

    ref_now = now()
    if period == "this_month":
        start, end = _month_window(ref_now)
    elif period == "last_month":
        prev = ref_now.replace(day=1) - timedelta(days=1)
        start, end = _month_window(prev)
    else:
        start = ref_now - timedelta(days=30)
        end = ref_now + timedelta(days=1)

    items = [t for t in txs if start <= t.created_at < end]
    if contact_id:
        items = [t for t in items if t.contact_id == contact_id]

    categories: dict[str, int] = defaultdict(int)
    by_recipient: dict[str, int] = defaultdict(int)
    for t in items:
        categories[t.category] += t.amount
        c = _contact_summary(t.contact_id)
        if c.get("display_name"):
            by_recipient[c["display_name"]] += t.amount

    return {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": len(items),
        "total": sum(t.amount for t in items),
        "items": [
            {
                **t.model_dump(mode="json"),
                "contact": _contact_summary(t.contact_id),
            }
            for t in items
        ],
        "average": int(mean([t.amount for t in items])) if items else 0,
        "by_category": dict(categories),
        "by_recipient": dict(by_recipient),
    }


def _contact_summary(contact_id: str) -> dict:
    store = get_store()
    c = store.contacts.get(contact_id)
    if not c:
        return {}
    return {
        "id": c.id,
        "display_name": c.display_name,
        "bank": c.bank,
        "account_masked": c.account_masked,
        "label": c.label,
    }


def create_schedule(
    *,
    user_id: str,
    recipient: Contact,
    amount: int,
    cron: str,
    description: str = "",
) -> Schedule:
    store = get_store()
    sched = Schedule(
        id=new_id("s"),
        owner_id=user_id,
        contact_id=recipient.id,
        amount=amount,
        description=description,
        cron=cron,
        next_run=_next_run_for(cron, now()),
        active=True,
    )
    return store.add_schedule(sched)


def _next_run_for(cron: str, ref: datetime) -> datetime:
    """Compute next run for a tiny subset of cron we use."""
    # Supported forms (set by NLP entity extractor):
    #   "0 9 1 * *"   -> 9am on day-1 of each month
    #   "0 9 D * *"   -> 9am on day-D of each month
    #   "0 9 * * 1"   -> 9am every Monday
    parts = cron.split()
    if len(parts) != 5:
        return ref + timedelta(days=30)
    _, hour, dom, _, dow = parts
    h = int(hour) if hour.isdigit() else 9

    if dom.isdigit():
        day = int(dom)
        candidate = ref.replace(day=min(day, 28), hour=h, minute=0, second=0, microsecond=0)
        if candidate <= ref:
            month = candidate.month + 1
            year = candidate.year + (1 if month > 12 else 0)
            month = ((month - 1) % 12) + 1
            candidate = candidate.replace(year=year, month=month)
        return candidate

    if dow.isdigit():
        target = int(dow) % 7
        days_ahead = (target - ref.weekday()) % 7
        days_ahead = days_ahead or 7
        return (ref + timedelta(days=days_ahead)).replace(hour=h, minute=0, second=0, microsecond=0)

    return ref + timedelta(days=30)

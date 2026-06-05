from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from ..banking.service import get_balance, get_history
from ..store import get_store
from .deps import current_user

router = APIRouter(prefix="/api", tags=["banking"])


@router.get("/me")
def me(user_id: str = Depends(current_user)):
    user = get_store().get_user_or_none(user_id)
    if user is None:
        return {"id": user_id, "display_name": user_id, "accounts": [], "phone": ""}
    return user.model_dump()


@router.get("/contacts")
def contacts(user_id: str = Depends(current_user)):
    return [c.model_dump() for c in get_store().contacts_of(user_id)]


@router.get("/transactions")
def transactions(user_id: str = Depends(current_user), limit: int = 50):
    txs = get_store().transactions_of(user_id)[:limit]
    return [
        {**t.model_dump(mode="json"), "contact": _summary(t.contact_id)}
        for t in txs
    ]


@router.get("/balance")
def balance(user_id: str = Depends(current_user)):
    return get_balance(user_id)


@router.get("/history")
def history(
    user_id: str = Depends(current_user),
    contact_id: Optional[str] = None,
    period: str = "this_month",
):
    return get_history(user_id=user_id, contact_id=contact_id, period=period)


@router.get("/schedules")
def schedules(user_id: str = Depends(current_user)):
    return [s.model_dump(mode="json") for s in get_store().schedules_of(user_id)]


def _summary(contact_id: str) -> dict:
    c = get_store().contacts.get(contact_id)
    if not c:
        return {}
    return {
        "id": c.id,
        "display_name": c.display_name,
        "bank": c.bank,
        "account_masked": c.account_masked,
        "label": c.label,
    }

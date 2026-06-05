from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..banking.dedup import find_duplicate_groups, merge_contacts
from ..banking.service import get_balance, get_history
from ..store import get_store
from .deps import current_user

router = APIRouter(prefix="/api", tags=["banking"])


class MergeContactsRequest(BaseModel):
    primary_id: str
    candidate_ids: list[str] = Field(default_factory=list)


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


@router.get("/audit")
def audit(user_id: str = Depends(current_user), limit: int = 100):
    return [e.model_dump(mode="json") for e in get_store().audit_of(user_id, limit)]


@router.get("/contacts/duplicates")
def contact_duplicates(user_id: str = Depends(current_user)):
    """Detect contact duplicates the user would merge without a second
    thought. See `banking/dedup.py` for the rule. Per-contact transaction
    counts and totals are joined in to power the side-by-side preview."""
    store = get_store()
    contacts = store.contacts_of(user_id)
    groups = find_duplicate_groups(contacts)

    # Per-contact tx aggregates for the merge preview UI.
    tx_counts: dict[str, int] = {}
    tx_totals: dict[str, int] = {}
    for t in store.transactions_of(user_id):
        tx_counts[t.contact_id] = tx_counts.get(t.contact_id, 0) + 1
        tx_totals[t.contact_id] = tx_totals.get(t.contact_id, 0) + t.amount

    def enrich(c) -> dict:
        return {
            **c.model_dump(),
            "tx_count": tx_counts.get(c.id, 0),
            "tx_total": tx_totals.get(c.id, 0),
        }

    return [
        {
            "primary": enrich(g.primary),
            "candidates": [enrich(c) for c in g.candidates],
            "reason": g.reason,
            "overlap": g.overlap,
        }
        for g in groups
    ]


@router.post("/contacts/merge")
def merge_contacts_endpoint(
    body: MergeContactsRequest,
    user_id: str = Depends(current_user),
):
    """Merge candidate contacts into a primary contact.

    Steps (atomic, under the store lock):
      1. Validate all contacts belong to this user.
      2. Re-attribute every transaction with contact_id in candidate_ids
         to primary_id.
      3. Merge `aliases` (deduped union; candidate display_names are
         folded in so the merged contact can still be found by old name).
      4. Hard-delete candidates.

    Returns: {merged_tx_count, retained_aliases, audit}. The `audit`
    block snapshots pre-merge candidates so the merge can be reconstructed
    manually if needed (no automatic undo).
    """
    store = get_store()
    primary = store.contacts.get(body.primary_id)
    if primary is None or primary.owner_id != user_id:
        raise HTTPException(404, "primary contact not found")

    cleaned: list[str] = []
    for cid in body.candidate_ids:
        if cid == body.primary_id:
            continue
        c = store.contacts.get(cid)
        if c is None or c.owner_id != user_id:
            raise HTTPException(404, f"candidate {cid} not found")
        cleaned.append(cid)

    if not cleaned:
        raise HTTPException(400, "no candidates to merge")

    # SQLite-style atomic merge: hold the store lock for the duration so
    # tx re-attribution + contact deletion are observed as one step.
    with store._lock:
        result = merge_contacts(
            store.contacts,
            store.transactions,
            primary_id=body.primary_id,
            candidate_ids=cleaned,
        )
    return result


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

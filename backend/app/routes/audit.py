"""Audit + explainability routes.

These endpoints power the forensic / "why did Omni do X?" view in the
UI. They are intentionally separate from `routes/banking.py:audit` (which
returns the raw audit list) so the explain payload can grow without
breaking existing callers.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..services.explain import build_explanation, find_audit_event
from ..store import get_store
from .deps import current_user

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _summarise_for_list(event_dict: dict) -> str:
    """Short one-liner for the list view — mirrors explain.summary but
    operates on the dumped dict so we don't need to re-instantiate."""
    intent = event_dict.get("intent", "unknown")
    decision = event_dict.get("decision", "unknown")
    who = event_dict.get("resolved_recipient")
    amount = (event_dict.get("entities") or {}).get("amount")
    if intent == "transfer" and who:
        amt = (
            f"{amount:,}đ".replace(",", ".") if isinstance(amount, int) else "?đ"
        )
        return f"{intent} · {amt} → {who} · {decision}"
    return f"{intent} · {decision}"


@router.get("/last")
def last_events(
    user_id: str = Depends(current_user), limit: int = 20
) -> list[dict]:
    """Return the most recent audit events for the calling user.

    Newest-first. Each row is enriched with a one-line `summary` so the UI
    can render a list without round-tripping every entry through
    `/explain`.
    """
    limit = max(1, min(limit, 200))
    rows = get_store().audit_of(user_id, limit)
    out: list[dict] = []
    for ev in rows:
        row = ev.model_dump(mode="json")
        row["summary"] = _summarise_for_list(row)
        out.append(row)
    return out


@router.get("/{audit_id}/explain")
def explain(audit_id: str, user_id: str = Depends(current_user)) -> dict:
    event = find_audit_event(user_id, audit_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Audit event not found.")
    return build_explanation(event)

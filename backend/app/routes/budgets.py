"""REST endpoints for budgets + savings goals.

Confirm flows in chat go via the orchestrator (``confirm_budget_draft``,
``confirm_goal_draft``). These endpoints are for the sidebar widgets
to read live state + power direct CRUD when the user clicks the gear
icon.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..banking.budgets import compute_statuses, label_for
from ..models.schemas import Budget, SavingsGoal
from ..services.orchestrator import (
    cancel_budget_draft,
    cancel_goal_draft,
    confirm_budget_draft,
    confirm_goal_draft,
)
from ..store import get_store, new_id, now
from .deps import current_user

router = APIRouter(prefix="/api", tags=["budgets"])


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


class BudgetCreate(BaseModel):
    """Direct REST create. ``category`` must already be the internal
    code ("food" / "transport" / …) — the chat path resolves
    Vietnamese surface forms; this endpoint expects the caller (UI)
    to pass codes."""

    category: str = Field(..., min_length=1, max_length=64)
    monthly_limit_vnd: int = Field(..., gt=0)


class BudgetUpdate(BaseModel):
    monthly_limit_vnd: int = Field(..., gt=0)


@router.get("/budgets")
def list_budgets(user_id: str = Depends(current_user)) -> list[dict]:
    statuses = {s.category: s for s in compute_statuses(user_id)}
    out = []
    for b in get_store().budgets_of(user_id):
        s = statuses.get(b.category)
        item = {
            **b.model_dump(mode="json"),
            "category_label": label_for(b.category),
            "spent_vnd": s.spent_vnd if s else 0,
            "remaining_vnd": s.remaining_vnd if s else b.monthly_limit_vnd,
            "ratio": s.ratio if s else 0.0,
        }
        out.append(item)
    return out


@router.post("/budgets")
def create_budget(
    body: BudgetCreate, user_id: str = Depends(current_user)
) -> dict:
    budget = Budget(
        id=new_id("b"),
        user_id=user_id,
        category=body.category,
        monthly_limit_vnd=body.monthly_limit_vnd,
        created_at=now(),
    )
    saved = get_store().add_budget(budget)
    return {
        **saved.model_dump(mode="json"),
        "category_label": label_for(saved.category),
    }


@router.put("/budgets/{budget_id}")
def update_budget(
    budget_id: str,
    body: BudgetUpdate,
    user_id: str = Depends(current_user),
) -> dict:
    updated = get_store().update_budget(budget_id, body.monthly_limit_vnd)
    if updated is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy ngân sách.")
    if updated.user_id != user_id:
        raise HTTPException(status_code=403, detail="Không có quyền sửa ngân sách này.")
    return {
        **updated.model_dump(mode="json"),
        "category_label": label_for(updated.category),
    }


@router.delete("/budgets/{budget_id}")
def delete_budget(
    budget_id: str, user_id: str = Depends(current_user)
) -> dict:
    # Owner check before delete so we don't silently nuke another user's
    # row (matters once we go past single-user demo).
    existing = next(
        (b for b in get_store().budgets_of(user_id) if b.id == budget_id),
        None,
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy ngân sách.")
    get_store().delete_budget(budget_id)
    return {"ok": True}


@router.post("/budgets/{draft_id}/confirm")
def chat_confirm_budget(
    draft_id: str, user_id: str = Depends(current_user)
) -> dict:
    return confirm_budget_draft(user_id, draft_id).model_dump(mode="json")


@router.post("/budgets/{draft_id}/cancel")
def chat_cancel_budget(
    draft_id: str, user_id: str = Depends(current_user)
) -> dict:
    return cancel_budget_draft(user_id, draft_id).model_dump(mode="json")


@router.get("/budgets/status")
def budget_status(user_id: str = Depends(current_user)) -> list[dict]:
    """Live snapshot of every budget vs this month's spend.

    Cheap to recompute on every request (one transactions_of() + a
    linear scan); the sidebar widget polls this on focus.
    """
    return [s.model_dump(mode="json") for s in compute_statuses(user_id)]


# ---------------------------------------------------------------------------
# Savings goals
# ---------------------------------------------------------------------------


class GoalCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    target_vnd: int = Field(..., gt=0)
    deadline: Optional[str] = None  # ISO date string


class GoalContribute(BaseModel):
    amount: int = Field(..., gt=0)


@router.get("/goals")
def list_goals(user_id: str = Depends(current_user)) -> list[dict]:
    return [g.model_dump(mode="json") for g in get_store().goals_of(user_id)]


@router.post("/goals")
def create_goal(
    body: GoalCreate, user_id: str = Depends(current_user)
) -> dict:
    goal = SavingsGoal(
        id=new_id("g"),
        user_id=user_id,
        name=body.name,
        target_vnd=body.target_vnd,
        current_vnd=0,
        deadline=body.deadline,
        created_at=now(),
    )
    saved = get_store().add_goal(goal)
    return saved.model_dump(mode="json")


@router.post("/goals/{goal_id}/contribute")
def contribute_goal(
    goal_id: str,
    body: GoalContribute,
    user_id: str = Depends(current_user),
) -> dict:
    existing = get_store().get_goal(goal_id)
    if existing is None or existing.user_id != user_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy mục tiêu.")
    try:
        updated = get_store().contribute_to_goal(goal_id, body.amount)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return updated.model_dump(mode="json")


@router.post("/goals/{draft_id}/confirm")
def chat_confirm_goal(
    draft_id: str, user_id: str = Depends(current_user)
) -> dict:
    return confirm_goal_draft(user_id, draft_id).model_dump(mode="json")


@router.post("/goals/{draft_id}/cancel")
def chat_cancel_goal(
    draft_id: str, user_id: str = Depends(current_user)
) -> dict:
    return cancel_goal_draft(user_id, draft_id).model_dump(mode="json")

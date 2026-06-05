"""Savings goal tests — partial contributions + over-target rejection
+ orchestrator routing."""

from __future__ import annotations

import pytest

from app.models.schemas import SavingsGoal
from app.nlp.pipeline import understand
from app.services.orchestrator import handle_message
from app.store import get_store, new_id, now


USER = "u_an"


@pytest.fixture(autouse=True)
def _clean_goals():
    get_store()  # ensure bootstrap has run
    from app.db.connection import get_connection

    conn = get_connection()
    conn.execute("DELETE FROM savings_goals")
    conn.execute("DELETE FROM budgets")
    from app.services import orchestrator as _o

    with _o._drafts_lock:
        _o._goal_drafts.clear()
        _o._budget_drafts.clear()
    yield
    conn.execute("DELETE FROM savings_goals")
    conn.execute("DELETE FROM budgets")


def _make_goal(name: str = "Tết 2027", target: int = 50_000_000) -> SavingsGoal:
    return SavingsGoal(
        id=new_id("g"),
        user_id=USER,
        name=name,
        target_vnd=target,
        current_vnd=0,
        deadline=None,
        created_at=now(),
    )


def test_add_and_list_goals():
    s = get_store()
    s.add_goal(_make_goal("Tết 2027", 50_000_000))
    s.add_goal(_make_goal("Mua xe", 200_000_000))
    rows = s.goals_of(USER)
    assert {g.name for g in rows} == {"Tết 2027", "Mua xe"}


def test_partial_contribute_increments_running_total():
    s = get_store()
    goal = s.add_goal(_make_goal("Tết 2027", 50_000_000))
    updated = s.contribute_to_goal(goal.id, 5_000_000)
    assert updated.current_vnd == 5_000_000
    updated = s.contribute_to_goal(goal.id, 3_000_000)
    assert updated.current_vnd == 8_000_000


def test_over_target_contribution_is_rejected():
    s = get_store()
    goal = s.add_goal(_make_goal("Tết 2027", 10_000_000))
    s.contribute_to_goal(goal.id, 6_000_000)
    with pytest.raises(ValueError):
        # 6M already in; +5M would push past 10M target.
        s.contribute_to_goal(goal.id, 5_000_000)
    # State unchanged after the rejected attempt.
    refreshed = s.get_goal(goal.id)
    assert refreshed.current_vnd == 6_000_000


def test_zero_or_negative_contribution_rejected():
    s = get_store()
    goal = s.add_goal(_make_goal())
    with pytest.raises(ValueError):
        s.contribute_to_goal(goal.id, 0)
    with pytest.raises(ValueError):
        s.contribute_to_goal(goal.id, -1)


def test_contribute_to_unknown_goal_raises():
    with pytest.raises(KeyError):
        get_store().contribute_to_goal("g_nope", 1_000)


def test_split_contribution_pattern():
    """Goals support PARTIAL contributions — the same chunk of money can
    be split across savings and spending. We model this by allowing
    multiple small contributions to add up to (but not exceed) the
    target."""
    s = get_store()
    goal = s.add_goal(_make_goal("Mua xe", 10_000_000))
    # Split a 5M chunk: 2M to goal, the rest would go to a transfer.
    s.contribute_to_goal(goal.id, 2_000_000)
    s.contribute_to_goal(goal.id, 3_000_000)
    s.contribute_to_goal(goal.id, 5_000_000)
    final = s.get_goal(goal.id)
    assert final.current_vnd == final.target_vnd == 10_000_000


# ---------------------------------------------------------------------------
# NLU + orchestrator
# ---------------------------------------------------------------------------


def test_nlu_routes_set_goal():
    r = understand("mục tiêu tiết kiệm Tết 50 triệu")
    assert r.intent == "set_goal"
    assert r.entities.amount == 50_000_000
    # Goal name should pick up "Tết" — exact form varies depending on
    # whether the extractor stripped the anchor word.
    assert r.entities.goal_name is not None
    assert "Tết" in r.entities.goal_name or "Tet" in r.entities.goal_name


def test_orchestrator_set_goal_creates_draft_and_persists_on_confirm():
    resp = handle_message(USER, "mục tiêu tiết kiệm Tết 50 triệu")
    assert resp.intent == "set_goal"
    assert resp.goal_draft is not None
    assert resp.goal_draft.target_vnd == 50_000_000

    confirm = handle_message(USER, "xác nhận")
    assert confirm.intent == "set_goal"
    rows = get_store().goals_of(USER)
    assert len(rows) == 1
    assert rows[0].target_vnd == 50_000_000


def test_orchestrator_set_goal_cancel_does_not_persist():
    handle_message(USER, "mục tiêu tiết kiệm Tết 50 triệu")
    cancel = handle_message(USER, "huỷ")
    assert "huỷ" in cancel.text.lower()
    assert get_store().goals_of(USER) == []


def test_orchestrator_set_goal_without_amount_asks_for_one():
    """No amount in the message → handler responds with a clarifying
    question rather than crashing or staging an empty draft."""
    resp = handle_message(USER, "đặt mục tiêu tiết kiệm Tết")
    # Either the LLM-disabled fallback finds set_goal and asks for the
    # amount, or it routes to unknown. Neither path may surface a
    # goal_draft.
    assert resp.goal_draft is None
    assert get_store().goals_of(USER) == []

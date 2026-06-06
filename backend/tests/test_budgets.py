"""Budget envelope CRUD + NLU routing tests.

The store layer + NLU layer are tested directly (no FastAPI client) so
the suite stays fast and doesn't depend on LLM credentials. The
``conftest.py`` bootstrap already empties Groq/Gemini keys and points
the SQLite store at a temp dir.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.banking.budgets import compute_statuses, label_for
from app.models.schemas import Budget, Transaction
from app.nlp.pipeline import understand
from app.services.orchestrator import handle_message
from app.store import get_store, new_id, now


def _conn():
    """Lazy connection accessor. Calling ``get_store()`` first guarantees
    the bootstrap has run before we issue raw DELETEs."""
    get_store()
    from app.db.connection import get_connection

    return get_connection()


USER = "u_an"


@pytest.fixture(autouse=True)
def _clean_budgets():
    """Only touch our own tables (``budgets`` / ``savings_goals``) so
    the suite stays hermetic w.r.t. other modules that depend on the
    bootstrap seed.

    The orchestrator's in-process budget-draft stash is also reset so
    a stale draft from a previous test can't leak through.
    """
    conn = _conn()
    conn.execute("DELETE FROM budgets")
    conn.execute("DELETE FROM savings_goals")
    from app.services import orchestrator as _o

    with _o._drafts_lock:
        _o._budget_drafts.clear()
        _o._goal_drafts.clear()
    yield
    conn.execute("DELETE FROM budgets")
    conn.execute("DELETE FROM savings_goals")


# ---------------------------------------------------------------------------
# Store CRUD
# ---------------------------------------------------------------------------


def _make_budget(category: str, limit: int) -> Budget:
    return Budget(
        id=new_id("b"),
        user_id=USER,
        category=category,
        monthly_limit_vnd=limit,
        created_at=now(),
    )


def test_add_and_list_budgets():
    s = get_store()
    s.add_budget(_make_budget("food", 3_000_000))
    s.add_budget(_make_budget("transport", 1_000_000))
    rows = s.budgets_of(USER)
    assert {b.category for b in rows} == {"food", "transport"}


def test_add_budget_upserts_when_category_repeats():
    """Repeating "đặt ngân sách ăn uống" must overwrite, not duplicate
    — the chat flow surfaces this as "cập nhật"."""
    s = get_store()
    s.add_budget(_make_budget("food", 3_000_000))
    s.add_budget(_make_budget("food", 4_500_000))
    rows = s.budgets_of(USER)
    assert len(rows) == 1
    assert rows[0].monthly_limit_vnd == 4_500_000


def test_update_budget_changes_only_limit():
    s = get_store()
    s.add_budget(_make_budget("food", 3_000_000))
    bid = s.budgets_of(USER)[0].id
    updated = s.update_budget(bid, 5_000_000)
    assert updated is not None
    assert updated.monthly_limit_vnd == 5_000_000
    assert updated.category == "food"


def test_update_unknown_budget_returns_none():
    assert get_store().update_budget("b_nope", 1_000_000) is None


def test_delete_budget():
    s = get_store()
    s.add_budget(_make_budget("food", 3_000_000))
    bid = s.budgets_of(USER)[0].id
    assert s.delete_budget(bid) is True
    assert s.budgets_of(USER) == []
    assert s.delete_budget(bid) is False  # already gone


# ---------------------------------------------------------------------------
# Status aggregation
#
# Uses a synthetic category code "budget_test" that the bootstrap seed
# never produces, so the per-category spend numbers stay independent of
# what other tests may have inserted.
# ---------------------------------------------------------------------------


_TEST_CAT = "budget_test_isolated"


def _add_tx(amount: int, days_ago: int = 0) -> None:
    ref = now() - timedelta(days=days_ago)
    tx = Transaction(
        id=new_id("t"),
        owner_id=USER,
        contact_id="",
        amount=amount,
        description="seed",
        category=_TEST_CAT,
        status="completed",
        created_at=ref,
    )
    get_store().add_transaction(tx)


def _wipe_test_cat_tx() -> None:
    """Drop only the transactions we inserted under ``_TEST_CAT``. This
    keeps the bootstrap seed (with its food / transport tx) untouched
    so other test modules that count on it still pass."""
    _conn().execute(
        "DELETE FROM transactions WHERE category = ?", (_TEST_CAT,)
    )


def test_compute_statuses_sums_this_months_spend():
    _wipe_test_cat_tx()
    s = get_store()
    s.add_budget(_make_budget(_TEST_CAT, 3_000_000))
    _add_tx(500_000)
    _add_tx(200_000)
    statuses = compute_statuses(USER)
    [st] = [x for x in statuses if x.category == _TEST_CAT]
    assert st.spent_vnd == 700_000
    assert st.remaining_vnd == 2_300_000
    assert 0 < st.ratio < 1.0


def test_compute_statuses_flags_over_budget():
    _wipe_test_cat_tx()
    s = get_store()
    s.add_budget(_make_budget(_TEST_CAT, 1_000_000))
    _add_tx(1_500_000)
    [st] = [x for x in compute_statuses(USER) if x.category == _TEST_CAT]
    assert st.ratio > 1.0
    assert st.remaining_vnd < 0


def test_compute_statuses_ignores_tx_before_budget_created():
    """Creating a budget mid-month must not retroactively count earlier
    spend in the same month against the new envelope — otherwise the
    user gets a phantom ``budget_overshoot`` for spending that happened
    before the budget existed.
    """
    _wipe_test_cat_tx()
    s = get_store()
    # 10 days of prior spend in the same month — should be excluded.
    _add_tx(950_000, days_ago=10)
    # Budget created "now" (after the early-month spend).
    s.add_budget(_make_budget(_TEST_CAT, 1_000_000))
    # New spend after the budget — should count.
    _add_tx(100_000, days_ago=0)
    [st] = [x for x in compute_statuses(USER) if x.category == _TEST_CAT]
    assert st.spent_vnd == 100_000
    assert st.ratio < 1.0


def test_compute_statuses_ignores_other_months():
    """A transaction dated 40 days ago is in last month — must not count
    against this month's envelope."""
    _wipe_test_cat_tx()
    s = get_store()
    s.add_budget(_make_budget(_TEST_CAT, 3_000_000))
    _add_tx(500_000, days_ago=0)
    _add_tx(2_000_000, days_ago=40)
    [st] = [x for x in compute_statuses(USER) if x.category == _TEST_CAT]
    assert st.spent_vnd == 500_000


def test_label_for_known_and_unknown_codes():
    assert label_for("food") == "Ăn uống"
    assert label_for("transport") == "Đi lại"
    assert label_for("widgets") == "Widgets"


# ---------------------------------------------------------------------------
# NLU routing
# ---------------------------------------------------------------------------


def test_nlu_routes_set_budget():
    r = understand("đặt ngân sách ăn uống 3 triệu")
    assert r.intent == "set_budget"
    assert r.entities.budget_category == "food"
    assert r.entities.amount == 3_000_000


def test_nlu_routes_set_budget_transport():
    r = understand("đặt ngân sách đi lại 1 triệu")
    assert r.intent == "set_budget"
    assert r.entities.budget_category == "transport"


def test_nlu_routes_budget_status_with_category():
    r = understand("tháng này còn bao nhiêu cho ăn uống?")
    assert r.intent == "budget_status"
    assert r.entities.budget_category == "food"


def test_nlu_routes_budget_status_general():
    r = understand("ngân sách giải trí thế nào")
    assert r.intent == "budget_status"
    assert r.entities.budget_category == "entertainment"


# ---------------------------------------------------------------------------
# End-to-end via orchestrator
# ---------------------------------------------------------------------------


def test_orchestrator_set_budget_creates_draft_then_confirm_persists():
    resp = handle_message(USER, "đặt ngân sách ăn uống 3 triệu")
    assert resp.intent == "set_budget"
    assert resp.budget_draft is not None
    assert resp.budget_draft.category == "food"
    assert resp.budget_draft.monthly_limit_vnd == 3_000_000

    confirm = handle_message(USER, "xác nhận")
    assert confirm.intent == "set_budget"
    rows = get_store().budgets_of(USER)
    assert len(rows) == 1
    assert rows[0].monthly_limit_vnd == 3_000_000


def test_orchestrator_set_budget_cancel_does_not_persist():
    handle_message(USER, "đặt ngân sách ăn uống 3 triệu")
    cancel = handle_message(USER, "huỷ")
    assert "huỷ" in cancel.text.lower()
    assert get_store().budgets_of(USER) == []


def test_orchestrator_budget_status_without_budgets_is_helpful():
    resp = handle_message(USER, "ngân sách ăn uống còn bao nhiêu")
    assert resp.intent == "budget_status"
    assert "chưa" in resp.text.lower() or "chua" in resp.text.lower()

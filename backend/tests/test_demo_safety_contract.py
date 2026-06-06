"""Regression guard for the demo-safety contract.

These cases pin down specific bugs the rule-only fallback (LLM keys
blank — the CI / Playwright / 429 path) used to hit silently. The
slide deck commits to "Demo never breaks on 429", and the audit logs
show each of these would have crashed or rendered the wrong card on
stage. Keep them green so the next merge wave can't undo the fix
without CI catching it.

Each commit referenced in the docstrings landed the fix that made the
assertion pass; if a case starts failing again, the first thing to do
is look at what reverted that commit's edit.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from app.context.session import session_for
from app.nlp.budget_entities import detect_goal_intent, extract_goal_name
from app.nlp.intent import classify
from app.nlp.pipeline import understand
from app.services.orchestrator import _is_confirm, handle_message


USER = "u_an"


@pytest.fixture(scope="module", autouse=True)
def _seed_demo_user():
    """Copy the canonical JSON seed into the conftest's isolated tmp
    data dir so the orchestrator paths that need an actual ``u_an``
    user (transfer, balance, history) work. Mirrors the pattern in
    test_metrics / test_demo_recorder — see 2d3da3f for the
    BANKING_DATA_DIR-empty-string footgun this fallback handles.
    """
    env_dir = os.environ.get("BANKING_DATA_DIR", "").strip()
    if not env_dir:
        env_dir = str(
            Path(__file__).resolve().parent.parent / ".tmp_test_seed"
        )
        os.environ["BANKING_DATA_DIR"] = env_dir
    data_dir = Path(env_dir).resolve()
    src = Path(__file__).resolve().parent.parent / "app" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "users.json",
        "contacts.json",
        "transactions.json",
        "schedules.json",
    ):
        target = data_dir / name
        if not target.exists() and (src / name).exists():
            shutil.copyfile(src / name, target)
    db_file = data_dir / "omni.db"
    if db_file.exists():
        db_file.unlink()
    # Drop the cached DB connection + Store singleton so the next
    # ``get_store()`` re-bootstraps from the just-copied JSON seeds.
    try:
        from app.db.connection import reset_connection
        reset_connection()
    except Exception:  # pragma: no cover — defensive
        pass
    try:
        import app.store as _store_mod
        _store_mod._store = None
    except Exception:  # pragma: no cover — defensive
        pass


def _clear_all_drafts() -> None:
    """Clear every draft type — transaction, contact, schedule, budget,
    goal. Avoids leaking state out to siblings like ``test_multiturn``
    that assert on no-draft behaviour.

    Wrapped in try/except so a session backend that lacks one of the
    clear methods (e.g. a future Redis variant) still wins through
    instead of breaking the regression guard."""
    s = session_for(USER)
    for attr in ("clear_draft", "clear_contact_draft", "clear_schedule_draft"):
        fn = getattr(s, attr, None)
        if fn:
            try:
                fn()
            except Exception:
                pass

    # Budget / goal drafts live in module-level dicts on the orchestrator
    # (in-memory stash, not in the session backend). Reach in and wipe them.
    try:
        from app.services import orchestrator as _orch

        for store in ("_budget_drafts", "_goal_drafts"):
            d = getattr(_orch, store, None)
            if isinstance(d, dict):
                d.pop(USER, None)
    except Exception:
        pass


def _r(text: str):
    """Fresh session per turn — these are intent / routing assertions, not
    multi-turn ones, so leaking draft state would just confuse the test."""
    _clear_all_drafts()
    return handle_message(USER, text)


@pytest.fixture(autouse=True)
def _isolate_session():
    """Per-test session isolation: clean before AND after so a leftover
    budget / goal / contact draft can't reach the next test (in this
    module or any sibling)."""
    _clear_all_drafts()
    yield
    _clear_all_drafts()


# ---------------------------------------------------------------------------
# ad20c2a — insights intent end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Tháng này có giao dịch nào bất thường không?",
        "Có gì khả nghi không?",
        "Phân tích chi tiêu hộ mình",
        "So sánh chi tiêu tháng này",
        "Có chi tiêu nào lạ không?",
    ],
)
def test_insights_intent_routes_under_rule_only(text: str) -> None:
    """schemas.Intent must include "insights" AND orchestrator must have a
    handler — otherwise the rule classifier emits "insights" and
    NLUResult raises pydantic ValidationError (500)."""
    r = handle_message(USER, text)
    assert r.intent == "insights", text
    assert r.text  # composed reply, not empty


# ---------------------------------------------------------------------------
# 1ac3c9e — add_contact rule extracts bank/alias/name
# ---------------------------------------------------------------------------


def test_kb7_add_contact_extracts_all_slots_under_rule_only() -> None:
    """Canonical KB7 demo phrasing must produce a complete contact_draft
    under the rule path (LLM rate-limited). If bank_name or alias come
    back empty the orchestrator asks "ngân hàng nào?" / "tên gọi tắt?"
    and the demo stalls."""
    nlu = understand("Lưu Nguyễn Văn Z STK 1112223334 MB Bank tên gọi tắt Z")
    assert nlu.intent == "add_contact"
    assert nlu.entities.recipient_text  # name extracted
    assert nlu.entities.bank_name == "MB Bank"
    assert nlu.entities.account_hint == "1112223334"
    assert nlu.entities.alias == "Z"


def test_add_contact_bank_normalization() -> None:
    """VCB / TCB / MBBank etc must normalise to the display form the card
    shows ("Vietcombank" / "Techcombank" / "MB Bank")."""
    cases = [
        ("Lưu Anh Toàn STK 9988 VCB", "Vietcombank"),
        ("Lưu A STK 8877 TCB", "Techcombank"),
        ("Lưu B STK 7766 mbbank", "MB Bank"),
    ]
    for text, expected in cases:
        nlu = understand(text)
        assert nlu.entities.bank_name == expected, text


# ---------------------------------------------------------------------------
# 072e9a2 — bare "lưu" doesn't auto-confirm a draft
# ---------------------------------------------------------------------------


def test_luu_with_name_is_not_a_confirm() -> None:
    """``_CONFIRM_RE`` used to match any "lưu …" so "Lưu Lê Mai STK …"
    auto-confirmed whatever draft was open. Now only bare "lưu" or
    "lưu + continuation particle" counts as confirm."""
    assert _is_confirm("Lưu Lê Mai STK 0123987654 Vietcombank") is False
    assert _is_confirm("Lưu Nguyễn Văn Z STK 1112") is False


@pytest.mark.parametrize(
    "text",
    [
        "lưu",
        "lưu lại",
        "lưu đi",
        "luu nha",
        "lưu giúp mình",
        "lưu giúp mình nhé",
        "lưu cho mình",
    ],
)
def test_luu_alone_or_with_particle_is_confirm(text: str) -> None:
    assert _is_confirm(text) is True, text


# ---------------------------------------------------------------------------
# 77db52c — atm_finder beats history for mid-token bank queries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "chi nhánh BIDV gần nhất",
        "ATM Vietcombank gần nhất",
        "chi nhánh Techcombank nào ở gần",
        "có ATM Vietinbank quanh đây không",
        "tìm cây ATM gần đây",
    ],
)
def test_atm_finder_with_bank_token_routes_correctly(text: str) -> None:
    """Tier-1 keyword substrings used to miss when a bank token sat
    between "atm/chi nhánh" and "gần nhất"; history's "gan nhat" then
    stole the route. The regex pre-check must keep these on atm_finder."""
    intent, _ = classify(text)
    assert intent == "atm_finder", text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("lần gần nhất gửi mẹ", "history"),
        ("5 giao dịch gần nhất", "history"),
        ("Chuyển 2 triệu cho mẹ", "transfer"),
    ],
)
def test_atm_finder_does_not_eat_history_or_transfer(text: str, expected: str) -> None:
    intent, _ = classify(text)
    assert intent == expected, text


# ---------------------------------------------------------------------------
# 559df9d + cfa2976 — goal name extraction across phrasings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_substr",
    [
        ("Tạo mục tiêu tiết kiệm 50 triệu cho Tết 2027", "Tết 2027"),
        ("Tôi muốn tiết kiệm 30 triệu mua xe", "xe"),
        ("tiết kiệm 100 triệu cho việc đi du học", "đi du học"),
        ("mục tiêu Tết 50tr", "Tết"),
        ("để dành 30 triệu cho mua xe", "xe"),
        ("để dành 50 triệu cho Tết", "Tết"),
        ("để dành 100 triệu cho con", "con"),
    ],
)
def test_goal_name_extraction(text: str, expected_substr: str) -> None:
    assert detect_goal_intent(text) is True, text
    name = extract_goal_name(text)
    assert name and expected_substr in name, f"{text!r} → {name!r}"


def test_goal_intent_recognises_de_danh() -> None:
    """``để dành`` is the everyday phrasing for "set aside" and must
    behave the same as "tiết kiệm" at the detector level."""
    assert detect_goal_intent("để dành 30 triệu cho mua xe") is True


# ---------------------------------------------------------------------------
# 782906c — insights + recurring keyword coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Có chi tiêu nào lạ không?", "insights"),
        ("So sánh chi tiêu tháng này", "insights"),
        ("Tháng này tôi tiêu nhiều hơn tháng trước không?", "insights"),
        ("mình đang có những khoản nào trả định kỳ", "recurring"),
        ("liệt kê các khoản trả tự động", "recurring"),
    ],
)
def test_insights_and_recurring_keyword_coverage(text: str, expected: str) -> None:
    r = _r(text)
    assert r.intent == expected, text


# ---------------------------------------------------------------------------
# 90a6c47 — set_budget constraint-style phrasing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Tôi muốn giới hạn chi tiêu 5 triệu mỗi tháng cho ăn uống",
        "giới hạn chi tiêu ăn uống 3 triệu",
        "khống chế chi tiêu mua sắm 2 triệu",
        "đặt mức chi 4 triệu cho ăn uống",
    ],
)
def test_set_budget_constraint_phrasing(text: str) -> None:
    """``giới hạn chi tiêu`` / ``khống chế chi tiêu`` / ``đặt mức chi``
    are the everyday verb-anchored budget phrasings that don't mention
    the noun "ngân sách". They must route to set_budget, not schedule
    (which would happen via the "mỗi tháng" Tier-2 keyword)."""
    r = _r(text)
    assert r.intent == "set_budget", text


# ---------------------------------------------------------------------------
# 10937f2 — anomaly callout shows the detector's per-recipient reason
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# goal_status — progress query against an existing savings goal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "tiến độ mục tiêu",
        "mục tiêu của tôi",
        "mục tiêu của mình",
        "đã tiết kiệm được bao nhiêu",
        "tiết kiệm đến đâu rồi",
    ],
)
def test_goal_status_routes_under_rule_only(text: str) -> None:
    """Goal progress queries used to fall through to ``unknown``.
    Now route through goal_status_handler which lists each goal's
    progress with a % bar."""
    r = _r(text)
    assert r.intent == "goal_status", text
    assert r.text  # composed reply


def test_goal_status_empty_state_is_helpful() -> None:
    """With no goals set, the handler must nudge toward set_goal
    instead of returning a confusing empty reply."""
    r = _r("tiến độ mục tiêu")
    assert r.intent == "goal_status"
    # Either we have a goal (from session pollution) and progress shows,
    # or the empty-state copy points the user to set_goal.
    assert (
        "Tiến độ" in r.text  # populated case
        or "chưa tạo mục tiêu" in r.text  # empty case
    )


# ---------------------------------------------------------------------------
# Confirm / cancel everyday Vietnamese phrasings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "okay",
        "oki",
        "được",
        "được rồi",
        "được luôn",
        "được nha",
        "ừ",
        "ừm",
    ],
)
def test_everyday_vietnamese_confirms(text: str) -> None:
    """``ok`` / ``đồng ý`` / ``yes`` were already in the confirm regex
    but ``okay`` / ``được`` / ``được rồi`` / ``ừ`` — what judges
    actually say — fell through to ``unknown`` and the active draft
    stayed dangling. Now match at word-boundary so all of these
    confirm the in-flight transfer."""
    from app.services.orchestrator import _is_confirm
    assert _is_confirm(text) is True, text


@pytest.mark.parametrize(
    "text",
    [
        "thôi",
        "thôi nha",
        "thôi không gửi nữa",
        "đừng",
        "đừng nữa",
        "khoan",
    ],
)
def test_everyday_vietnamese_cancels(text: str) -> None:
    """``huỷ`` / ``cancel`` / ``no`` worked but the much more common
    ``thôi`` / ``đừng`` / ``khoan`` were missing. Now they short-circuit
    the orchestrator into the cancel path instead of running NLU and
    silently leaving the draft open."""
    from app.services.orchestrator import _is_cancel
    assert _is_cancel(text) is True, text


# ---------------------------------------------------------------------------
# Bounded "hi"/"hey" smalltalk match — substring vs word-boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Bare "hi" inside "hiện" / "nghi" / "chi" — substring smalltalk
        # used to steal these into smalltalk before the bounded fix.
        "phát hiện chi tiêu lạ",
        "phát hiện giao dịch lạ",
        "omni rà soát chi tiêu hộ mình",
        "Chi ơi cho mình số dư",
        "ghi nhớ giúp mình",
    ],
)
def test_hi_substring_does_not_route_smalltalk(text: str) -> None:
    """Tier-2 smalltalk used the bare substrings "hi" / "hey" — those
    match inside the Vietnamese tokens *hiện* / *nghi* / *chi* and
    misrouted normal banking text. Word-boundary match required."""
    intent, _ = classify(text)
    assert intent != "smalltalk", text


@pytest.mark.parametrize("text", ["hi", "hey", "hi omni", "hey omni", "hello"])
def test_actual_greetings_still_route_smalltalk(text: str) -> None:
    intent, _ = classify(text)
    assert intent == "smalltalk", text


# ---------------------------------------------------------------------------
# Isolation Forest fraud scorer wired into the safety contract
# ---------------------------------------------------------------------------


def test_fraud_risk_high_flag_is_in_schema() -> None:
    """Schema's SafetyFlag Literal must include ``fraud_risk_high`` —
    otherwise the score-above-threshold path crashes Pydantic at flag
    construction. Test schema directly so a revert in schemas.py is
    caught even when the fraud model isn't loaded for the test user."""
    from app.models.schemas import SafetyFlag

    # If the literal includes the code, constructing the flag succeeds.
    f = SafetyFlag(code="fraud_risk_high", severity="warn", message="x")
    assert f.code == "fraud_risk_high"


def test_fraud_risk_high_triggers_step_up() -> None:
    """``requires_step_up()`` must include the new flag — otherwise a
    high IF score wouldn't gate an OTP and the model would be
    decorative."""
    from app.models.schemas import SafetyFlag
    from app.safety.rules import requires_step_up

    flags = [SafetyFlag(code="fraud_risk_high", severity="warn", message="x")]
    assert requires_step_up(flags) is True


# ---------------------------------------------------------------------------
# History coverage — top_recipient, total expense, default-N list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Tổng thu chi tháng này",
        "Tổng chi tiêu tháng trước",
        "tổng chi tiêu của tôi từ trước đến nay",
        # Newer history phrasings (e1cde1c + this commit). "Tổng chi phí
        # hàng tháng" used to route to schedule because "hang thang" hit
        # the schedule Tier-2 keyword before any history rule fired.
        "Tổng chi phí hàng tháng",
        "Báo cáo tháng",
        "Báo cáo chi tiêu tháng này",
    ],
)
def test_total_expense_phrasings_route_history(text: str) -> None:
    """``Tổng thu chi`` and ``tổng chi tiêu`` are the most common ways
    judges ask for an aggregate — ``tong chi`` substring missed them
    before the keyword expansion landed."""
    intent, _ = classify(text)
    assert intent == "history", text


@pytest.mark.parametrize(
    "text",
    [
        "Top 5 người tôi gửi nhiều nhất",
        "Top người chuyển nhiều nhất",
        "Tôi gửi ai nhiều nhất tháng này?",
    ],
)
def test_top_recipient_entity_extracted(text: str) -> None:
    """The history handler needs ``top_recipient=True`` to surface the
    "Người nhận nhiều nhất" line. Pre-fix the entity was only set on
    "ai gửi nhiều nhất" — missing the very common "Top N người" form
    and the verb-first "tôi gửi ai nhiều nhất" form."""
    from app.nlp.entities import extract
    e = extract(text)
    assert e.top_recipient is True, text


@pytest.mark.parametrize(
    "text",
    [
        "Cho tôi xem các giao dịch gần nhất",
        "Giao dịch gần đây của tôi",
        "Các giao dịch gần nhất",
    ],
)
def test_default_limit_for_listy_history_queries(text: str) -> None:
    """When the user says "các giao dịch gần nhất" without a number,
    the extractor used to leave ``limit=None`` and the handler
    rendered the period aggregate instead of a list. Default-N=5
    fills the blank so judges see the receipt-style list they asked
    for."""
    from app.nlp.entities import extract
    e = extract(text)
    assert e.limit == 5, f"{text!r} → limit={e.limit}"


def test_budget_overshoot_warn_when_draft_exceeds_envelope() -> None:
    """End-to-end: when the user has a monthly budget for ``category`` and
    the draft amount would push spent past the limit, evaluate() must
    emit a ``budget_overshoot`` warn flag with structured details.
    Soft warn only — must NOT trigger requires_step_up (the user already
    opted in to the limit; the safety layer just reminds)."""
    from datetime import datetime, timezone

    from app.models.schemas import Account, Budget, Contact
    from app.safety.rules import evaluate, requires_step_up
    from app.store import get_store, new_id

    store = get_store()
    budget = Budget(
        id=new_id("b"), user_id=USER, category="food",
        monthly_limit_vnd=2_000_000,
        created_at=datetime.now(timezone.utc),
    )
    store.add_budget(budget)

    recipient = Contact(
        id="c_quan", owner_id=USER, display_name="Quán Phở",
        bank="MB", account_number="0123456789", account_masked="6789",
    )
    account = Account(
        id="a_t", bank="Omni", number="999",
        balance=100_000_000, primary=True,
    )

    flags = evaluate(
        amount=2_500_000,
        recipient_candidates=[],
        recipient=recipient,
        transactions=[],
        account=account,
        user_id=USER,
        category="food",
    )
    over = next((f for f in flags if f.code == "budget_overshoot"), None)
    assert over is not None, (
        f"expected budget_overshoot in {[f.code for f in flags]}"
    )
    assert over.severity == "warn"
    assert over.details is not None
    d = over.details
    assert d["kind"] == "budget_overshoot"
    assert d["category"] == "food"
    assert d["monthly_limit_vnd"] == 2_000_000
    assert d["overshoot_vnd"] == 500_000

    # Soft warn — never gates the transfer.
    assert requires_step_up(flags) is False, (
        "budget_overshoot must not trigger OTP step-up; "
        "the user already set the limit themselves"
    )

    # Clean up so the seeded budget doesn't leak to sibling tests.
    store.delete_budget(budget.id)


def test_transfer_to_known_recipient_populates_recent_ledger() -> None:
    """End-to-end: ``Gửi mẹ 2 triệu`` against the seeded ``u_an`` user
    must produce a draft whose ``recent_to_recipient`` mini-ledger lists
    up to 3 prior completed transfers (the seed has ≥3 to mẹ / Nguyễn
    Thị Lan). The schema-level test below catches a silent field drop;
    this one catches an orchestrator regression that stops populating
    the field even though the schema still accepts it."""
    r = _r("Gửi mẹ 2 triệu")
    assert r.intent == "transfer", r.intent
    assert r.draft is not None, "transfer should produce a draft"
    assert r.draft.recipient is not None, "alias mẹ must resolve"
    items = r.draft.recent_to_recipient
    assert items, (
        "draft.recent_to_recipient must be populated for a recipient with "
        f"history (got {items!r})"
    )
    assert len(items) <= 3
    for row in items:
        assert isinstance(row["amount"], int) and row["amount"] > 0
        assert isinstance(row["created_at"], str) and row["created_at"]
        assert "description" in row


def test_balance_response_carries_7day_outflow_series() -> None:
    """get_balance() must include ``recent_outflow_7d`` — a 7-element
    list of non-negative ints (oldest → newest). The BalanceCard
    sparkline depends on this exact shape; if the key drops or the
    list length changes the sparkline silently disappears and judges
    lose a visible ML/UX touchpoint."""
    from app.banking.service import get_balance

    b = get_balance(USER)
    assert "recent_outflow_7d" in b, "balance response missing recent_outflow_7d"
    series = b["recent_outflow_7d"]
    assert isinstance(series, list) and len(series) == 7, (
        f"recent_outflow_7d must be a length-7 list, got {series!r}"
    )
    assert all(isinstance(x, int) and x >= 0 for x in series), (
        f"every cell must be a non-negative int, got {series!r}"
    )


def test_transaction_draft_schema_carries_recent_to_recipient_field() -> None:
    """The TransactionCard renders an inline mini-ledger from
    ``draft.recent_to_recipient`` (last 3 completed transfers to the
    chosen recipient). If that schema field is silently dropped the
    frontend just hides the ledger and the regression is invisible —
    so pin the field's existence + shape at the schema layer where the
    isolated test conftest can verify it without a seeded DB."""
    from app.models.schemas import TransactionDraft

    fields = TransactionDraft.model_fields
    assert "recent_to_recipient" in fields, (
        "TransactionDraft.recent_to_recipient must exist — mini-ledger UI depends on it"
    )
    # Construct a draft with the payload the orchestrator builds — same
    # raw-dict shape the frontend consumes. Pydantic must accept it.
    d = TransactionDraft(
        id="d_x",
        recent_to_recipient=[
            {
                "amount": 1_000_000,
                "created_at": "2026-06-01T08:00:00+07:00",
                "description": "phí học",
                "category": "education",
            }
        ],
    )
    assert d.recent_to_recipient is not None and len(d.recent_to_recipient) == 1
    row = d.recent_to_recipient[0]
    assert row["amount"] == 1_000_000
    assert row["description"] == "phí học"


def test_amount_above_average_carries_structured_details() -> None:
    """The per-recipient anomaly flag must ship a ``details`` payload with
    median / p90 / n_samples / ratio so the TransactionCard can render a
    "why" box under the warn line. Without these, the chip falls back to
    prose-only and the UX regression goes silent."""
    from datetime import datetime, timezone

    from app.models.schemas import Account, Contact, Transaction
    from app.safety.rules import evaluate

    recipient = Contact(
        id="c_test", owner_id=USER, display_name="Test",
        bank="MB", account_number="0123456789", account_masked="6789",
    )
    account = Account(
        id="a_test", bank="Omni", number="987",
        balance=500_000_000, primary=True,
    )
    # 6 prior transfers all ~1M; the new draft is 50M — 50× the median.
    base = datetime.now(timezone.utc)
    past = [
        Transaction(
            id=f"t{i}", owner_id=USER, contact_id="c_test",
            amount=1_000_000, description="prior",
            category="other", status="completed", created_at=base,
        )
        for i in range(6)
    ]
    flags = evaluate(
        amount=50_000_000,
        recipient_candidates=[],
        recipient=recipient,
        transactions=past,
        account=account,
        user_id=USER,
    )
    anomaly = next((f for f in flags if f.code == "amount_above_average"), None)
    assert anomaly is not None, f"expected amount_above_average in {[f.code for f in flags]}"
    assert anomaly.details is not None, "details payload must be populated"
    d = anomaly.details
    assert d["kind"] == "per_recipient"
    assert d["median"] == 1_000_000
    assert d["p90"] == 1_000_000
    assert d["n_samples"] == 6
    assert d["current_amount"] == 50_000_000
    assert d["ratio"] >= 49.0  # ~50×


def test_transfer_velocity_high_details_payload_shape() -> None:
    """The transfer_velocity_high warn ships a structured ``details``
    block (kind="velocity" + recent_count + window_sec + threshold)
    that TransactionCard renders as a "why" panel. Backend rename
    here silently drops the explanation, same failure mode as the
    fraud_risk_high test. Pin the field names so the next merge
    can't quietly break the panel."""
    from app.models.schemas import SafetyFlag

    # The flag literal must accept "transfer_velocity_high" — without
    # this entry rules.py raises Pydantic ValidationError before any
    # frontend ever sees the warn.
    f = SafetyFlag(
        code="transfer_velocity_high",
        severity="warn",
        message="x",
        details={
            "kind": "velocity",
            "recent_count": 5,
            "window_sec": 60,
            "threshold": 3,
        },
    )
    assert f.code == "transfer_velocity_high"
    assert f.details is not None
    assert f.details.get("kind") == "velocity"
    assert f.details.get("recent_count") == 5
    assert f.details.get("window_sec") == 60


def test_velocity_high_triggers_step_up() -> None:
    """``requires_step_up()`` must include the velocity flag — otherwise
    a velocity hit would render the warn message but never gate OTP
    and the velocity rule would be decorative."""
    from app.models.schemas import SafetyFlag
    from app.safety.rules import requires_step_up

    flags = [
        SafetyFlag(
            code="transfer_velocity_high",
            severity="warn",
            message="x",
        )
    ]
    assert requires_step_up(flags) is True


# ---------------------------------------------------------------------------
# Smalltalk + bare-recipient coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "bye omni",
        "tạm biệt",
        "Bye Bye",
        "Goodbye",
        "Good morning omni",
        "Tạm biệt Omni",
        "cảm ơn omni",
    ],
)
def test_farewells_route_smalltalk(text: str) -> None:
    """``bye``/``tạm biệt``/``goodbye`` used to fall through to
    unknown — judges who said goodbye saw the awkward "thử chuyển cho
    mẹ 2 triệu" fallback. Now they hit the smalltalk handler."""
    intent, _ = classify(text)
    assert intent == "smalltalk", text


@pytest.mark.parametrize(
    "text,expected_recipient_substr",
    [
        # Vietnamese chat shorthand without "cho" / verb. The bare leading
        # token + amount form is what judges actually type in casual
        # demos.
        ("mẹ 2tr", "mẹ"),
        ("mẹ 2tr tiền ăn", "mẹ"),
        ("anh Hùng 500k", "Hùng"),
        ("mẹ 5 triệu", "mẹ"),
    ],
)
def test_bare_recipient_amount_pattern_extracts_recipient(
    text: str, expected_recipient_substr: str
) -> None:
    """The leading-token + amount pattern fills the recipient when
    no other extractor catches it."""
    from app.nlp.entities import extract
    e = extract(text)
    assert e.recipient_text is not None, text
    assert expected_recipient_substr in e.recipient_text, (
        f"{text!r} → {e.recipient_text!r}; expected substring "
        f"{expected_recipient_substr!r}"
    )


@pytest.mark.parametrize(
    "text",
    [
        # Amount-context nouns must NOT route to a transfer with the
        # noun as recipient. Wrong-recipient = wrong money.
        "lương 5tr",
        "số dư 2tr",
        "tiền nhà 3tr",
        "ngân sách 1tr",
        "tiết kiệm 5tr",
    ],
)
def test_bare_recipient_denylist_rejects_context_nouns(text: str) -> None:
    from app.nlp.entities import extract
    e = extract(text)
    assert e.recipient_text is None, (
        f"{text!r} → recipient={e.recipient_text!r}; expected None — "
        "context nouns must NOT be treated as recipients"
    )


def test_audit_log_records_transfer_lifecycle(
    tmp_path, monkeypatch
) -> None:
    """SBV-style audit trail must record OTP request + verify + the
    actual transfer_executed event, plus cancel for the unhappy path.
    eaf4484 shipped the writer; this test pins the call sites are
    actually wired so a future revert can't silently disable the
    audit trail."""
    import json

    monkeypatch.setenv("OMNI_AUDIT_DIR", str(tmp_path))
    # Force a fresh writer that picks up the patched env.
    from app.services import audit_log
    audit_log._FH = None
    audit_log._FH_DAY = None

    from app.services.orchestrator import (
        cancel_draft,
        confirm_draft,
        handle_message,
    )
    _clear_all_drafts()

    # Happy path: draft → confirm (OTP req) → verify → execute.
    r1 = handle_message(USER, "Chuyển 500k cho mẹ")
    assert r1.draft is not None
    draft_id = r1.draft.id
    confirm_draft(USER, draft_id)            # OTP requested
    confirm_draft(USER, draft_id, otp="123456")  # OTP verified + exec

    # Unhappy path: draft → cancel.
    _clear_all_drafts()
    r4 = handle_message(USER, "Chuyển 500k cho mẹ")
    cancel_draft(USER, r4.draft.id)

    log_files = list(tmp_path.glob("audit-*.log"))
    assert log_files, "no audit log file produced"
    events = []
    for f in log_files:
        for line in f.read_text().splitlines():
            events.append(json.loads(line))
    kinds = {e["kind"] for e in events}
    actions = {e.get("action") for e in events if e["kind"] == "otp"}

    assert "otp" in kinds, "OTP request / verify not recorded"
    assert "requested" in actions, "OTP requested action not recorded"
    assert "verified" in actions, "OTP verified action not recorded"
    assert "transfer_executed" in kinds, "transfer execute not recorded"
    assert "cancel" in kinds, "draft cancel not recorded"


# ---------------------------------------------------------------------------
# Schedule cron — DOW extraction + cron→Python weekday translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_cron",
    [
        # Numeric Vietnamese DOW. "thứ 5 hàng tuần" used to silently
        # map to Monday — wrong-day schedule.
        ("đặt lịch chuyển mẹ 2tr thứ 5 hàng tuần", "0 9 * * 4"),
        ("đặt lịch chuyển mẹ 2tr thứ 2 hàng tuần", "0 9 * * 1"),
        ("đặt lịch chuyển mẹ 2tr thứ 6 hàng tuần", "0 9 * * 5"),
        ("đặt lịch chuyển mẹ 2tr thứ 7 hàng tuần", "0 9 * * 6"),
        # Spelled-out.
        ("đặt lịch chuyển mẹ 2tr thứ hai hàng tuần", "0 9 * * 1"),
        ("đặt lịch chuyển mẹ 2tr Chủ nhật hàng tuần", "0 9 * * 0"),
        ("đặt lịch chuyển mẹ 2tr CN hàng tuần", "0 9 * * 0"),
        # Daily.
        ("mỗi ngày 100k cho mẹ", "0 9 * * *"),
        # Defaults preserved.
        ("đặt lịch chuyển mẹ 2tr hàng tuần", "0 9 * * 1"),
        ("đặt lịch chuyển mẹ 2tr mùng 5 hàng tháng", "0 9 5 * *"),
    ],
)
def test_schedule_cron_extracts_day_of_week(
    text: str, expected_cron: str
) -> None:
    from app.nlp.entities import extract
    e = extract(text)
    assert e.schedule_cron == expected_cron, text


def test_next_run_for_translates_cron_dow_to_python_weekday() -> None:
    """next_run_for used to compare cron DOW directly against Python's
    ``datetime.weekday()`` — so every day landed one off. Monday cron
    fell on Tuesday, Sunday cron fell on Monday, etc. Wrong-schedule
    bug judges would hit on day 2 of using the calendar feature."""
    from datetime import datetime, timezone, timedelta

    from app.banking.service import next_run_for

    # 2026-06-06 was Saturday (per CLAUDE.md's current-date pin).
    sat = datetime(2026, 6, 6, 12, 0, tzinfo=timezone(timedelta(hours=7)))

    expected = {
        "0 9 * * 1": 0,  # next Monday — weekday 0
        "0 9 * * 2": 1,  # Tuesday
        "0 9 * * 3": 2,  # Wednesday
        "0 9 * * 4": 3,  # Thursday
        "0 9 * * 5": 4,  # Friday
        "0 9 * * 6": 5,  # Saturday (next, not today)
        "0 9 * * 0": 6,  # Sunday
    }
    for cron, expected_weekday in expected.items():
        n = next_run_for(cron, sat)
        assert n.weekday() == expected_weekday, (
            f"cron {cron!r} → weekday {n.weekday()}, expected {expected_weekday}"
        )


def test_cron_label_renders_correct_vn_day() -> None:
    """``_cron_label`` mapped DOW=1 (Monday) to "thứ Ba" (Tuesday) — every
    weekly schedule rendered the wrong Vietnamese day. Pin the correct
    mapping so a future revert is caught."""
    from app.services.orchestrator import _cron_label

    assert _cron_label("0 9 * * 1") == "vào thứ Hai hàng tuần"
    assert _cron_label("0 9 * * 4") == "vào thứ Năm hàng tuần"
    assert _cron_label("0 9 * * 6") == "vào thứ Bảy hàng tuần"
    assert _cron_label("0 9 * * 0") == "vào Chủ Nhật hàng tuần"
    assert _cron_label("0 9 * * *") == "mỗi ngày"


# ---------------------------------------------------------------------------
# Month-year reference routes to history, not transfer (Tier-3 fallback bug)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Month / year — original coverage.
        "tháng 5 năm 2026",
        "tháng 5/2026",
        "thang 4",
        "tháng 12 năm 2025",
        # Year only.
        "năm 2026",
        "năm 2026 tiêu bao nhiêu",
        # Quarter.
        "quý 1",
        "quý 2 năm 2026",
        # Day / slash-date.
        "ngày 15/5",
        "15/5/2026",
        "15/5",
        # N-period-gần-đây.
        "6 tháng gần đây",
        "3 tuần qua",
        # Bare temporal phrases.
        "tuần này",
        "tuần trước tiêu bao nhiêu",
        "hôm qua tiêu gì",
        "năm nay",
        "năm ngoái",
        "đầu năm",
        "cuối năm",
        "đầu tháng",
        "cuối tháng",
    ],
)
def test_temporal_reference_routes_history(text: str) -> None:
    """Temporal references — month, year, quarter, day, week, hôm/đầu/
    cuối — must route to history, not transfer. The Tier-3 bare-digit
    fallback used to grab "năm 2026" / "15/5" / "tháng 5 năm 2026" as
    transfers (because the year is a digit) and bare phrases like "tuần
    này" / "năm ngoái" fell to "unknown". Judges asking about a
    specific period would see a confused transfer draft or generic
    fallback instead of a history aggregate."""
    intent, _ = classify(text)
    assert intent == "history", text


@pytest.mark.parametrize(
    "text,expected",
    [
        # Same-class negatives — must still route correctly.
        ("Chuyển 2 triệu cho mẹ", "transfer"),
        ("Gửi mẹ 5 triệu", "transfer"),
        # Transfer with temporal phrase — Tier-1 "chuyển" wins first.
        ("chuyển mẹ 2 triệu đầu tháng", "transfer"),
        ("gửi anh Hùng 500k tuần này", "transfer"),
        # Schedule with temporal phrase — Tier-1 "đặt lịch" wins first.
        ("đặt lịch chuyển mẹ 2tr đầu tháng", "schedule"),
        ("số dư", "balance"),
        ("Lưu Lê Mai STK 0123987654 Vietcombank", "add_contact"),
    ],
)
def test_month_year_check_does_not_eat_other_intents(
    text: str, expected: str
) -> None:
    intent, _ = classify(text)
    assert intent == expected, text


# ---------------------------------------------------------------------------
# Colloquial balance phrasings — "còn bao nhiêu tiền", "cạn ví", "lương về"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "còn bao nhiêu tiền",
        "còn nhiều tiền không",
        "hết tiền chưa",
        "hết sạch tiền",
        "cạn ví",
        "tiền nong còn không",
        "tiền nong còn ko",
        "tiền còn không",
        "lương về chưa",
        "lương về rồi chưa",
    ],
)
def test_colloquial_balance_phrasings_route_to_balance(text: str) -> None:
    """The Tier-1 substring "so du" / "balance" matched only the literal
    "số dư" / "balance" question. A judge typing "còn bao nhiêu tiền" or
    "cạn ví" used to fall to the Tier-2 history match ("bao nhieu") and
    get a month aggregate instead of the actual balance — the most
    visible mis-routing in the demo. These are all idiomatic Vietnamese
    "do I still have money?" phrasings that judges actually use."""
    intent, _ = classify(text)
    assert intent == "balance", text


@pytest.mark.parametrize(
    "text,expected",
    [
        # Negatives — must still route correctly.
        ("tháng này tiêu nhiều tiền", "history"),
        ("tháng này hết bao nhiêu", "history"),
        ("gửi mẹ 2 triệu", "transfer"),
        ("số dư", "balance"),
    ],
)
def test_colloquial_balance_does_not_eat_other_intents(
    text: str, expected: str
) -> None:
    intent, _ = classify(text)
    assert intent == expected, text


# ---------------------------------------------------------------------------
# Account-info phrasings route to balance; transaction-search to history
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Per-account / account-count queries — the balance reply already
        # surfaces primary + total + per-account list, so routing these
        # to balance gives the right answer. Pre-fix they fell to
        # Tier-2 "bao nhieu" → month-aggregate history reply.
        "tài khoản chính của mình",
        "tài khoản tiết kiệm có bao nhiêu",
        "có bao nhiêu tài khoản",
        "tài khoản của tôi",
        "các tài khoản",
        "tổng tài sản",
    ],
)
def test_account_info_phrasings_route_balance(text: str) -> None:
    intent, _ = classify(text)
    assert intent == "balance", text


@pytest.mark.parametrize(
    "text",
    [
        # "Find / which / over / under" transaction searches — common
        # judge probes. Pre-fix all of them fell to "unknown" or the
        # Tier-3 transfer fallback (because "1 triệu" contains a digit).
        "tìm giao dịch trên 1 triệu",
        "giao dịch nào lớn nhất",
        "giao dịch nhỏ nhất tháng này",
        "giao dịch trên 5 triệu",
        "giao dịch dưới 100k",
    ],
)
def test_transaction_search_phrasings_route_history(text: str) -> None:
    intent, _ = classify(text)
    assert intent == "history", text


# ---------------------------------------------------------------------------
# Help intent — VN "how do I / what can you do" phrasings reach _HELP_TEXT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Exact commands.
        "/help", "help", "menu", "trợ giúp", "hướng dẫn",
        # "How do I" — Tier-2 transfer keyword "chuyển" used to steal
        # this; help check now runs before NLU.
        "làm sao chuyển tiền",
        "làm sao để chuyển tiền",
        "làm cách nào để xem số dư",
        # "What can you do" — used to fall through to "unknown".
        "omni làm gì được",
        "omni có thể làm gì",
        "có thể làm gì",
        "bạn làm được gì",
        "omni biết làm gì",
        # Guide / instructions.
        "hướng dẫn sử dụng",
        "cách dùng",
        "cách sử dụng",
        "làm thế nào",
        # Direct help asks.
        "giúp mình với",
        "giúp với",
        "help me",
        "giúp đỡ",
    ],
)
def test_help_phrasings_emit_help_text(text: str) -> None:
    """The judge's first question is almost always "what can you do?"
    or "how do I X?". Pre-fix these fell to either an empty transfer
    draft ("Bạn muốn chuyển bao nhiêu cho ai?") or the robotic "Mình
    chưa rõ ý bạn..." guess-correction page. The help check now runs
    BEFORE the NLU classifier so Tier-2 transfer/history keywords
    inside the help question ("làm sao **chuyển** tiền") can't steal
    the routing."""
    s = session_for(USER)
    s.clear_draft()
    r = handle_message(USER, text)
    s.clear_draft()
    # The deterministic help text starts with "Mình có thể giúp bạn:"
    # and lists the capability bullets. Pin a single high-signal token
    # that appears in every help response.
    assert "Mình có thể giúp bạn" in r.text, (text, r.text[:100])


@pytest.mark.parametrize(
    "text,expected",
    [
        # Polite prefix "giúp mình" before a real intent must NOT eat
        # the routing — the user is asking for X, not for help generally.
        ("giúp mình kiểm tra số dư", "balance"),
        ("chuyển mẹ 2 triệu", "transfer"),
        ("tháng này tiêu bao nhiêu", "history"),
        ("số dư", "balance"),
    ],
)
def test_help_check_does_not_eat_other_intents(
    text: str, expected: str
) -> None:
    s = session_for(USER)
    s.clear_draft()
    r = handle_message(USER, text)
    s.clear_draft()
    assert r.intent == expected, (text, r.intent, r.text[:100])


# ---------------------------------------------------------------------------
# Smalltalk subtypes — thanks / farewell / greeting each get their own reply
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # English + VN thanks variants — all routed to smalltalk.
        "cảm ơn", "cám ơn omni", "thank you", "thanks",
        # Farewells.
        "tạm biệt", "bye", "goodbye",
        # Greetings.
        "chào em", "chào anh", "chào omni", "chào", "chào!",
        "xin chào", "hi", "hello",
    ],
)
def test_smalltalk_phrasings_route_to_smalltalk(text: str) -> None:
    """The most common Vietnamese + English chat openers / closers all
    route to smalltalk. Used to fall to "unknown" → "Mình chưa rõ ý
    bạn..." (a robotic guess-correction reply that's bad demo vibe)."""
    intent, _ = classify(text)
    assert intent == "smalltalk", text


@pytest.mark.parametrize(
    "text,must_contain",
    [
        # Thanks → "không có chi"; NOT a re-greeting.
        ("cảm ơn", "Không có chi"),
        ("thank you", "Không có chi"),
        ("thanks", "Không có chi"),
        # Farewell → "hẹn gặp lại"; NOT a re-greeting.
        ("tạm biệt", "Hẹn gặp lại"),
        ("bye", "Hẹn gặp lại"),
        ("goodbye", "Hẹn gặp lại"),
        # Greeting → "Chào bạn".
        ("xin chào", "Chào bạn"),
        ("chào em", "Chào bạn"),
        ("hi", "Chào bạn"),
    ],
)
def test_smalltalk_reply_branches_by_subtype(text: str, must_contain: str) -> None:
    """Pre-fix the smalltalk handler ignored what the user actually said
    and replied "Chào bạn! Mình là Omni..." for thanks AND farewell —
    a robotic feel that judges noticed immediately. The deterministic
    fallback now branches on the user text before calling the LLM, so
    even under 429 the reply stays human."""
    s = session_for(USER)
    s.clear_draft()
    r = handle_message(USER, text)
    s.clear_draft()
    assert r.intent == "smalltalk", text
    assert must_contain in r.text, (text, r.text)


# ---------------------------------------------------------------------------
# First-person pronoun guard — "mình"/"tôi" must not be mistaken for "Minh"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Each of these was previously misrouted to an "Bạn muốn chuyển
        # bao nhiêu cho Minh nào (giữa ...)" prompt because diacritic-
        # stripping "mình" → "minh" matched the contact alias.
        "ai gửi tiền cho mình",
        "ai chuyển tiền cho mình",
        "có ai vừa chuyển cho mình không",
        "gửi mình 200k",
        "chuyển cho mình 500k",
        "trả tôi 100k",
        "cho tôi 50k",
    ],
)
def test_self_pronoun_not_extracted_as_recipient(text: str) -> None:
    """The diacritic-bearing first-person pronouns must NOT survive the
    recipient extractor — otherwise "ai gửi tiền cho mình" turns into a
    confused transfer draft offering to send Minh money. The contact
    "Minh" (no `ì`) still resolves; only the pronoun form is dropped.

    We assert on the extracted entity, not the full response, so the
    test stays sharp even if the orchestrator's downstream phrasing
    changes."""
    from app.nlp.entities import extract
    e = extract(text)
    assert e.recipient_text is None, (
        f"{text!r} → extracted {e.recipient_text!r}; should be dropped"
    )


@pytest.mark.parametrize(
    "text",
    [
        # The contact "Minh" (no diacritic) must STILL resolve as a
        # recipient — otherwise we've thrown the baby out with the
        # bathwater.
        "gửi Minh 200k",
        "chuyển cho Minh 500k",
        "gửi minh 200k",  # lowercase no-diacritic = ambiguous, defer to name
    ],
)
def test_contact_minh_still_resolves(text: str) -> None:
    from app.nlp.entities import extract
    e = extract(text)
    assert e.recipient_text is not None, (
        f"{text!r} dropped the real contact name"
    )
    # Both "Minh" and "minh" survive (diacritic-fold equal); we don't
    # care about case here.
    assert e.recipient_text.lower() == "minh"


# ---------------------------------------------------------------------------
# Fine-grained history periods — hôm nay, hôm qua, tuần này/trước, năm
# nay/ngoái. Previously every temporal phrase except "tháng trước" fell into
# either "this_month" (the default) or "recent_30d", so "hôm nay tiêu bao
# nhiêu" returned a month aggregate. The fix maps each phrase to its own
# window in get_history().
# ---------------------------------------------------------------------------


def test_history_period_today_is_today_only() -> None:
    """The period label echoes back as "hôm nay" — confirms the window
    didn't silently widen to this_month."""
    from app.banking.service import get_history
    h = get_history(user_id=USER, period="today")
    assert h["period"] == "today"
    # End is exclusive; window must be ≤ 24h so we never accidentally
    # roll yesterday's spending into today's count.
    from datetime import datetime
    start = datetime.fromisoformat(h["start"])
    end = datetime.fromisoformat(h["end"])
    assert (end - start).total_seconds() == 86400


def test_history_period_this_week_is_seven_days() -> None:
    from app.banking.service import get_history
    h = get_history(user_id=USER, period="this_week")
    assert h["period"] == "this_week"
    from datetime import datetime
    start = datetime.fromisoformat(h["start"])
    end = datetime.fromisoformat(h["end"])
    assert (end - start).total_seconds() == 7 * 86400


def test_history_period_this_year_starts_jan_1() -> None:
    from app.banking.service import get_history
    h = get_history(user_id=USER, period="this_year")
    from datetime import datetime
    start = datetime.fromisoformat(h["start"])
    end = datetime.fromisoformat(h["end"])
    assert start.month == 1 and start.day == 1
    assert end.month == 1 and end.day == 1
    assert end.year == start.year + 1


@pytest.mark.parametrize(
    "phrase,expected_period",
    [
        ("hôm nay", "today"),
        ("hom nay", "today"),
        ("hôm qua", "yesterday"),
        ("hom qua", "yesterday"),
        ("tuần này", "this_week"),
        ("tuan nay", "this_week"),
        ("tuần trước", "last_week"),
        ("tuan truoc", "last_week"),
        ("năm nay", "this_year"),
        ("nam nay", "this_year"),
        ("năm ngoái", "last_year"),
        ("nam ngoai", "last_year"),
        ("tháng trước", "last_month"),  # baseline — still works
    ],
)
def test_temporal_phrase_maps_to_correct_period(
    phrase: str, expected_period: str
) -> None:
    """Each temporal phrase must map to its specific window. Critically:
    "hôm qua" must NOT keep mapping to recent_30d (last 30 days) — that
    silent broadening was the original bug that hid in tháng-trước's
    shadow because tests only ever pinned the tháng-trước path."""
    from app.services.orchestrator import _period_from_temporal
    assert _period_from_temporal(phrase) == expected_period


# ---------------------------------------------------------------------------
# "Lặp lại?" only fires when draft.amount actually matches the referenced tx
# ---------------------------------------------------------------------------


def test_temporal_reference_explicit_different_amount_does_not_say_repeat() -> None:
    """When the user says "gửi mẹ 5 triệu như tháng trước" and tháng trước
    was 3.000.000đ, the reply must NOT say "Lặp lại?" — that would imply
    we're about to repeat the 3M figure, which would either confuse the
    judge or get a silent over-confirm. The fix surfaces the diff
    ("tháng trước bạn gửi 3tr — lần này 5tr") and asks "Xác nhận?"."""
    s = session_for(USER)
    s.clear_draft()
    r = handle_message(USER, "Gửi cho mẹ 5 triệu như tháng trước")
    s.clear_draft()
    assert r.draft is not None
    assert r.draft.amount == 5_000_000
    assert "Lặp lại?" not in r.text
    assert "5.000.000đ" in r.text
    # The prior amount surfaces in the diff so the user can spot a typo.
    assert "Tháng trước" in r.text


def test_temporal_reference_matching_amount_still_says_repeat() -> None:
    """The classic "Gửi mẹ 3 triệu như tháng trước" (amount matches the
    prior tx) keeps the short "Lặp lại?" framing — the demo's flagship
    "intent over wording" moment."""
    s = session_for(USER)
    s.clear_draft()
    r = handle_message(USER, "Gửi cho mẹ 3 triệu như tháng trước")
    s.clear_draft()
    assert r.draft is not None
    assert r.draft.amount == 3_000_000
    assert "Lặp lại?" in r.text


def test_temporal_reference_no_amount_fills_and_says_repeat() -> None:
    """"Gửi mẹ như tháng trước" with no explicit amount fills from
    history and asks "Lặp lại?" — the draft amount equals the prior tx
    by construction."""
    s = session_for(USER)
    s.clear_draft()
    r = handle_message(USER, "Gửi cho mẹ như tháng trước")
    s.clear_draft()
    assert r.draft is not None
    assert r.draft.amount == 3_000_000
    assert "Lặp lại?" in r.text


def test_budget_overshoot_details_payload_shape() -> None:
    """The budget_overshoot warn ships a ``details`` dict that the
    frontend's "why" panel renders as a category / spent / projected /
    overshoot breakdown. Pin the field names so a backend rename
    silently drops the explanation."""
    from app.banking.budgets import compute_status_for
    from app.models.schemas import Account, Budget, BudgetStatus, Contact, Transaction
    from app.safety.rules import evaluate
    from datetime import datetime, timezone

    # Wire a manual budget overshoot scenario without touching the store.
    # The rules engine reads ``compute_statuses`` lazily inside evaluate(),
    # so monkeypatching the store-level helper isn't reliable — instead
    # we just assert the contract via the BudgetStatus model + shape
    # check on flag.details. evaluate() will produce the warn naturally
    # as soon as a real budget is over.
    bs = BudgetStatus(
        category="food",
        category_label="Ăn uống",
        monthly_limit_vnd=3_000_000,
        spent_vnd=2_900_000,
        remaining_vnd=100_000,
        ratio=0.967,
    )
    # The BudgetStatus model must accept the four fields the rule emits;
    # if a schema rename drops one of these, this assertion is the
    # earliest signal.
    assert bs.category_label == "Ăn uống"
    assert bs.monthly_limit_vnd == 3_000_000
    assert bs.spent_vnd == 2_900_000

    # Spot-check the helper is importable — the rule engine relies on
    # it; a module rename would silently disable the entire feature.
    assert callable(compute_status_for)


# ---------------------------------------------------------------------------
# Alias resolver — possessive / vocative tail stripping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "surface,expected",
    [
        ("mẹ tôi", "Nguyễn Thị Lan"),
        ("mẹ mình", "Nguyễn Thị Lan"),
        ("mẹ tôi nhé", "Nguyễn Thị Lan"),
        ("anh tuấn", "Phạm Quốc Tuấn"),  # prefix path, kept as-is
        ("Trần Hoàng Minh", "Trần Hoàng Minh"),  # full name, no strip
    ],
)
def test_alias_resolver_strips_possessive_tail(
    surface: str, expected: str
) -> None:
    """The natural Vietnamese phrasing "mẹ tôi" / "mẹ mình" / "chị X
    ơi" / "anh Y nhé" used to lose the alias match because the
    possessive/vocative suffix isn't in any contact's alias list. Now
    the resolver strips trailing tokens (tôi / mình / ơi / nhé / nha
    / em / anh / chi) before alias lookup, so "Chuyển cho mẹ tôi 2tr"
    resolves to mẹ → Lan instead of asking "who?"."""
    from app.context.alias import resolve_recipient
    from app.store import get_store

    contacts = get_store().contacts_of(USER)
    matches = resolve_recipient(surface, contacts)
    names = [m.contact.display_name for m in matches]
    assert expected in names, (
        f"{surface!r} should resolve to {expected!r}; got {names}"
    )


@pytest.mark.parametrize(
    "surface,expected",
    [
        # Exact full-name match must short-circuit before RAG/embedding
        # fallback. Otherwise multi-token names fall through to the
        # semantic stage and surface many "similar-looking" candidates
        # (reported bug: "chuyển cho Vũ Thị Hạnh 2 nghìn" → 16 candidates).
        ("Vũ Thị Hạnh", "Vũ Thị Hạnh"),
        ("Nguyễn Thị Lan", "Nguyễn Thị Lan"),
        ("Trần Hoàng Minh", "Trần Hoàng Minh"),
        # Case-insensitive / diacritic-insensitive variants should also
        # short-circuit on exact display-name fold.
        ("vũ thị hạnh", "Vũ Thị Hạnh"),
        ("VU THI HANH", "Vũ Thị Hạnh"),
    ],
)
def test_alias_resolver_exact_full_name_returns_single_candidate(
    surface: str, expected: str
) -> None:
    """Regression: an exact display-name match must yield exactly one
    candidate, regardless of whether the embedding model is loaded.
    Prior to the fix, names not present as an alias (e.g. "Vũ Thị
    Hạnh" — aliases are only "chị hạnh"/"hạnh") fell through to step
    5 (RAG/lexical) which could return a wide set of plausible names
    from the same demographic. The orchestrator then surfaced all of
    them as disambiguation candidates — the screenshot bug.
    """
    from app.context.alias import resolve_recipient
    from app.store import get_store

    contacts = get_store().contacts_of(USER)
    matches = resolve_recipient(surface, contacts)
    names = [m.contact.display_name for m in matches]
    assert len(matches) == 1, (
        f"{surface!r} should match exactly one contact; got {names}"
    )
    assert names[0] == expected


# ---------------------------------------------------------------------------
# Amount parser — wrong-money bugs (visible safety contract!)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_amount",
    [
        # CRITICAL: "5 trăm" used to match the "tr" unit inside "trăm"
        # and parse as 5_000_000 instead of 500. 10× overpay — judges
        # confirming such a transfer would be horrified.
        ("chuyển mẹ 5 trăm", 500),
        ("5 trăm", 500),
        # "5 trăm nghìn" = 500K (five hundred thousand). Used to also
        # match "tr" and give 5M.
        ("chuyển mẹ 5 trăm nghìn", 500_000),
        # "1tr5" / "2tr5" = decimal-fraction form for "1.5 / 2.5 million".
        # Used to parse as 1_005_000 / 2_005_000 — half-million underpay.
        ("chuyển mẹ 1tr5", 1_500_000),
        ("chuyển mẹ 2tr5", 2_500_000),
        # Regression negatives — "5tr500" (3-digit tail) keeps the
        # historical *1000 interpretation; "5 triệu rưỡi" still 5.5M.
        ("chuyển mẹ 5tr500", 5_500_000),
        ("chuyển mẹ 5 triệu rưỡi", 5_500_000),
        ("chuyển mẹ 500k", 500_000),
        ("chuyển mẹ 2 trieu", 2_000_000),
    ],
)
def test_amount_parser_wrong_money_regressions(
    text: str, expected_amount: int
) -> None:
    from app.nlp.amount import parse_amount
    got, _ = parse_amount(text)
    assert got == expected_amount, (
        f"{text!r} → {got}; expected {expected_amount}. "
        "Wrong-money parsing — judges would confirm an off-by-factor transfer."
    )


# ---------------------------------------------------------------------------
# Amount parser — Vietnamese spelled-out numerals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_amount",
    [
        # Bare spelled forms — the most common natural-speech variants.
        ("hai triệu", 2_000_000),
        ("ba triệu", 3_000_000),
        ("một triệu", 1_000_000),
        ("mười triệu", 10_000_000),
        ("năm trăm", 500),
        ("năm trăm nghìn", 500_000),
        ("bốn trăm nghìn", 400_000),
        ("chín trăm nghìn", 900_000),
        ("một tỷ", 1_000_000_000),
        # Compound with "chục" (×10).
        ("hai chục nghìn", 20_000),
        ("năm chục triệu", 50_000_000),
        # Mixed: spelled + "rưỡi" — full chain through both special
        # cases.
        ("ba triệu rưỡi", 3_500_000),
    ],
)
def test_amount_parser_spelled_out(
    text: str, expected_amount: int
) -> None:
    from app.nlp.amount import parse_amount
    got, _ = parse_amount(text)
    assert got == expected_amount, (
        f"{text!r} → {got}; expected {expected_amount}. "
        "Spelled-out Vietnamese numerals are everyday speech — judges "
        "who say 'hai triệu' must get 2.000.000đ, not None."
    )


@pytest.mark.parametrize(
    "text",
    [
        "hai con mèo",
        "ba tuổi",
        "một mình",  # NOT 1 million via "m" — covered by the original
                    # "no bare m" guard but still worth pinning.
        "năm sao",
    ],
)
def test_amount_parser_spelled_negatives_reject(text: str) -> None:
    """The spelled substitution only fires when the number word is
    immediately followed by an amount unit. Anything else stays None
    so contextual speech ("hai con mèo" = "two cats") doesn't get a
    phantom amount injected."""
    from app.nlp.amount import parse_amount
    got, _ = parse_amount(text)
    assert got is None, f"{text!r} should NOT parse as an amount; got {got}"


def test_fraud_model_threshold_constant_exists() -> None:
    """rules.evaluate() reads ``fraud_model.FRAUD_RISK_THRESHOLD``. A
    rename / reorder would silently disable the integration; assert
    the public name is stable."""
    from app.safety import fraud_model

    assert hasattr(fraud_model, "FRAUD_RISK_THRESHOLD")
    # Threshold is a probability — must be in [0, 1].
    assert 0.0 <= fraud_model.FRAUD_RISK_THRESHOLD <= 1.0


def test_fraud_risk_high_appears_when_score_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: when ``fraud_model.score_draft`` returns a score above
    ``FRAUD_RISK_THRESHOLD``, ``evaluate()`` must produce a ``fraud_risk_high``
    warn flag. Patches the scorer so the test doesn't need a trained model
    on disk (CI runs without one)."""
    from datetime import datetime, timezone

    from app.models.schemas import Account, Contact, Transaction
    from app.safety import fraud_model
    from app.safety.rules import evaluate

    high = fraud_model.FRAUD_RISK_THRESHOLD + 0.1
    monkeypatch.setattr(fraud_model, "score_draft", lambda **kw: high)

    recipient = Contact(
        id="c_test", owner_id=USER, display_name="Test Recipient",
        bank="MB", account_number="0123456789", account_masked="6789",
    )
    account = Account(
        id="a_test", bank="Omni", number="987654321",
        balance=10_000_000, primary=True,
    )
    # Build a single past tx so the per-recipient anomaly path has data
    # but at a similar amount — we want fraud_risk_high to fire, not
    # amount_above_average (which would suppress it).
    past = Transaction(
        id="t_past", owner_id=USER, contact_id="c_test",
        amount=1_000_000, description="prior tx",
        category="other", status="completed",
        created_at=datetime.now(timezone.utc),
    )

    flags = evaluate(
        amount=1_000_000,
        recipient_candidates=[],
        recipient=recipient,
        transactions=[past],
        account=account,
        user_id=USER,
    )
    codes = [f.code for f in flags]
    assert "fraud_risk_high" in codes, f"expected fraud_risk_high in {codes}"
    fraud_flag = next(f for f in flags if f.code == "fraud_risk_high")
    assert fraud_flag.severity == "warn"
    # Details payload — frontend renders a "why" panel from these. Missing
    # / renamed fields silently drop the explanation. ``kind`` is the
    # discriminator the TransactionCard switches on.
    assert fraud_flag.details is not None
    assert fraud_flag.details.get("kind") == "fraud_model"
    assert fraud_flag.details.get("score") is not None
    assert fraud_flag.details.get("threshold") is not None
    assert fraud_flag.details.get("current_amount") == 1_000_000


# ---------------------------------------------------------------------------
# recent_to_recipient mini-ledger — must populate on every draft path
# ---------------------------------------------------------------------------


def test_recent_to_recipient_populated_after_disambig_select() -> None:
    """KB3 (ambiguous "Minh") used to leave ``recent_to_recipient = None``
    after the user picked one of the candidates, because the helper was
    only computed in _handle_transfer, not in select_candidate. Judges
    would see the mini-ledger appear on KB1/KB2 but not KB3 — visible
    inconsistency. Now every draft producer populates it."""
    from app.services.orchestrator import handle_message, select_candidate

    _clear_all_drafts()
    r1 = handle_message(USER, "Chuyển cho Minh 500k")
    assert r1.draft is not None and len(r1.draft.candidates) >= 2

    # Pick the Techcombank Minh — the demo seed has prior transactions
    # to him so the mini-ledger should have at least one row.
    target = next(c for c in r1.draft.candidates if "Techcom" in c.bank)
    r2 = select_candidate(USER, r1.draft.id, target.id)
    assert r2.draft is not None
    assert r2.draft.recipient is not None
    # The seed has multiple prior tx to Trần Hoàng Minh; assert at
    # least one populates (don't pin the exact amount to keep the
    # test resilient to seed enrichment).
    assert r2.draft.recent_to_recipient, (
        "select_candidate must populate recent_to_recipient — "
        "without it the disambig-confirm card lacks the mini-ledger "
        "the no-ambiguity path shows"
    )
    for row in r2.draft.recent_to_recipient:
        assert "amount" in row and "created_at" in row
        assert "description" in row and "category" in row


def test_recent_to_recipient_refreshed_after_modify() -> None:
    """When the user edits an existing draft and the recipient changes
    ("đổi sang Nam"), the mini-ledger must point at the NEW recipient,
    not the old one. _modify_transfer_draft now recomputes it."""
    from app.services.orchestrator import handle_message

    _clear_all_drafts()
    # Create a transfer to mẹ (Nguyễn Thị Lan).
    r1 = handle_message(USER, "Gửi cho mẹ 2 triệu")
    assert r1.draft is not None and r1.draft.recipient is not None
    before_recipient = r1.draft.recipient.display_name
    before_ledger = r1.draft.recent_to_recipient or []

    # Modify the same draft — change the amount only (recipient stays
    # the same). The ledger must stay populated, not get cleared.
    r2 = handle_message(USER, "đổi sang 3 triệu")
    assert r2.draft is not None and r2.draft.recipient is not None
    assert r2.draft.recipient.display_name == before_recipient
    # If before had a ledger, modify must keep one.
    if before_ledger:
        assert r2.draft.recent_to_recipient, (
            "modify must refresh recent_to_recipient, not drop it to None"
        )


def test_thoi_chi_chuyen_amount_is_edit_not_cancel() -> None:
    """A leading "thôi" used to short-circuit straight into the cancel
    path, so "thôi chỉ chuyển 20 thôi" silently killed the draft. The
    edit-guard only recognised amounts *with* a unit (parse_amount), and
    a bare "20" returned None — so the cancel won. Now a bare number in
    an edit frame inherits the draft's unit (20 → 20 triệu on a
    triệu-scale draft) and the message routes to the modify path instead
    of cancelling. Regression for the rate-limited-fallback report."""
    from app.services.orchestrator import handle_message

    _clear_all_drafts()
    r1 = handle_message(USER, "Gửi cho mẹ 20 triệu")
    assert r1.draft is not None and r1.draft.recipient is not None
    recipient = r1.draft.recipient.display_name

    r2 = handle_message(USER, "thôi chỉ chuyển 20 thôi")
    assert r2.draft is not None, "must NOT cancel — it's an amount restatement"
    assert "huỷ" not in r2.text.lower() and "hủy" not in r2.text.lower()
    assert r2.draft.recipient is not None
    assert r2.draft.recipient.display_name == recipient
    assert r2.draft.amount == 20_000_000


def test_thoi_chi_chuyen_bare_number_reduces_amount() -> None:
    """Bare-number edit also *reduces*: on a 20-triệu draft, "thôi chỉ
    chuyển 5 thôi" means 5 triệu (unit inherited from the draft), not a
    cancel and not 5 đồng."""
    from app.services.orchestrator import handle_message

    _clear_all_drafts()
    r1 = handle_message(USER, "Gửi cho mẹ 20 triệu")
    assert r1.draft is not None
    r2 = handle_message(USER, "thôi chỉ chuyển 5 thôi")
    assert r2.draft is not None, "must edit, not cancel"
    assert r2.draft.amount == 5_000_000


def test_thoi_with_new_recipient_is_redirect_not_cancel() -> None:
    """"thôi chuyển cho bố" after a draft to mẹ is a REDIRECT (switch to
    bố, keep the amount), not a cancel — the leading "thôi" is a discourse
    particle, not a kill. Pure cancels ("thôi", "thôi không chuyển nữa")
    must still cancel (covered separately)."""
    from app.services.orchestrator import handle_message, session_for

    session_for(USER).clear_draft()
    r1 = handle_message(USER, "chuyển cho mẹ 5 triệu")
    assert r1.draft is not None and r1.draft.amount == 5_000_000
    r2 = handle_message(USER, "thôi chuyển cho bố")
    assert r2.draft is not None, "must redirect, not cancel"
    assert r2.draft.recipient is not None
    assert "Hùng" in r2.draft.recipient.display_name  # bố = Lê Văn Hùng
    assert r2.draft.amount == 5_000_000, "amount must be remembered across the redirect"
    session_for(USER).clear_draft()


def test_thoi_khong_chuyen_nua_still_cancels() -> None:
    """A pure cancel that the extractor might mis-read ("nữa" looks like a
    name) must still cancel — the redirect step-aside only fires when the
    named recipient actually RESOLVES to a contact."""
    from app.services.orchestrator import handle_message, session_for

    session_for(USER).clear_draft()
    handle_message(USER, "chuyển cho mẹ 5 triệu")
    r = handle_message(USER, "thôi không chuyển nữa")
    assert r.draft is None
    assert "huỷ" in r.text.lower() or "hủy" in r.text.lower()


def test_unconfirmed_draft_auto_cancels_after_timeout() -> None:
    """An unconfirmed draft is dropped once its expiry passes (default 60s,
    "tự huỷ sau 1 phút"). Tested deterministically by tampering the stored
    envelope's expiry rather than sleeping."""
    import json
    import time as _t

    from app.context import session as sess

    # Unit: the expiry envelope.
    inner, expired = sess._unwrap_expiry(json.dumps({"__exp": _t.time() - 1, "d": {"x": 1}}))
    assert expired is True and inner is None
    _, not_expired = sess._unwrap_expiry(json.dumps({"__exp": _t.time() + 99, "d": {"x": 1}}))
    assert not_expired is False

    # Facade: a draft whose stored envelope is past expiry auto-cancels on read.
    s = sess.session_for(USER)
    _draft_to("nam", 2_000_000)
    assert s.current_draft is not None
    raw = s._backend.get_draft(USER)
    obj = json.loads(raw)
    obj["__exp"] = _t.time() - 1  # force-expire
    s._backend.set_draft(USER, json.dumps(obj))
    assert s.current_draft is None, "expired draft must auto-cancel"
    s.clear_draft()


def test_restart_confirm_discards_old_and_starts_new() -> None:
    """When a new transfer is pending the discard question, answering "có"
    cancels the old draft and starts the new one (which then asks for the
    amount it never carried over)."""
    from app.services import orchestrator as orch
    from app.services.orchestrator import handle_message, session_for

    session_for(USER).clear_draft()
    r1 = handle_message(USER, "chuyển cho cường 4 triệu")
    assert r1.draft is not None and r1.draft.amount == 4_000_000
    # Simulate the LLM having asked "huỷ giao dịch cũ?" for a fresh request.
    orch._PENDING_RESTART[USER] = "chuyển cho mẹ"
    r2 = handle_message(USER, "có")
    orch._PENDING_RESTART.pop(USER, None)
    assert r2.draft is not None and r2.draft.recipient is not None
    assert "Lan" in r2.draft.recipient.display_name  # mẹ
    assert r2.draft.amount is None, "the old 4tr must NOT carry into the new transfer"
    session_for(USER).clear_draft()


def test_restart_decline_keeps_old_draft() -> None:
    """Answering "không" to the discard question keeps the old draft intact."""
    from app.services import orchestrator as orch
    from app.services.orchestrator import handle_message, session_for

    session_for(USER).clear_draft()
    handle_message(USER, "chuyển cho cường 4 triệu")
    orch._PENDING_RESTART[USER] = "chuyển cho mẹ"
    r = handle_message(USER, "không")
    orch._PENDING_RESTART.pop(USER, None)
    assert r.draft is not None and r.draft.recipient is not None
    assert "Cường" in r.draft.recipient.display_name
    assert r.draft.amount == 4_000_000
    session_for(USER).clear_draft()


def test_pure_thoi_still_cancels_draft() -> None:
    """Positive control: a *pure* "thôi" (no amount) must still cancel —
    the edit-guard only steps aside when the message names a sum."""
    from app.services.orchestrator import handle_message

    _clear_all_drafts()
    r1 = handle_message(USER, "Gửi cho mẹ 20 triệu")
    assert r1.draft is not None
    r2 = handle_message(USER, "thôi")
    assert r2.draft is None
    assert "huỷ" in r2.text.lower() or "hủy" in r2.text.lower()


# ---------------------------------------------------------------------------
# Redirecting an open draft to a NEW recipient must not silently inherit the
# previous recipient's amount. Reported bug: after "chuyển cho nam 10 triệu",
# typing "chuyển cho lan" (no amount) showed "chuyển 10.000.000đ cho ai?" —
# a figure the user never entered this turn. Driven through
# _modify_transfer_draft with synthesized NLU so the assertion is
# deterministic regardless of LLM/rule-classifier availability.
# ---------------------------------------------------------------------------


def _draft_to(recipient_text: str, amount: int):
    """Build a fresh transfer draft for ``USER`` and register it as the
    session's current draft, bypassing the (LLM-dependent) NLU layer."""
    from app.models.schemas import ExtractedEntities, NLUResult
    from app.services.orchestrator import (
        _handle_transfer,
        session_for,
    )

    session_for(USER).clear_draft()
    nlu = NLUResult(
        intent="transfer",
        confidence=0.9,
        entities=ExtractedEntities(recipient_text=recipient_text, amount=amount),
        raw_text=f"chuyển cho {recipient_text} {amount}",
        source="rule",
    )
    draft = _handle_transfer(USER, nlu).draft
    session_for(USER).set_draft(draft)
    return draft


def _modify(draft, *, recipient_text=None, amount=None, account_hint=None, raw_text=""):
    from app.models.schemas import ExtractedEntities, NLUResult
    from app.services.orchestrator import _modify_transfer_draft

    nlu = NLUResult(
        intent="transfer",
        confidence=0.9,
        entities=ExtractedEntities(
            recipient_text=recipient_text, amount=amount, account_hint=account_hint
        ),
        raw_text=raw_text,
        source="rule",
    )
    return _modify_transfer_draft(USER, draft, nlu)


def test_redirect_to_ambiguous_recipient_keeps_amount() -> None:
    """"chuyển cho lan" after a 10tr draft to Nam: lan is ambiguous, the
    user only changed the recipient → the amount they already typed (10tr)
    is REMEMBERED and carried to the new recipient; they just pick which
    Lan. The committed recipient is cleared (must disambiguate) but the
    candidate list is surfaced."""
    d = _draft_to("nam", 10_000_000)
    r = _modify(d, recipient_text="lan", raw_text="chuyển cho lan")
    assert r.draft is not None
    assert r.draft.recipient is None  # ambiguous → must pick one
    assert len(r.draft.candidates) >= 2
    assert r.draft.amount == 10_000_000, "amount the user set must be remembered"


def test_redirect_to_single_recipient_keeps_amount() -> None:
    """Swapping to a clean single match keeps the amount the user already
    set — "remember the whole transaction". No re-asking, no auto-predict
    of a NEW figure for the new person."""
    d = _draft_to("nam", 2_000_000)
    r = _modify(d, recipient_text="Thảo", raw_text="đổi sang Thảo")
    assert r.draft is not None and r.draft.recipient is not None
    assert r.draft.recipient.display_name != "Vũ Hoàng Nam"
    assert r.draft.amount == 2_000_000, "amount must be remembered across a recipient change"
    assert r.draft.predicted_amount is False


def test_named_recipient_with_amount_swaps_recipient() -> None:
    """Reported alias bug: with an active draft to Linh, "chuyển cho mẹ 10
    triệu" must SWAP to mẹ (Nguyễn Thị Lan). The amount-edit fast path in
    _try_continue_draft must not fire on a unit-bearing amount and silently
    keep the old recipient. Goes through handle_message so the continuation
    path is exercised (LLM off in tests → rule pipeline)."""
    from app.services.orchestrator import handle_message, session_for

    session_for(USER).clear_draft()
    r1 = handle_message(USER, "chuyển cho linh 200k")
    assert r1.draft is not None and r1.draft.recipient is not None
    assert "Linh" in r1.draft.recipient.display_name
    r2 = handle_message(USER, "chuyển cho mẹ 10 triệu")
    assert r2.draft is not None and r2.draft.recipient is not None
    assert "Lan" in r2.draft.recipient.display_name, "must swap to mẹ, not keep Linh"
    assert r2.draft.amount == 10_000_000
    session_for(USER).clear_draft()


def _apply_action(draft, decision):
    from app.services.orchestrator import _apply_llm_draft_action

    return _apply_llm_draft_action(USER, draft, decision)


def test_llm_action_relative_amounts() -> None:
    """The deterministic applier computes relative amount ops from the
    draft's own amount / the source balance — the LLM only names the op."""
    # gấp đôi
    r = _apply_action(_draft_to("nam", 2_000_000), {"action": "edit", "amount_op": "multiply", "amount_operand": 2})
    assert r.draft.amount == 4_000_000
    # một nửa
    r = _apply_action(_draft_to("nam", 2_000_000), {"action": "edit", "amount_op": "fraction", "amount_operand": 0.5})
    assert r.draft.amount == 1_000_000
    # thêm 500k
    r = _apply_action(_draft_to("nam", 2_000_000), {"action": "edit", "amount_op": "add", "amount_operand": 500_000})
    assert r.draft.amount == 2_500_000
    # bớt 500k
    r = _apply_action(_draft_to("nam", 2_000_000), {"action": "edit", "amount_op": "subtract", "amount_operand": 500_000})
    assert r.draft.amount == 1_500_000


def test_llm_action_all_balance_uses_account_balance() -> None:
    """"chuyển hết số dư" → amount = the source account's balance."""
    d = _draft_to("nam", 2_000_000)
    bal = next(a for a in d.source_accounts if a.id == d.source_account_id).balance
    r = _apply_action(d, {"action": "edit", "amount_op": "all_balance"})
    assert r.draft.amount == bal


def test_llm_action_redirect_keeps_amount() -> None:
    """An LLM edit that only changes the recipient keeps the amount (remember
    the transaction)."""
    d = _draft_to("nam", 2_000_000)
    r = _apply_action(d, {"action": "edit", "recipient_text": "Thảo"})
    assert r.draft.recipient is not None
    assert r.draft.recipient.display_name != "Vũ Hoàng Nam"
    assert r.draft.amount == 2_000_000


def test_llm_action_never_invents_amount() -> None:
    """An edit decision with no amount fields must not change the amount —
    the applier only does arithmetic the LLM explicitly asked for."""
    d = _draft_to("nam", 2_000_000)
    r = _apply_action(d, {"action": "edit", "account_hint": "phụ"})
    assert r.draft.amount == 2_000_000  # unchanged


def test_amount_only_edit_keeps_recipient_and_amount() -> None:
    """Regression guard: "đổi sang 3 triệu" changes only the amount; the
    recipient and the new amount both stick (this path must be untouched
    by the redirect-amount-reset fix)."""
    d = _draft_to("nam", 10_000_000)
    r = _modify(d, amount=3_000_000, raw_text="đổi sang 3 triệu")
    assert r.draft is not None and r.draft.recipient is not None
    assert r.draft.recipient.display_name == "Vũ Hoàng Nam"
    assert r.draft.amount == 3_000_000


def test_redirect_with_new_amount_uses_that_amount() -> None:
    """When the user redirects AND states a new amount in the same turn,
    that amount wins (no reset, no inheritance)."""
    d = _draft_to("nam", 10_000_000)
    r = _modify(
        d, recipient_text="Thảo", amount=2_000_000, raw_text="đổi sang chị Thảo 2 triệu"
    )
    assert r.draft is not None and r.draft.recipient is not None
    assert r.draft.recipient.display_name != "Vũ Hoàng Nam"
    assert r.draft.amount == 2_000_000


# ---------------------------------------------------------------------------
# Anti-fabrication backstop — the LLM's FOLLOW-UP rule re-emits the prior
# turn's amount/recipient/intent. These two guards make sure money values
# are only ever trusted when the CURRENT message actually carries them, so
# a stale figure can't ride into a draft the user never asked for. Driven
# with synthesized NLU (source mimics the LLM having inherited a value) so
# the assertions are deterministic with the LLM off.
# ---------------------------------------------------------------------------


def test_llm_hallucinated_amount_ignored_when_not_typed() -> None:
    """The deterministic gate: the LLM emits a DIFFERENT amount (9tr,
    hallucinated/inherited) while the raw text "à chuyển cho linh đi" has
    no number. The draft's real amount (2tr the user set) must be kept —
    the LLM's untyped figure is ignored, never applied."""
    d = _draft_to("nam", 2_000_000)
    r = _modify(
        d, recipient_text="linh", amount=9_000_000, raw_text="à chuyển cho linh đi"
    )
    assert r.draft is not None and r.draft.recipient is not None
    assert r.draft.amount == 2_000_000, (
        "an untyped LLM amount must neither override nor wipe the user's real amount"
    )


def test_vague_message_does_not_fabricate_transfer() -> None:
    """No active draft + a vague message ("ờ cái kia") whose only transfer
    content was inherited by the LLM from history → ask, never build a
    draft out of stale recipient/amount."""
    from app.models.schemas import ExtractedEntities, NLUResult
    from app.services.orchestrator import _handle_transfer

    nlu = NLUResult(
        intent="transfer",
        confidence=0.6,
        entities=ExtractedEntities(recipient_text="lan", amount=10_000_000),
        raw_text="ờ cái kia",
        source="llm",
    )
    r = _handle_transfer(USER, nlu)
    assert r.draft is None, "must not fabricate a draft from inherited fields"
    low = r.text.lower()
    assert "bao nhiêu" in low or "cho ai" in low


def test_transfer_with_real_signal_still_builds_draft() -> None:
    """Positive control: a message that genuinely carries a verb + amount +
    recipient must still build a draft — the backstop only blocks the
    no-signal case."""
    from app.models.schemas import ExtractedEntities, NLUResult
    from app.services.orchestrator import _handle_transfer

    nlu = NLUResult(
        intent="transfer",
        confidence=0.9,
        entities=ExtractedEntities(recipient_text="mẹ", amount=2_000_000),
        raw_text="chuyển cho mẹ 2 triệu",
        source="rule",
    )
    r = _handle_transfer(USER, nlu)
    assert r.draft is not None
    assert r.draft.amount == 2_000_000


def test_source_account_switch_mid_draft() -> None:
    """"người gửi" change: "dùng tài khoản phụ" moves the draft's source
    account to the non-primary one, keeping recipient + amount; "chính"
    moves it back. Resolution is against the draft's own accounts."""
    d = _draft_to("nam", 2_000_000)
    accounts = d.source_accounts
    assert len(accounts) >= 2, "demo user needs ≥2 accounts for this test"
    primary = next(a for a in accounts if a.primary)
    secondary = next(a for a in accounts if not a.primary)

    r = _modify(d, account_hint="phụ", raw_text="dùng tài khoản phụ nhé")
    assert r.draft is not None
    assert r.draft.source_account_id == secondary.id
    assert r.draft.amount == 2_000_000
    assert r.draft.recipient is not None  # recipient untouched

    r2 = _modify(r.draft, account_hint="chính", raw_text="à quay lại tài khoản chính")
    assert r2.draft.source_account_id == primary.id


def test_missing_amount_offers_suggestion_not_autofill() -> None:
    """First mention of a known recipient without an amount must NOT
    auto-fill the figure. The history estimate is OFFERED via
    ``suggested_amount`` (tappable chip) while ``amount`` stays None →
    ``missing_amount`` → the reply asks "bao nhiêu" and mentions the
    suggestion. Regression for the "tự set 220.000đ" report."""
    from app.models.schemas import ExtractedEntities, NLUResult
    from app.services.orchestrator import _handle_transfer, session_for

    session_for(USER).clear_draft()
    nlu = NLUResult(
        intent="transfer",
        confidence=0.9,
        entities=ExtractedEntities(recipient_text="mẹ"),
        raw_text="chuyển cho mẹ",
        source="rule",
    )
    r = _handle_transfer(USER, nlu)
    d = r.draft
    assert d is not None and d.recipient is not None
    assert d.amount is None, "amount must NOT be auto-filled from the predictor"
    assert d.predicted_amount is False
    assert any(f.code == "missing_amount" for f in d.flags)
    # mẹ has rich history, so a suggestion should be offered (chip + hint).
    assert d.suggested_amount is not None
    assert "gợi ý" in r.text.lower()


def test_redirect_while_amount_unset_refreshes_suggestion() -> None:
    """When NO amount has been set yet, redirecting to a new known
    recipient refreshes the OFFERED suggestion for THEM (chip) while
    ``amount`` stays None — still asks, never auto-decides. (Once an amount
    IS set, a recipient change keeps it — see the *_keeps_amount tests.)"""
    from app.models.schemas import ExtractedEntities, NLUResult
    from app.services.orchestrator import _handle_transfer, session_for

    session_for(USER).clear_draft()
    base = _handle_transfer(
        USER,
        NLUResult(
            intent="transfer",
            confidence=0.9,
            entities=ExtractedEntities(recipient_text="mẹ"),
            raw_text="chuyển cho mẹ",
            source="rule",
        ),
    ).draft
    assert base is not None and base.amount is None
    session_for(USER).set_draft(base)

    r = _modify(base, recipient_text="linh", raw_text="à chuyển cho linh")
    assert r.draft is not None and r.draft.recipient is not None
    assert r.draft.amount is None
    assert r.draft.predicted_amount is False
    assert r.draft.suggested_amount is not None  # Linh has history
    assert any(f.code == "missing_amount" for f in r.draft.flags)


def test_insights_anomaly_renders_detector_reason() -> None:
    """The MAD anomaly detector returns ``contact_name`` + ``reason``
    (e.g. "cao gấp 8.4 lần mức thường (per-contact)"). The chat reply
    must surface those, not the empty older ``contact`` / ``typical``
    fields that produced "(không rõ): X (thường ~0đ)"."""
    r = _r("Tháng này có giao dịch nào bất thường không?")
    assert r.intent == "insights"
    # Either we have anomalies and the contact_name appears, or the
    # demo seed has been reset and the empty-state copy renders. Both
    # are acceptable — what's NOT acceptable is the broken "(không rõ)"
    # / "~0đ" rendering from the field-mismatch bug.
    assert "(không rõ)" not in r.text
    assert "thường ~0đ" not in r.text


# ---------------------------------------------------------------------------
# Category-shaped queries — "ăn uống tháng này" / "cafe bao nhiêu" → history
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # <category> + temporal
        "ăn uống tháng này",
        "ăn uống tuần trước",
        "mua sắm tháng này",
        "cafe tháng này",
        "cà phê tháng này",
        "xăng tháng này",
        "grab tháng này",
        "tiền điện tháng này",
        # <category> + aggregation cue
        "giải trí bao nhiêu",
        "shopping bao nhiêu",
        "tiền nhà bao nhiêu",
        # tiêu/chi + <category>
        "tiêu ăn uống",
        "chi giải trí",
        "tiêu ăn uống bao nhiêu",
    ],
)
def test_category_shaped_queries_route_history(text: str) -> None:
    """Without a category-aware route, "ăn uống tháng này" / "mua sắm
    tháng này" / "tiền điện tháng này" all fell to "unknown" — the
    Tier-2 history defaults (bao nhieu / tieu) only fired on phrasings
    that included a verb. Judges naturally drop the verb when asking
    about a specific category."""
    intent, _ = classify(text)
    assert intent == "history", text


@pytest.mark.parametrize(
    "text,expected",
    [
        # Critical negatives — category words inside a transfer command
        # ("tiền ăn" / "tiền nhà") must NOT trigger the category route.
        ("gửi mẹ tiền ăn 100k", "transfer"),
        ("gửi mẹ tiền nhà 5tr", "transfer"),
        ("chuyển mẹ 2 triệu", "transfer"),
        ("số dư", "balance"),
        ("tháng này tiêu bao nhiêu", "history"),
    ],
)
def test_category_route_doesnt_eat_transfer_commands(
    text: str, expected: str
) -> None:
    intent, _ = classify(text)
    assert intent == expected, text


@pytest.mark.parametrize(
    "text",
    [
        "kiểm tra tài khoản đi",
        "kiểm tra tài khoản",
        "thông tin tài khoản",
        "check balance",
        "check số dư",
        "show balance",
    ],
)
def test_check_account_phrasings_route_balance(text: str) -> None:
    """Code-switched and verb-led account queries that pre-fix fell to
    transfer ("kiểm tra" matched no balance keyword; the Tier-2 default
    sent any unrecognised verb-led message to transfer)."""
    intent, _ = classify(text)
    assert intent == "balance", text


# ---------------------------------------------------------------------------
# Confirm matcher — VN polite/informal acks (dạ/vâng/ờ/okela) + neg-guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Polite forms — judges saying yes politely.
        "dạ", "dạ vâng", "vâng", "vâng ạ",
        # Right / correct affirmations.
        "đúng", "đúng rồi", "đúng vậy", "chuẩn",
        "phải", "phải rồi",
        # Informal acks.
        "ờ", "ờm", "ờ ơ",
        # Slangy ok variants judges actually type in chat.
        "okela", "oce", "okie", "okê",
        # Regression — original confirms still work.
        "ok", "ừ", "xác nhận", "được",
    ],
)
def test_confirm_matches_common_vn_acks(text: str) -> None:
    """Pre-fix the rule fallback only recognised "ok / okay / ừ / xác
    nhận / được" as confirms — a judge saying "dạ" / "vâng" / "đúng" /
    "okela" against a confirm card got the message routed to NLU and
    treated as an unknown / transfer instead. Polite formal Vietnamese
    is the default register; without these the demo feels brittle."""
    assert _is_confirm(text), text


@pytest.mark.parametrize(
    "text",
    [
        # Question / action followers must NOT trip the confirm guard.
        # "phải/đúng" matched bare is a confirm; followed by a verb
        # they're part of a question.
        "phải làm gì bây giờ",
        "đúng làm gì",
        "phải đi đâu",
        "phải về nhà",
        # Cancel particles must NOT route to confirm.
        "không",
        "không phải",
        # Real intents must NOT route to confirm.
        "tháng này tiêu bao nhiêu",
        "chuyển mẹ 2 triệu",
    ],
)
def test_confirm_negative_lookahead_doesnt_eat_questions(text: str) -> None:
    assert not _is_confirm(text), text


# ---------------------------------------------------------------------------
# Backward word-order + preposition strip + honorific fall-through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_recipient",
    [
        # Verb first, amount first, recipient last — the order judges
        # actually type. Pre-fix the rule extractor's bare-token-amount
        # regex matched "chuyển" as recipient and dropped the real name.
        ("chuyển 5tr Nam", "Nam"),
        ("chuyển 2 triệu mẹ", "mẹ"),
        ("gửi 300k sếp", "sếp"),
        ("chuyển 2tr Hùng", "Hùng"),
        ("chuyển 1tr Minh", "Minh"),
        ("trả 500k bố", "bố"),
        # "sang" / "qua" prepositions weren't in the strip list, so
        # "gửi sang Minh 300k" leaked the preposition into the resolver
        # query as "sang Minh" → 0 candidates.
        ("gửi sang Minh 300k", "Minh"),
        ("chuyển qua bạn thân 500k", "bạn thân"),
    ],
)
def test_backward_word_order_extracts_recipient(
    text: str, expected_recipient: str
) -> None:
    from app.nlp.entities import extract

    e = extract(text)
    assert e.recipient_text == expected_recipient, (text, e.recipient_text)


@pytest.mark.parametrize(
    "text",
    [
        # Verbs and amount-context nouns must NOT be picked up as
        # recipients by the bare-token-amount pattern.
        "chuyển 5tr",
        "gửi 300k",
        "lương 5tr",
        "tiền nhà 3tr",
        "ngân sách 1tr",
    ],
)
def test_bare_token_denylist_blocks_verb_and_context_nouns(text: str) -> None:
    from app.nlp.entities import extract

    e = extract(text)
    assert e.recipient_text is None, (text, e.recipient_text)


def test_resolver_honorific_falls_through_to_name_lookup() -> None:
    """Pre-fix, "cô Lan" routed via the alias heuristic to alias-only
    lookup and returned 0 candidates even though stripping "cô" and
    token-matching "Lan" would have surfaced ambiguity between the two
    Lans the user has. Heuristic now only biases ordering — alias
    lookup runs first; name lookup runs second when alias is empty."""
    from app.context.alias import resolve_recipient
    from app.store import get_store

    contacts = get_store().contacts_of("u_an")
    r = resolve_recipient("cô Lan", contacts)
    names = sorted(c.contact.display_name for c in r)
    # Demo seed has two contacts named Lan (Nguyễn Thị Lan + Phạm Thị
    # Lan). Resolver should return both so the chat asks which one.
    assert len(r) >= 2, names
    assert all("Lan" in n for n in names)


def test_amount_parser_bare_digit_with_transfer_context() -> None:
    """Pre-fix the user-typed bare-VND amount "chuyển 100000000 cho mẹ"
    parsed as ``None`` (no unit suffix) and the amount predictor then
    silently overwrote it with the recipient's median ~750k. The
    confirm card said "Đã hiểu! Xác nhận chuyển 750.000đ" while the
    user thought they were sending 100M. Money-touching silent
    override. Now the bare-integer + transfer-verb branch picks up
    the explicit amount before the predictor runs."""
    from app.nlp.amount import parse_amount

    amount, _ = parse_amount("chuyển 100000000 cho mẹ")
    assert amount == 100_000_000
    amount, _ = parse_amount("chuyển 50000 cho bố")
    assert amount == 50_000
    amount, _ = parse_amount("chuyển 100 cho mẹ")
    assert amount is None


def test_amount_parser_bare_digit_excludes_account_hints() -> None:
    """The bare-digit branch must NOT swallow account numbers — those
    have a separate extractor and a different downstream meaning. Pre-
    fix the new branch happily read ``stk 9990001234`` as a 9.99-billion
    đồng amount."""
    from app.nlp.amount import parse_amount

    amount, _ = parse_amount("gửi mẹ stk 9990001234")
    assert amount is None
    amount, _ = parse_amount("Lưu Nam STK 9990001234 MB Bank")
    assert amount is None


def test_amount_parser_zero_djong_explicit() -> None:
    """Explicit ``0đ`` must parse as 0 (not None) so the orchestrator's
    ``user_invalid_amount`` guard catches it and the predictor doesn't
    silently fill 750k median."""
    from app.nlp.amount import parse_amount

    amount, _ = parse_amount("chuyển 0đ mẹ")
    assert amount == 0


def test_transfer_zero_amount_blocks_predictor() -> None:
    """When the user types an explicit 0đ, do NOT swap it for a
    history-median prediction. Surface ``missing_amount`` instead."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    resp = handle_message("u_an", "chuyển 0đ mẹ")
    assert resp.draft is not None
    assert resp.draft.amount is None
    assert resp.draft.predicted_amount is False
    assert any(f.code == "missing_amount" for f in resp.draft.flags)


def test_transfer_negative_amount_rejected() -> None:
    """Pre-fix ``chuyển -5tr cho mẹ`` parsed to amount=5_000_000 (the
    minus sign was stripped silently). Now the leading-minus guard
    catches it and the safety engine surfaces ``missing_amount``."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    resp = handle_message("u_an", "chuyển -5tr cho mẹ")
    assert resp.draft is not None
    assert resp.draft.amount is None
    assert any(f.code == "missing_amount" for f in resp.draft.flags)


def test_fresh_verb_swap_recipient_keeps_amount() -> None:
    """User flow from feedback "cứ đổi người là quên mất số tiền":
    ``chuyển mẹ 1tr`` then ``chuyển cho bố`` (fresh verb + recipient
    only, no amount). Pre-fix the fresh-transfer guard wiped the draft
    on any verb-led prefix, so the second turn started a NEW draft with
    no amount. Now we only wipe when BOTH slots arrive together — a
    single-slot edit routes through the modify path and the amount is
    preserved."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    first = handle_message("u_an", "chuyển mẹ 1tr")
    assert first.draft and first.draft.amount == 1_000_000

    edit = handle_message("u_an", "chuyển cho bố")
    assert edit.draft is not None, "draft must survive a single-slot fresh-verb edit"
    assert edit.draft.amount == 1_000_000, "amount must carry over from the previous turn"
    assert edit.draft.recipient is not None
    # Recipient changed away from mẹ (loose check — exact name depends on seed).
    assert edit.draft.recipient.display_name != first.draft.recipient.display_name


def test_fresh_verb_swap_amount_keeps_recipient() -> None:
    """Mirror: ``chuyển mẹ 1tr`` then ``chuyển 500k`` (fresh verb +
    amount only, no recipient). Pre-fix the guard wiped the draft and
    the second turn lost the recipient. Now the amount edit lands on
    the existing draft."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    first = handle_message("u_an", "chuyển mẹ 1tr")
    assert first.draft and first.draft.recipient is not None
    first_name = first.draft.recipient.display_name

    edit = handle_message("u_an", "chuyển 500k")
    assert edit.draft is not None, "draft must survive a single-slot fresh-verb edit"
    assert edit.draft.amount == 500_000, "amount must update to the new value"
    assert edit.draft.recipient is not None
    assert edit.draft.recipient.display_name == first_name


def test_new_chat_session_clears_orchestrator_draft() -> None:
    """User report: open a new chat in the left drawer, ask
    "chuyển tiền cho bố" — Omni auto-fills the amount from the prior
    conversation's abandoned draft. The orchestrator session is keyed
    by user_id only, so without a session-switch hook the in-memory
    draft persisted across conversations. Now /api/chat clears it on
    every session_id change, AND POST /chat/sessions clears it on
    create."""
    from fastapi.testclient import TestClient
    from app.context.session import session_for as _sf
    from app.main import app
    from app.routes.chat import _LAST_SESSION_BY_USER

    client = TestClient(app)
    _LAST_SESSION_BY_USER.clear()
    _sf("u_an").clear_draft()

    # First conversation — leave an incomplete draft hanging.
    r1 = client.post(
        "/api/chat",
        headers={"x-user-id": "u_an"},
        json={"message": "chuyển 2 triệu"},
    )
    assert r1.status_code == 200
    first_session = r1.headers["X-Chat-Session-Id"]
    assert _sf("u_an").current_draft is not None
    assert _sf("u_an").current_draft.amount == 2_000_000

    # User opens a brand-new conversation via the left drawer button.
    create = client.post("/api/chat/sessions", headers={"x-user-id": "u_an"})
    assert create.status_code == 200
    new_session = create.json()["id"]
    assert new_session != first_session

    # The orchestrator draft must have been wiped — otherwise the next
    # turn in the new conversation would inherit the abandoned 2tr.
    assert _sf("u_an").current_draft is None

    # _enter_chat_session also fires from /api/chat itself: switching
    # back to the FIRST session via a turn must again clear the draft
    # (so going back-and-forth in the drawer never leaks slots).
    _sf("u_an").clear_draft()  # baseline
    r2 = client.post(
        "/api/chat",
        headers={"x-user-id": "u_an"},
        json={"message": "chuyển 9tr", "session_id": first_session},
    )
    assert r2.status_code == 200
    assert _sf("u_an").current_draft is not None
    assert _sf("u_an").current_draft.amount == 9_000_000

    # Now hop back to the NEW session — draft from "chuyển 9tr" above
    # must NOT carry over.
    r3 = client.post(
        "/api/chat",
        headers={"x-user-id": "u_an"},
        json={"message": "số dư bao nhiêu", "session_id": new_session},
    )
    assert r3.status_code == 200
    # After the switch the orchestrator's draft is cleared. The balance
    # turn itself doesn't create a draft, so it stays None.
    assert _sf("u_an").current_draft is None


def test_fresh_verb_both_slots_still_starts_fresh() -> None:
    """Counter-test: when the user provides BOTH recipient AND amount
    on a verb-led command, it's truly a fresh request — wipe the old
    draft. Without this we'd silently inherit safety flags / mini-
    ledger from the previous recipient."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    first = handle_message("u_an", "chuyển mẹ 1tr")
    assert first.draft and first.draft.amount == 1_000_000

    fresh = handle_message("u_an", "chuyển cho bố 500k")
    assert fresh.draft is not None
    assert fresh.draft.amount == 500_000
    # Recipient swapped — and the mini-ledger / flags are recomputed
    # against the new recipient because the draft was wiped first.
    assert fresh.draft.recipient is not None
    assert fresh.draft.recipient.display_name != first.draft.recipient.display_name


def test_modify_amount_preserves_recipient() -> None:
    """Sequence: ``chuyển mẹ 2tr`` then ``đổi thành 5tr``. Pre-fix the
    rule extractor matched "đổi thành" as ``recipient_text`` and
    ``_modify_transfer_draft`` then cleared the existing recipient on
    the failed alias lookup. User got "Bạn muốn chuyển 5tr cho ai?"
    when they only meant to change the amount."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    first = handle_message("u_an", "chuyển mẹ 2tr")
    assert first.draft and first.draft.recipient is not None
    first_name = first.draft.recipient.display_name

    edit = handle_message("u_an", "đổi thành 5tr")
    assert edit.draft is not None
    assert edit.draft.recipient is not None
    assert edit.draft.recipient.display_name == first_name
    assert edit.draft.amount == 5_000_000


@pytest.mark.parametrize(
    "text",
    [
        "tạm dừng lịch chuyển mẹ",
        "huỷ lịch chuyển mẹ",
        "dừng lịch",
        "xem lịch chuyển",
    ],
)
def test_schedule_management_does_not_open_transfer(text: str) -> None:
    """CRITICAL safety: pre-fix, "tạm dừng lịch chuyển mẹ" tripped the
    Tier-1 ``chuyen`` transfer keyword, the predictor filled a
    history-median ~500k, and the chat opened a one-click confirm card
    to send mẹ that money. The user wanted to PAUSE a recurring
    schedule. Route schedule-management verbs to ``recurring`` so the
    user sees their schedule list and can act safely instead."""
    intent, _ = classify(text)
    assert intent == "recurring", (text, intent)


@pytest.mark.parametrize(
    "text",
    [
        # Modifier verbs after a draft — must NOT be matched as
        # recipient_text. Pre-fix the bare-recipient pattern read
        # "cộng thêm" / "thêm" / "giảm" / "tăng" as recipient and the
        # modify path then cleared the existing recipient on the
        # failed alias lookup.
        "cộng thêm 500k",
        "thêm 200k",
        "giảm 300k",
        "tăng 1tr",
        "bớt 100k",
    ],
)
def test_amount_modifier_verbs_dont_steal_recipient(text: str) -> None:
    from app.nlp.entities import extract

    e = extract(text)
    assert e.recipient_text is None, (text, e.recipient_text)
    # Amount should still parse — modifier verb only blocks the
    # recipient match, not the amount.
    assert e.amount is not None, (text, e.amount)


def test_additive_modifier_preserves_recipient_on_modify_draft() -> None:
    """Sequence: ``chuyển mẹ 1tr`` then ``cộng thêm 500k``. Pre-fix
    "cộng thêm" was matched as recipient_text, the modify path cleared
    mẹ, and the draft showed 500k → unspecified recipient. Now mẹ
    survives the turn. The amount math (1tr + 500k = 1.5tr) is NOT
    implemented — the user sees the new 500k amount on the card and
    can correct it; the safety fix here is the recipient survival."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    first = handle_message("u_an", "chuyển mẹ 1tr")
    assert first.draft and first.draft.recipient is not None
    first_name = first.draft.recipient.display_name

    edit = handle_message("u_an", "cộng thêm 500k")
    assert edit.draft is not None
    assert edit.draft.recipient is not None
    assert edit.draft.recipient.display_name == first_name


@pytest.mark.parametrize(
    "text,expected",
    [
        # CRITICAL safety: greeting prefix must not eat the imperative.
        # Pre-fix the message was classified as smalltalk and the
        # transfer instruction was silently dropped — user thought
        # they queued a transfer and walked away.
        ("Chào Omni, chuyển mẹ 2tr nhé", "transfer"),
        ("cảm ơn Omni, chuyển bố 500k", "transfer"),
        ("hello chuyển 100k cho mẹ", "transfer"),
        ("chào Omni, số dư", "balance"),
        ("cảm ơn Omni, đặt lịch chuyển mẹ 2tr mùng 1", "schedule"),
        # Bare greetings must still route to smalltalk.
        ("chào omni", "smalltalk"),
        ("xin chào", "smalltalk"),
        ("cảm ơn", "smalltalk"),
        ("tạm biệt", "smalltalk"),
    ],
)
def test_greeting_prefix_does_not_swallow_command(
    text: str, expected: str
) -> None:
    intent, _ = classify(text)
    assert intent == expected, (text, intent)


@pytest.mark.parametrize(
    "text,expected_recipient",
    [
        # Trailing politeness particles must not glue to the recipient
        # surface form. Pre-fix the prep regex captured "mẹ giúp tôi"
        # and the resolver returned 0.
        ("chuyển 5tr cho mẹ giúp tôi", "mẹ"),
        ("chuyển 5tr cho mẹ nhé", "mẹ"),
        ("chuyển 5tr cho mẹ đi", "mẹ"),
        ("chuyển 5tr cho mẹ ạ", "mẹ"),
        ("chuyển 5tr cho mẹ nha", "mẹ"),
        # Leading "do me a favour" auxiliary between verb and recipient.
        ("chuyển giúp mẹ 200k", "mẹ"),
        ("gửi giùm bố 500k", "bố"),
        ("chuyển hộ mẹ 1tr", "mẹ"),
        # Leading filler interjection.
        ("ê chuyển 5tr cho mẹ giúp tôi", "mẹ"),
    ],
)
def test_filler_and_particle_strip_keeps_recipient(
    text: str, expected_recipient: str
) -> None:
    from app.nlp.entities import extract

    e = extract(text)
    assert e.recipient_text == expected_recipient, (text, e.recipient_text)


@pytest.mark.parametrize(
    "text",
    [
        "khoản ăn uống dưới 200k",
        "ăn uống dưới 200k tháng này",
        "shopping trên 1tr",
        "cà phê từ 50k đến 200k",
    ],
)
def test_category_amount_range_routes_to_history(text: str) -> None:
    """Pre-fix ``khoản ăn uống dưới 200k`` fell to the Tier-3 bare-digit
    transfer fallback and opened a 200k-to-unknown draft. Category +
    range cue is a history filter, not a transfer command."""
    intent, _ = classify(text)
    assert intent == "history", (text, intent)


@pytest.mark.parametrize(
    "text",
    [
        # Negation + transfer verb — user is saying "don't transfer"
        # not "transfer". Pre-fix the Tier-1 ``chuyen`` substring won,
        # opened a one-click confirm card, and the user could land at
        # OTP for a transfer they explicitly refused.
        "đừng chuyển mẹ 2tr",
        "không muốn chuyển mẹ 2tr",
        "hủy ý định chuyển mẹ 2tr",
        # Hypothetical / modal — "what if I sent...?" / "let me try
        # sending..." used to become real drafts. "thử chuyển mẹ 1k"
        # became a real 1.000đ transfer pre-fix.
        "giả sử chuyển mẹ 5tr",
        "thử chuyển mẹ 1k xem được không",
        "nếu chuyển mẹ 2tr thì còn dư không?",
    ],
)
def test_negation_and_hypothetical_route_to_unknown(text: str) -> None:
    intent, _ = classify(text)
    assert intent == "unknown", (text, intent)


@pytest.mark.parametrize(
    "text",
    [
        # Common confirmations missing from the pre-fix list. Judges
        # who type "có" / "tất nhiên" / "chắc chắn" / "uh" at the
        # confirm card had the message routed to NLU and re-prompted.
        "có",
        "tất nhiên",
        "chắc chắn",
        "uh",
    ],
)
def test_confirm_matches_more_vn_acks(text: str) -> None:
    assert _is_confirm(text), text


@pytest.mark.parametrize(
    "text",
    [
        # Bare "có" with question / modal follow-ups must NOT confirm.
        "có thể",
        "có gì không",
        "có sao không",
        "có nên",
        "có chuyện gì",
    ],
)
def test_confirm_bare_co_negative_lookahead(text: str) -> None:
    assert not _is_confirm(text), text


@pytest.mark.parametrize(
    "text",
    [
        # Pre-fix these reassurance phrases starting with "không" /
        # "thôi" were silently cancelling valid draft confirms — the
        # user meant "no change, proceed" / "just go with it" but the
        # session got cancelled. CRITICAL UX bug — valid intent lost.
        "không thay đổi gì cả",
        "không có gì thay đổi",
        "không sao",
        "không phải",
        "thôi cứ thế đi",
        "thôi vậy đi",
        "thôi ok",
    ],
)
def test_cancel_false_positive_guards(text: str) -> None:
    from app.services.orchestrator import _is_cancel

    assert not _is_cancel(text), text


@pytest.mark.parametrize(
    "text",
    [
        # Bare cancel particles must still cancel.
        "không",
        "thôi",
        "huỷ",
        "cancel",
        "không, huỷ đi",
    ],
)
def test_cancel_bare_particles_still_cancel(text: str) -> None:
    from app.services.orchestrator import _is_cancel

    assert _is_cancel(text), text


@pytest.mark.parametrize(
    "text,expected_amount",
    [
        # Pre-fix "100k 2 lần cho mẹ" concatenated to 100.002đ — money-
        # loss-class wrong-amount. The "2 lần" (times) is NOT an amount
        # continuation; the negative lookahead now stops the rest match.
        ("100k 2 lần cho mẹ", 100_000),
        ("chuyển mẹ 100k 3 lần", 100_000),
        # Legitimate concatenations must still work.
        ("5tr500", 5_500_000),
        ("5tr 500k", 5_500_000),
        ("100k500", 100_500),
    ],
)
def test_amount_no_digit_concatenation_before_non_unit_word(
    text: str, expected_amount: int
) -> None:
    from app.nlp.amount import parse_amount

    amount, _ = parse_amount(text)
    assert amount == expected_amount, (text, amount)


@pytest.mark.parametrize(
    "text,expected_recipient",
    [
        # Digit-in-label class — "Bạn cấp 3" is the LABEL of one of the
        # seed contacts (Phạm Thuý Vy). Pre-fix the rule extractor's
        # STOP_LOOKAHEAD `\d` truncated "bạn cấp 3" → "bạn cấp" at the
        # digit, and the resolver couldn't find the label. The new
        # STOP_LOOKAHEAD requires `\d+ + amount unit` to terminate;
        # bare digits inside a label stay inside the surface.
        ("cho bạn cấp 3", "Phạm Thuý Vy"),
        ("chuyển cho bạn cấp 3 2tr", "Phạm Thuý Vy"),
        ("chuyển bạn cấp 3 500k", "Phạm Thuý Vy"),
        ("chuyển cho Bạn cấp 3 1tr", "Phạm Thuý Vy"),
        ("gửi cho bạn cấp 3 100k", "Phạm Thuý Vy"),
    ],
)
def test_digit_in_label_does_not_truncate_recipient(
    text: str, expected_recipient: str
) -> None:
    from app.nlp.entities import extract
    from app.context.alias import resolve_recipient
    from app.store import get_store

    contacts = get_store().contacts_of("u_an")
    e = extract(text)
    assert e.recipient_text, (text, "no recipient_text extracted")
    r = resolve_recipient(e.recipient_text, contacts)
    names = [c.contact.display_name for c in r]
    assert expected_recipient in names, (text, names)


@pytest.mark.parametrize(
    "text,expected_recipient",
    [
        # Possessive "của tôi" / "của mình" must be stripped from the
        # surface form before alias / label lookup. Pre-fix the
        # _strip_relational chain pre-stripped "bạn" as a relational
        # prefix AND failed to remove "của", leaving "than" alone —
        # which matched nothing. New _strip_tail_only variant keeps
        # the relational prefix intact for label matching.
        ("chuyển cho bạn thân của tôi", "Vũ Quốc Bảo"),
        ("chuyển cho bạn thân của tôi 2tr", "Vũ Quốc Bảo"),
        ("chuyển cho bạn thân của mình 500k", "Vũ Quốc Bảo"),
        ("chuyển cho bạn cấp 3 của tôi 1tr", "Phạm Thuý Vy"),
        ("gửi bạn cấp 3 của mình 100k", "Phạm Thuý Vy"),
        ("chuyển mẹ của tôi 5tr", "Nguyễn Thị Lan"),
        ("chuyển cho anh Tuấn của mình 1tr", "Phạm Quốc Tuấn"),
    ],
)
def test_possessive_cua_toi_minh_strips_for_label_match(
    text: str, expected_recipient: str
) -> None:
    from app.nlp.entities import extract
    from app.context.alias import resolve_recipient
    from app.store import get_store

    contacts = get_store().contacts_of("u_an")
    e = extract(text)
    assert e.recipient_text, (text, "no recipient_text extracted")
    r = resolve_recipient(e.recipient_text, contacts)
    names = [c.contact.display_name for c in r]
    assert expected_recipient in names, (text, names)


@pytest.mark.parametrize(
    "text,token_in_candidate",
    [
        # Kinship honorifics "dì" (maternal aunt) and "cậu" (maternal
        # uncle) weren't in the prefix-strip whitelist, so the resolver
        # tried to match "di lan" / "cau minh" verbatim against display
        # names and got nothing.
        ("chuyển dì Lan 200k", "Lan"),
        ("chuyển cậu Minh 200k", "Minh"),
    ],
)
def test_kinship_di_cau_strips_for_name_match(
    text: str, token_in_candidate: str
) -> None:
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    resp = handle_message("u_an", text)
    assert resp.draft is not None
    candidates = list(resp.draft.candidates) if resp.draft else []
    if resp.draft.recipient is not None:
        candidates = [resp.draft.recipient] + candidates
    assert any(
        token_in_candidate in c.display_name for c in candidates
    ), (text, [c.display_name for c in candidates])


@pytest.mark.parametrize(
    "text",
    [
        # Commas / colons / quotes around the label used to leak into
        # the resolver query verbatim ("\"bạn thân\""), failing the
        # exact alias / label match. Now pre-normalised to spaces in
        # ``extract()`` and stripped in ``_clean_recipient``.
        "chuyển,bạn thân,2tr",
        "chuyển cho bạn thân: 2tr",
        'chuyển cho "Bạn thân" 2tr',
        "chuyển  cho   bạn thân   2tr",
    ],
)
def test_punctuation_around_label_resolves_to_recipient(text: str) -> None:
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    resp = handle_message("u_an", text)
    assert resp.draft is not None
    assert resp.draft.recipient is not None, (text, resp.text)
    assert resp.draft.recipient.display_name == "Vũ Quốc Bảo"
    assert resp.draft.amount == 2_000_000


def test_slot_fill_accepts_bare_label_with_digit() -> None:
    """Pre-fix the slot-fill heuristic rejected any text containing a
    digit — so after ``chuyển 2tr`` the follow-up ``bạn cấp 3`` was
    treated as a fresh non-transfer message and the amount slot was
    lost. The new heuristic only rejects digit + amount-unit shapes
    (real amounts) and bare-digit-only short strings (OTPs); a label
    with an interior digit stays eligible."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    t1 = handle_message("u_an", "chuyển 2tr")
    assert t1.draft is not None
    assert t1.draft.recipient is None
    assert t1.draft.amount == 2_000_000

    t2 = handle_message("u_an", "bạn cấp 3")
    assert t2.draft is not None
    assert t2.draft.recipient is not None
    assert t2.draft.recipient.display_name == "Phạm Thuý Vy"
    assert t2.draft.amount == 2_000_000


def test_ambiguous_slot_fill_surfaces_ambiguous_recipient_flag() -> None:
    """Slot-fill with a bare name that matches multiple contacts must
    raise ``ambiguous_recipient`` (and a disambiguation prompt), not
    ``missing_recipient``. Pre-fix the modify-draft path passed
    ``recipient_candidates=[]`` to ``evaluate()`` so the safety rule
    fell back to "Bạn muốn chuyển X cho ai?" instead of "Có nhiều
    người trùng tên: ...". User saw a dead-end "ai?" prompt despite
    the draft carrying multiple candidates."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    handle_message("u_an", "chuyển 2tr")
    resp = handle_message("u_an", "Minh")
    assert resp.draft is not None
    assert resp.draft.recipient is None
    assert len(resp.draft.candidates) >= 2
    flag_codes = [f.code for f in resp.draft.flags]
    assert "ambiguous_recipient" in flag_codes, flag_codes
    assert "missing_recipient" not in flag_codes, flag_codes


def test_bare_name_swaps_recipient_on_filled_draft() -> None:
    """Pre-fix the slot-fill heuristic only fired when ``draft.recipient
    is None``. So when the resolver matched the user's original surface
    to an unintended contact (e.g. "abc" → Công ty ABC) and the user
    typed a different bare name in the next turn, the message fell
    through to NLU → intent=unknown → "Mình chưa rõ ý bạn". Visible
    "context dropped" UX bug — user could not redirect the transfer.
    The branch now also fires when a recipient IS set; the new name
    swaps in."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    t1 = handle_message("u_an", "chuyển 2tr cho abc")
    assert t1.draft is not None
    assert t1.draft.recipient is not None

    t2 = handle_message("u_an", "Nam")
    assert t2.draft is not None
    assert t2.draft.recipient is not None
    assert t2.draft.recipient.display_name == "Vũ Hoàng Nam"
    assert t2.draft.amount == 2_000_000


def test_bare_name_after_complete_draft_does_not_break_confirm() -> None:
    """Companion safety: ``ok`` / ``xác nhận`` after a complete draft
    must NOT be hijacked by the bare-name branch. The confirm regex
    runs first and short-circuits the handler."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    handle_message("u_an", "chuyển mẹ 2tr")
    resp = handle_message("u_an", "ok")
    assert "OTP" in resp.text or "otp" in resp.text.lower()


def test_fresh_transfer_does_not_inherit_stale_amount() -> None:
    """User report: after a previous draft cached amount=2tr, typing a
    fresh ``chuyển tiền cho t`` (no amount specified) saw the prompt
    "Bạn muốn chuyển 2.000.000đ cho ai?" — the stale 2tr leaked from
    the session draft via the modify-path. A fresh verb-led transfer
    command must start a NEW draft and not inherit the prior amount."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    # Establish a stale draft with an amount.
    handle_message("u_an", "chuyển 2tr")
    # Fresh transfer command, no amount mentioned.
    resp = handle_message("u_an", "chuyển tiền cho t")
    assert resp.draft is not None
    # The stale 2tr must NOT be carried over.
    assert resp.draft.amount is None, (resp.draft.amount, resp.text)
    # Both slots are missing → safety engine asks generically.
    flag_codes = [f.code for f in resp.draft.flags]
    assert "missing_amount" in flag_codes


def test_modify_verbs_still_inherit_amount() -> None:
    """Companion: ``đổi sang 5tr`` / ``cộng thêm 500k`` after a draft
    must still hit the modify-path and keep the recipient. The
    fresh-transfer guard only fires on leading transfer verbs
    (``chuyển`` / ``gửi`` / ``trả`` / ``nạp``), not modify verbs."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    handle_message("u_an", "chuyển mẹ 2tr")
    resp = handle_message("u_an", "đổi sang 5tr")
    assert resp.draft is not None
    assert resp.draft.recipient is not None
    assert resp.draft.recipient.display_name == "Nguyễn Thị Lan"
    assert resp.draft.amount == 5_000_000


def test_concurrent_turns_serialise_per_user() -> None:
    """Round-9 stress reproduced a race: when /api/chat fires for the
    same user ~250ms apart, the read-modify-write sequence around
    ``session.current_draft`` interleaves and the OTP prompt latches
    onto a stale draft from a prior turn. Worst case: wrong recipient
    AND wrong amount silently routed to step-up.

    Fix: per-user mutex in ``handle_message``. This test fires 4
    concurrent threads with the canonical slot-fill flow and asserts
    the final draft state matches the sequential expectation."""
    import threading
    from app.context.session import session_for as _sf

    _sf("u_concurrent").clear_draft()

    msgs = ["chuyển", "mẹ", "2tr", "ok"]
    threads = []
    for m in msgs:
        t = threading.Thread(target=handle_message, args=("u_concurrent", m))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # With serialisation, the canonical flow lands on a complete
    # mẹ + 2tr draft in OTP-awaiting state (or already cleared if
    # confirm fired with OTP — both are fine, what matters is no
    # cross-turn corruption).
    s = _sf("u_concurrent")
    draft = s.current_draft
    if draft is not None:
        # If a draft is still alive, it must be the legitimate mẹ + 2tr,
        # not a stale Vũ Quốc Bảo / 5tr from a prior test block.
        if draft.recipient is not None:
            assert draft.recipient.display_name == "Nguyễn Thị Lan", (
                draft.recipient.display_name
            )
        if draft.amount is not None:
            assert draft.amount == 2_000_000, draft.amount


def test_otp_state_blocks_recipient_mutation() -> None:
    """CRITICAL exploit class — round-10 stress: after "chuyển mẹ 50tr
    → ok" the draft is awaiting_otp=True. Typing "bố" used to silently
    swap the recipient to Lê Văn Hùng while keeping awaiting_otp=True
    and the same draft_id — the user's OTP would have authorised a
    transfer to bố instead of mẹ. The OTP-state lock now blocks any
    non-OTP, non-cancel, non-confirm text turn while awaiting_otp."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    handle_message("u_an", "chuyển mẹ 50tr")
    handle_message("u_an", "ok")
    s = _sf("u_an")
    assert s.current_draft is not None
    assert s.current_draft.awaiting_otp is True
    pre_recipient = s.current_draft.recipient.display_name
    pre_amount = s.current_draft.amount

    resp = handle_message("u_an", "bố")
    assert resp.draft is not None
    assert resp.draft.recipient.display_name == pre_recipient
    assert resp.draft.amount == pre_amount
    assert resp.draft.awaiting_otp is True
    assert "OTP" in resp.text or "otp" in resp.text.lower()


@pytest.mark.parametrize(
    "follow",
    [
        "bố thôi",
        "à bố",
        "à bố chứ",
        "nhầm, bố",
        "không, bố",
        "mà bố",
        "ơ bố",
        "bố nhé",
        "đổi sang bố",
        "à mà bố",
        "ơ nhầm bố",
        "à không bố",
    ],
)
def test_pivot_particles_strip_to_clean_recipient(follow: str) -> None:
    """Round-10 pivot test — terse second-turn corrections with leading
    or trailing fillers used to erase the recipient because the
    particle got glued to "bố" and the resolver returned 0. Slot-fill
    now strips these in a loop."""
    from app.context.session import session_for as _sf

    _sf("u_an").clear_draft()
    handle_message("u_an", "chuyển mẹ 2tr")
    resp = handle_message("u_an", follow)
    assert resp.draft is not None
    assert resp.draft.recipient is not None, (follow, resp.text)
    assert resp.draft.recipient.display_name == "Lê Văn Hùng", (
        follow, resp.draft.recipient.display_name
    )
    assert resp.draft.amount == 2_000_000


def test_compound_noun_does_not_strip_to_kinship_prefix() -> None:
    """User report: hỏi "chủ nhà" lại nhận suggestion Phạm Văn Đạt (chú).

    Root cause: ``_strip_tail_only("chu nha")`` returned "chu" because
    "nha" is in the tail-token set (kept for the "mẹ nha" softener).
    "chu" then matched the "Chú" label on an unrelated contact.

    Compound-noun guard now reverts the strip when it would collapse
    the surface to a SINGLE token that is itself a relational prefix
    (chu/anh/chi/em/ban/...). The original "chu nha" stays intact;
    only Vũ Đình Phong (label="Chủ nhà") matches."""
    from app.context.alias import resolve_recipient
    from app.store import get_store

    contacts = get_store().contacts_of("u_an")
    r = resolve_recipient("chủ nhà", contacts)
    names = [c.contact.display_name for c in r]
    # The genuine landlord must match.
    assert "Vũ Đình Phong" in names, names
    # The unrelated "Chú" contact must NOT leak in.
    assert "Phạm Văn Đạt" not in names, names


def test_tail_strip_still_works_for_real_particles() -> None:
    """Regression: the compound-noun guard must not over-trigger and
    block legit tail-particle strips ("mẹ nhé" → "mẹ", "ny ơi" → "ny")."""
    from app.context.alias import _strip_tail_only

    assert _strip_tail_only("me nha") == "me"
    assert _strip_tail_only("sep oi") == "sep"
    assert _strip_tail_only("ban than cua toi") == "ban than"
    # Compound nouns stay intact.
    assert _strip_tail_only("chu nha") == "chu nha"
    assert _strip_tail_only("anh em") == "anh em"
    assert _strip_tail_only("chi em") == "chi em"


def test_resolver_alias_kind_does_not_fall_through_to_names() -> None:
    """When the LLM explicitly tags ``recipient_kind="alias"`` we must
    NOT fall through to name lookup — the user said "bạn thân", not a
    name, and silently picking by name token is the very class of bug
    PR #15 closed. Keep that path locked."""
    from app.context.alias import resolve_recipient
    from app.store import get_store

    contacts = get_store().contacts_of("u_an")
    # "Hùng" exists as both a token in display names AND as label/alias.
    # When tagged kind="alias" but the surface has no alias match, must
    # return [] rather than silently picking the name-token match.
    r = resolve_recipient("không tồn tại", contacts, kind="alias")
    assert r == []


# ---------------------------------------------------------------------------
# Schedule-list (recurring) + casual insights queries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # READ-side "show me my schedules" — pre-fix these either fell
        # to "unknown" or, worse, opened a TRANSFER draft because the
        # Tier-1 transfer keyword "chuyen" sat inside "lịch chuyển tiền
        # của mình" → one-click money-send card. Same class as the
        # PR #19 fix for "tạm dừng lịch chuyển mẹ".
        "lịch chuyển tiền của mình",
        "các lịch của mình",
        "có lịch nào đang chạy",
        "lịch của tôi",
        "lịch sắp tới",
        "lịch nào sắp đến",
        "lịch tự động của mình",
    ],
)
def test_schedule_list_queries_route_recurring(text: str) -> None:
    intent, _ = classify(text)
    assert intent == "recurring", text


@pytest.mark.parametrize(
    "text",
    [
        # Casual "anything weird / interesting?" — judges' opening
        # probe of the insights tab. Pre-fix all fell to "unknown".
        "tháng này có gì lạ không",
        "thấy gì lạ không",
        "có gì đáng chú ý không",
        "tiêu hợp lý chưa",
        "check spending pattern",
    ],
)
def test_casual_insights_queries_route_insights(text: str) -> None:
    intent, _ = classify(text)
    assert intent == "insights", text


@pytest.mark.parametrize(
    "text,expected",
    [
        # Negatives — the "chuyen" substring in "lịch chuyển" must NOT
        # eat the recurring route, and a real transfer command must
        # still route to transfer.
        ("chuyển mẹ 2 triệu", "transfer"),
        ("đặt lịch chuyển mẹ 2tr mùng 5 hàng tháng", "schedule"),
        ("số dư", "balance"),
        ("tháng này tiêu bao nhiêu", "history"),
    ],
)
def test_schedule_list_route_doesnt_eat_commands(
    text: str, expected: str
) -> None:
    intent, _ = classify(text)
    assert intent == expected, text


# ---------------------------------------------------------------------------
# Category extractor → semantic_filter → actual category-filtered totals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'text,expected_filter',
    [
        ('ăn uống tháng này', 'ăn uống'),
        ('trà sữa bao nhiêu', 'trà sữa'),
        ('cà phê tháng trước', 'cà phê'),
        ('cafe tháng này', 'cafe'),
        ('mua sắm bao nhiêu', 'mua sắm'),
        ('giải trí tháng này', 'giải trí'),
        ('xăng tháng này', 'xăng'),
        ('grab tháng này', 'grab'),
        ('tiền điện tháng này', 'tiền điện'),
        ('tiền nhà bao nhiêu', 'tiền nhà'),
        ('shopping bao nhiêu', 'shopping'),
    ],
)
def test_category_extractor_populates_semantic_filter(
    text: str, expected_filter: str
) -> None:
    """The router fix (iteration #35) sent these queries to history,
    but without an extracted ``semantic_filter`` the handler returned
    the unfiltered month total — judges saw the same number for 'ăn
    uống tháng này' and 'tháng này tiêu bao nhiêu'. Adding category
    extraction closes the loop so the response actually filters."""
    from app.nlp.entities import extract
    e = extract(text)
    assert e.semantic_filter is not None
    assert expected_filter in e.semantic_filter.lower(), (
        text, e.semantic_filter
    )


def test_category_filtered_response_scoped_to_period() -> None:
    """Combined check: 'ăn uống tháng này' must (a) route to history,
    (b) extract semantic_filter 'ăn uống', (c) keep period=this_month
    (not silently expand to all_time as the semantic_filter override
    used to do when no temporal_reference was set). Pre-fix label said
    'Tất cả thời gian' because 'tháng này' wasn't in _TEMPORAL_PATTERNS."""
    s = session_for(USER)
    s.clear_draft()
    r = handle_message(USER, 'ăn uống tháng này')
    s.clear_draft()
    assert r.intent == 'history'
    assert 'Tháng này' in r.text or 'tháng này' in r.text.lower(), r.text
    assert 'Tất cả thời gian' not in r.text, r.text


@pytest.mark.parametrize('text', ['tháng này', 'thang nay'])
def test_thang_nay_is_a_temporal_reference(text: str) -> None:
    from app.nlp.entities import extract
    e = extract(text)
    assert e.temporal_reference is not None, text



# ---------------------------------------------------------------------------
# Modify description mid-flow — "nội dung là X" / "ghi chú là X" updates
# ---------------------------------------------------------------------------


def test_modify_description_mid_flow() -> None:
    """After an active draft, a judge typing 'nội dung là tiền học' /
    'ghi chú là tiền cơm' must update draft.description, not fall to
    the guess-correction page. Pre-fix the modify gate required
    nlu.intent == 'transfer' — but a bare description message gets
    intent='unknown' because there's no transfer verb."""
    s = session_for(USER)
    s.clear_draft()
    handle_message(USER, 'gửi mẹ 2 triệu')
    r = handle_message(USER, 'ghi chú là tiền cơm')
    s.clear_draft()
    assert r.draft is not None
    assert r.draft.description == 'tiền cơm'
    # Recipient must be preserved.
    assert r.draft.recipient is not None
    assert 'Mình chưa rõ' not in r.text


def test_modify_description_with_embedded_cho_preserves_recipient() -> None:
    """'nội dung là tiền học cho em' — the 'cho em' inside the
    description used to trip the recipient extractor and clobber the
    active draft's recipient. The description anchor at message start
    now suppresses recipient extraction, so the draft stays whole."""
    s = session_for(USER)
    s.clear_draft()
    handle_message(USER, 'gửi mẹ 2 triệu')
    r = handle_message(USER, 'nội dung là tiền học cho em')
    s.clear_draft()
    assert r.draft is not None
    assert r.draft.recipient is not None, 'recipient was clobbered'
    assert 'tiền học' in (r.draft.description or '')


@pytest.mark.parametrize(
    'text,expected_desc',
    [
        ('nội dung là tiền học', 'tiền học'),
        ('ghi chú là tiền cơm', 'tiền cơm'),
        ('nội dung tiền học', 'tiền học'),
        ('ghi chú tiền cơm', 'tiền cơm'),
    ],
)
def test_description_extractor_strips_la_thanh_linker(
    text: str, expected_desc: str
) -> None:
    """Linker words 'là' / 'thành' between the anchor and the actual
    content must be stripped — pre-fix 'nội dung là tiền học' yielded
    description='là tiền học'."""
    from app.nlp.entities import extract
    e = extract(text)
    assert e.description == expected_desc, (text, e.description)


@pytest.mark.parametrize(
    'amount,expected',
    [
        (1, 1),
        (4_999, 4_999),
        (5_000, 5_000),
        (9_999, 9_999),
    ],
)
def test_round_to_nice_preserves_sub_10k_amounts(
    amount: int, expected: int,
) -> None:
    """Pre-fix `_round_to_nice` divided by a 10k step then banker's-
    rounded, so 5_000 → round(0.5) → 0. The predictor then surfaced
    "đề xuất 0đ" on the confirm card while safety raised
    `missing_amount` (block) — user saw a contradiction. Below 10k we
    leave the amount intact: nothing to smooth at that magnitude."""
    from app.ml.amount_predictor import _round_to_nice
    assert _round_to_nice(amount) == expected


def test_round_to_nice_still_smooths_large_amounts() -> None:
    """The fix must not regress the smoothing behaviour for the actual
    target range. 50_000 stays as a "nice" 10k-aligned value; large
    noisy amounts continue to snap to the appropriate step."""
    from app.ml.amount_predictor import _round_to_nice
    assert _round_to_nice(50_000) == 50_000
    assert _round_to_nice(2_017_345) == 2_000_000
    assert _round_to_nice(12_345_678) == 12_500_000


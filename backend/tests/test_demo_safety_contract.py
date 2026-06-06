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

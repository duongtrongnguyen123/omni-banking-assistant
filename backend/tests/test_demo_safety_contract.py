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

import pytest

from app.context.session import session_for
from app.nlp.budget_entities import detect_goal_intent, extract_goal_name
from app.nlp.intent import classify
from app.nlp.pipeline import understand
from app.services.orchestrator import _is_confirm, handle_message


USER = "u_an"


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

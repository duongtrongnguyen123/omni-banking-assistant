"""Tests for multi-account source picker, biometric step-up, and NLU hints.

Covers the four task scenarios:
  1. Auto-pick when the user names a bank ("từ Vietcombank").
  2. Auto-pick to savings when the amount is large.
  3. NLU detection of "chuyển nội bộ" / source-account hints.
  4. Biometric-only vs biometric+OTP step-up tiers.
"""

from __future__ import annotations

import pytest

from app.context.session import session_for
from app.models.schemas import Contact, SafetyFlag
from app.nlp.entities import extract
from app.safety.rules import auth_policy
from app.services.orchestrator import (
    confirm_draft,
    handle_message,
    pick_source_account,
)
from app.store import get_store


USER = "u_an"


@pytest.fixture(autouse=True)
def _reset_session():
    """Each test starts with a clean draft + history."""
    s = session_for(USER)
    s.current_draft = None
    s.current_contact_draft = None
    s.current_schedule_draft = None
    s.history.clear()
    yield
    s.current_draft = None
    s.current_contact_draft = None
    s.current_schedule_draft = None
    s.history.clear()


# ---------------------------------------------------------------------------
# Auto-pick decision matrix
# ---------------------------------------------------------------------------


def _fake_contact(bank: str) -> Contact:
    return Contact(
        id="c_x",
        owner_id=USER,
        display_name="Test",
        bank=bank,
        account_number="000",
        account_masked="*000",
    )


def test_pick_default_to_primary_when_nothing_matches():
    accounts = get_store().get_user(USER).accounts
    recipient = _fake_contact("Foreign Bank")
    acc, reason = pick_source_account(
        accounts=accounts, recipient=recipient, amount=500_000, hint=None
    )
    assert acc is not None
    assert acc.primary is True
    assert reason == "default_primary"


def test_pick_same_bank_no_fee():
    accounts = get_store().get_user(USER).accounts
    recipient = _fake_contact("Vietcombank")
    acc, reason = pick_source_account(
        accounts=accounts, recipient=recipient, amount=1_000_000, hint=None
    )
    assert acc.bank == "Vietcombank"
    assert reason == "same_bank_no_fee"


def test_pick_large_amount_routes_to_savings():
    accounts = get_store().get_user(USER).accounts
    recipient = _fake_contact("BIDV")  # not in our accounts -> no same-bank shortcut
    acc, reason = pick_source_account(
        accounts=accounts, recipient=recipient, amount=15_000_000, hint=None
    )
    assert acc.kind == "savings"
    assert reason == "large_amount_uses_savings"


def test_pick_user_hint_bank_overrides_savings_rule():
    accounts = get_store().get_user(USER).accounts
    recipient = _fake_contact("BIDV")
    acc, reason = pick_source_account(
        accounts=accounts, recipient=recipient, amount=15_000_000, hint="vpbank"
    )
    assert acc.bank == "VPBank"
    assert reason.startswith("user_hint_bank")


def test_pick_user_hint_kind_savings():
    accounts = get_store().get_user(USER).accounts
    recipient = _fake_contact("BIDV")
    acc, reason = pick_source_account(
        accounts=accounts, recipient=recipient, amount=500_000, hint="savings"
    )
    assert acc.kind == "savings"
    assert reason == "user_hint_kind:savings"


# ---------------------------------------------------------------------------
# NLU extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("chuyển 1tr cho mẹ từ Vietcombank", "vietcombank"),
        ("gửi anh Nam 500k từ VPB", "vpbank"),
        ("trả 2tr cho Lan từ tài khoản tiết kiệm", "savings"),
        ("chuyển từ savings 1tr cho mẹ", "savings"),
        ("dùng lương trả Nam 1 triệu", "salary"),
        ("chuyển từ tài khoản chính 500k cho mẹ", "checking"),
    ],
)
def test_nlu_extracts_source_account_hint(text, expected):
    e = extract(text)
    assert e.source_account_hint == expected, (
        f"{text!r} -> {e.source_account_hint!r} (want {expected!r})"
    )


@pytest.mark.parametrize(
    "text",
    [
        "chuyển nội bộ 5tr",
        "chuyen noi bo cho minh 1tr",
        "internal transfer 2 triệu",
    ],
)
def test_nlu_detects_internal_transfer(text):
    e = extract(text)
    assert e.internal_transfer is True


def test_nlu_external_transfer_is_not_internal():
    e = extract("chuyển cho mẹ 2 triệu")
    assert e.internal_transfer is False


# ---------------------------------------------------------------------------
# Biometric / OTP step-up tiers
# ---------------------------------------------------------------------------


def test_step_up_tier0_no_flags_means_no_auth():
    assert auth_policy([], amount=500_000) == []


def test_step_up_tier1_biometric_only_for_single_warn():
    flags = [
        SafetyFlag(
            code="new_recipient_large_amount",
            severity="warn",
            message="x",
        )
    ]
    assert auth_policy(flags, amount=12_000_000) == ["biometric"]


def test_step_up_tier2_biometric_plus_otp_for_big_amount():
    flags = [
        SafetyFlag(
            code="new_recipient_large_amount",
            severity="warn",
            message="x",
        )
    ]
    assert auth_policy(flags, amount=25_000_000) == ["biometric", "otp"]


def test_step_up_tier2_biometric_plus_otp_for_multi_warn():
    flags = [
        SafetyFlag(code="new_recipient_large_amount", severity="warn", message="x"),
        SafetyFlag(code="amount_above_average", severity="warn", message="y"),
    ]
    assert auth_policy(flags, amount=11_000_000) == ["biometric", "otp"]


def test_blocked_flag_clears_auth_path():
    flags = [
        SafetyFlag(code="insufficient_balance", severity="block", message="x"),
    ]
    assert auth_policy(flags, amount=99_000_000) == []


# ---------------------------------------------------------------------------
# End-to-end through the orchestrator
# ---------------------------------------------------------------------------


def test_e2e_small_same_bank_transfer_executes_with_one_tap():
    resp = handle_message(USER, "chuyển cho mẹ 500k")
    draft = resp.draft
    assert draft is not None
    assert draft.same_bank is True
    assert draft.auth_required == []
    out = confirm_draft(USER, draft.id)
    assert out.draft is None  # executed
    assert "Đã chuyển" in out.text


def test_e2e_user_hint_routes_source():
    resp = handle_message(USER, "chuyển cho mẹ 2 triệu từ Techcombank")
    draft = resp.draft
    assert draft is not None
    assert draft.source_account_id == "acc_an_tcb"
    assert draft.auto_pick_reason.startswith("user_hint_bank")


def test_e2e_large_new_recipient_tier2():
    resp = handle_message(USER, "chuyển 50 triệu cho Hùng STK 9990001234")
    draft = resp.draft
    assert draft is not None
    assert set(draft.auth_required) == {"biometric", "otp"}
    # Biometric only — should still need OTP.
    bio = confirm_draft(USER, draft.id, biometric_verified=True)
    assert bio.draft is not None
    assert "biometric" in bio.draft.auth_completed
    # OTP completes it.
    done = confirm_draft(USER, draft.id, otp="123456")
    assert done.draft is None
    assert "Đã chuyển" in done.text

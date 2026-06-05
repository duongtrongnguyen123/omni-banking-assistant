"""Smoke test for Person 2: safety, auth policy, account validation, audit.

Run from repo root:
  backend\.venv\Scripts\python backend\scripts\safety_auth_audit_smoke.py

This intentionally exercises the backend without the React UI, so Person 2 can
verify the contract quickly after Person 1 pushes frontend changes.
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.context.session import session_for  # noqa: E402
from app.services.orchestrator import confirm_draft, handle_message  # noqa: E402
from app.store import get_store  # noqa: E402

USER = "u_an"
SAVINGS_ACCOUNT = "acc_an_tcb"  # Techcombank savings on the multi-account seed


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def flag_codes(resp) -> list[str]:
    return [f.code for f in (resp.draft.flags if resp.draft else [])]


def reset_session() -> None:
    session_for(USER).clear_draft()


def print_step(title: str) -> None:
    print(f"\n=== {title} ===")


def test_normal_transfer_requires_otp() -> None:
    print_step("1. Normal small transfer -> no step-up -> executed")
    reset_session()

    resp = handle_message(USER, "chuyen cho me 800k nhe")
    draft = resp.draft

    assert_true(draft is not None, "Expected transfer draft")
    # New tiered policy: small, known-recipient → tap-to-confirm, no step-up.
    assert_true(draft.auth_required == [], f"Expected no step-up, got {draft.auth_required}")
    assert_true(flag_codes(resp) == [], f"Expected no safety flags, got {flag_codes(resp)}")
    assert_true(
        draft.description == "Nguyễn Hoàng An chuyển tiền",
        f"Expected default description, got {draft.description}",
    )
    assert_true(draft.amount == 800_000, f"Expected 800k amount, got {draft.amount}")
    print("draft.auth_required:", draft.auth_required)
    print("draft.description:", draft.description)
    print("draft.same_bank:", draft.same_bank, "auto_pick_reason:", draft.auto_pick_reason)

    confirmed = confirm_draft(USER, draft.id)
    assert_true(confirmed.draft is None, "Expected executed transfer to clear draft")
    assert_true("Da chuyen" in _fold_text(confirmed.text), confirmed.text)
    print("confirm:", confirmed.text)


def test_large_transfer_blocks_primary_account() -> object:
    print_step("2. Wrong account number must not resolve to named contact")
    reset_session()

    wrong = handle_message(USER, "chuyen 50 trieu cho hung stk 123456789")
    wrong_draft = wrong.draft

    assert_true(wrong_draft is not None, "Expected blocked transfer draft")
    assert_true(
        wrong_draft.recipient is None,
        "Wrong STK must not show a recipient card",
    )
    assert_true(
        flag_codes(wrong) == ["account_hint_mismatch"],
        f"Expected only account_hint_mismatch, got {flag_codes(wrong)}",
    )
    print("wrong STK flags:", flag_codes(wrong))

    print_step("3. Large transfer auto-routes to savings + tier-2 step-up")
    reset_session()

    resp = handle_message(USER, "Chuyen 50 trieu cho Hung STK 9990001234")
    draft = resp.draft

    assert_true(draft is not None, "Expected transfer draft")
    assert_true(draft.recipient.account_masked == "*1234", draft.recipient.account_masked)
    # 50M with a new recipient triggers tier-2 (biometric + otp) and the
    # picker routes the source to savings (Techcombank, 85M balance).
    assert_true(
        draft.source_account_id == SAVINGS_ACCOUNT,
        f"Expected auto-pick to savings, got {draft.source_account_id} "
        f"(reason={draft.auto_pick_reason})",
    )
    assert_true(
        "insufficient_balance" not in flag_codes(resp),
        f"Savings should fund the transfer, got {flag_codes(resp)}",
    )
    assert_true(
        set(draft.auth_required) == {"biometric", "otp"},
        f"Expected biometric+otp tier-2 step-up, got {draft.auth_required}",
    )
    print("flags:", flag_codes(resp))
    print("draft.auth_required:", draft.auth_required)
    print("auto_pick_reason:", draft.auto_pick_reason)
    return draft


def test_savings_account_needs_otp_and_biometric(draft) -> None:
    print_step("4. Tier-2 step-up: biometric then OTP -> executed")

    # Pass biometric first (UI shows biometric step 1).
    bio_resp = confirm_draft(USER, draft.id, biometric_verified=True)
    assert_true(bio_resp.draft is not None, "Expected draft to remain after biometric")
    assert_true(
        "biometric" in bio_resp.draft.auth_completed,
        f"Expected biometric completed, got {bio_resp.draft.auth_completed}",
    )
    print("after biometric:", bio_resp.text)

    # Then OTP.
    otp_resp = confirm_draft(USER, draft.id, otp="123456")
    assert_true(otp_resp.draft is None, "Expected transfer to execute after OTP")
    assert_true("Da chuyen" in _fold_text(otp_resp.text), otp_resp.text)
    print("after OTP:", otp_resp.text)


def test_audit_path() -> None:
    print_step("5. Audit log records blocked/auth_partial/executed")
    audit = get_store().audit_of(USER)
    decisions = [event.decision for event in audit]

    for expected in ("blocked", "auth_partial", "executed"):
        assert_true(expected in decisions, f"Expected audit decision {expected}, got {decisions}")

    print("recent decisions:", decisions[:8])
    latest = audit[0]
    print("latest event:", {
        "decision": latest.decision,
        "flags": latest.safety_flags,
        "auth_required": latest.auth_required,
        "auth_completed": latest.auth_completed,
    })


def _fold_text(text: str) -> str:
    text = text.replace("Đ", "D").replace("đ", "d")
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def main() -> None:
    test_normal_transfer_requires_otp()
    draft = test_large_transfer_blocks_primary_account()
    test_savings_account_needs_otp_and_biometric(draft)
    test_audit_path()
    print("\nOK: safety/auth/audit smoke test passed.")


if __name__ == "__main__":
    main()

"""Regression tests for the 3-bug fix bundle:

A. ``biometric_verified=True`` boolean used to be client-trusted on the
   confirm endpoint. The field has been removed from both
   ``ConfirmTransactionRequest`` and ``confirm_draft``; only a real 8D
   scan payload advances the biometric auth method.

B. ``execute_transfer`` exception path used to leak the raw English
   ValueError (``"no_source_account"`` / ``"insufficient_balance"``)
   into the VN chat as ``"Giao dịch thất bại: {e}"``. The user now sees
   a friendly VN-only line; the raw exception lands in the server log.

C. After a draft closes, typing a bare ``"cho bố"`` (no verb, no amount)
   used to classify as ``unknown`` because the slot-fill branch in
   ``handle_message`` only fires when an existing draft is missing a
   recipient. The intent classifier now routes the bare
   ``cho|gửi|tới <alias>`` surface to ``transfer``.
"""

from __future__ import annotations

import logging

import pytest

from app.context.session import session_for
from app.models.schemas import OmniResponse
from app.nlp.intent import classify
from app.services.orchestrator import (
    _execute_and_record,
    confirm_draft,
)

USER = "u_an"


def _clear_session() -> None:
    s = session_for(USER)
    for attr in ("clear_draft", "clear_contact_draft", "clear_schedule_draft"):
        fn = getattr(s, attr, None)
        if fn:
            try:
                fn()
            except Exception:  # pragma: no cover
                pass


@pytest.fixture(autouse=True)
def _isolate() -> None:
    _clear_session()
    yield
    _clear_session()


# ---------------------------------------------------------------------------
# Bug A: biometric_verified boolean is GONE
# ---------------------------------------------------------------------------


def test_bug_a_confirm_request_has_no_biometric_verified_field() -> None:
    """The request schema must NOT carry a client-trusted boolean. A future
    refactor that re-introduces ``biometric_verified: bool`` would be a
    direct trust-the-attacker primitive — pin the absence."""
    from app.routes.chat import ConfirmTransactionRequest

    assert "biometric_verified" not in ConfirmTransactionRequest.model_fields, (
        "ConfirmTransactionRequest must not expose a biometric_verified "
        "boolean — biometric auth is gated by a real scan payload only."
    )


def test_bug_a_confirm_draft_signature_has_no_biometric_verified() -> None:
    """The orchestrator-level entry point also stays free of the flag,
    so a unit caller can't smuggle ``biometric_verified=True`` in."""
    import inspect

    sig = inspect.signature(confirm_draft)
    assert "biometric_verified" not in sig.parameters, (
        "confirm_draft must not accept a biometric_verified kwarg — "
        "biometric auth is gated by a real scan payload only."
    )


# ---------------------------------------------------------------------------
# Bug B: VN-only failure message; raw exception goes to the log
# ---------------------------------------------------------------------------


def test_bug_b_execute_transfer_error_shows_friendly_vn_text(
    monkeypatch, caplog
) -> None:
    """When ``execute_transfer`` raises (e.g. ``no_source_account`` /
    ``insufficient_balance``), the user sees one neutral VN line — not
    the raw enum-style English token. The original error is logged
    server-side so ops can still debug."""
    from app.banking import service as banking_service
    from app.models.schemas import Contact, TransactionDraft

    def _raise(*args, **kwargs):
        raise ValueError("insufficient_balance")

    monkeypatch.setattr(banking_service, "execute_transfer", _raise)

    draft = TransactionDraft(
        id="t_test_b",
        recipient=Contact(
            id="c1",
            owner_id=USER,
            display_name="Mẹ",
            bank="MB Bank",
            account_number="1234567890",
            account_masked="****7890",
        ),
        amount=100_000,
    )

    with caplog.at_level(logging.WARNING, logger="app.services.orchestrator"):
        resp: OmniResponse = _execute_and_record(USER, draft, otp_used=True)

    assert resp.intent == "transfer"
    # The friendly VN line — the regression we're pinning.
    assert resp.text == "Có lỗi khi xử lý giao dịch, bạn thử lại sau nhé."
    # And the raw English exception must NOT appear in the user-facing text.
    assert "insufficient_balance" not in resp.text
    assert "Giao dịch thất bại" not in resp.text

    # Server-side log captures the raw cause for ops.
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "insufficient_balance" in logged, (
        "raw exception must be logged server-side for debugging"
    )


# ---------------------------------------------------------------------------
# Bug C: bare "cho|gửi|tới <alias>" classifies as transfer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "cho bố",
        "cho mẹ",
        "gửi mẹ",
        "tới Lan",
        "gui me",
        "  cho bố  ",
        "cho bố?",
    ],
)
def test_bug_c_bare_recipient_classifies_as_transfer(text: str) -> None:
    intent, _ = classify(text)
    assert intent == "transfer", (
        f"{text!r} classified as {intent!r}; expected 'transfer' so the "
        "downstream pipeline can slot-fill recipient and ask for amount."
    )


@pytest.mark.parametrize(
    "text",
    [
        # These must keep their existing routing — the bare-recipient rule
        # is the LAST fallback, so anything matched by an earlier tier
        # (balance, history, schedule, …) still wins.
        "số dư bao nhiêu",
        "lịch sử giao dịch",
        "chào Omni",
    ],
)
def test_bug_c_bare_recipient_rule_does_not_steal_other_intents(text: str) -> None:
    intent, _ = classify(text)
    assert intent != "transfer", (
        f"{text!r} → {intent!r}; the bare-recipient rule must not steal "
        "routing from balance / history / smalltalk intents."
    )

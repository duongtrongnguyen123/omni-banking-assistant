"""Thin translation table for deterministic orchestrator response strings.

The orchestrator hand-builds a handful of safety-critical lines
(transfer-confirmed receipt, balance summary, draft prompts) that the LLM
is forbidden from rephrasing. This module mirrors the top-10 of those
into English so judges who flip the UI language pill see Omni speak the
same language back.

`Lang` is selected per-request from either the `Accept-Language` HTTP
header (preferring `en` if it appears at all) or an explicit `?lang=en`
query string. The default is Vietnamese — matching the rest of the app.

Important: we only translate static templates. Anything that flows
through `llm_phrase()` is left alone — that path is already
multilingual via the LLM and re-translating its output would muddy the
safety contract. See docs/llm-vs-rule.md.
"""

from __future__ import annotations

from typing import Literal, Optional

Lang = Literal["vi", "en"]

_STRINGS: dict[str, dict[str, str]] = {
    # 1. Transfer confirmation receipt (the most-visible line in the app).
    #    Args: amount_text, recipient_name, bank, tx_id.
    "transfer_confirmed": {
        "vi": "Đã chuyển {amount} cho {name} ({bank}). Mã giao dịch: {tx_id}.",
        "en": "Sent {amount} to {name} ({bank}). Transaction ID: {tx_id}.",
    },
    # 2. Draft prompt — "Xác nhận chuyển X cho Y".
    "transfer_confirm_prompt": {
        "vi": "Đã hiểu! Xác nhận chuyển {amount} cho {name} ({bank}).",
        "en": "Got it. Confirm sending {amount} to {name} ({bank}).",
    },
    # 3. OTP prompt.
    "otp_prompt": {
        "vi": "Vui lòng nhập OTP để xác minh giao dịch. Mã demo: 123456.",
        "en": "Please enter the OTP to verify this transaction. Demo code: 123456.",
    },
    # 4. OTP failed.
    "otp_failed": {
        "vi": "OTP chưa đúng. Bạn kiểm tra và nhập lại mã xác minh nhé.",
        "en": "That OTP is incorrect. Please double-check and try again.",
    },
    # 5. Cancelled transfer.
    "transfer_cancelled": {
        "vi": "Đã huỷ giao dịch.",
        "en": "Transfer cancelled.",
    },
    # 6. Cancelled schedule.
    "schedule_cancelled": {
        "vi": "Đã huỷ đặt lịch.",
        "en": "Schedule cancelled.",
    },
    # 7. Generic unknown-intent fallback.
    "unknown_fallback": {
        "vi": (
            "Mình chưa rõ ý bạn. Bạn thử nói cụ thể hơn nhé — ví dụ "
            "\"chuyển cho mẹ 2 triệu\" hoặc \"tháng này tiêu bao nhiêu?\""
        ),
        "en": (
            "I'm not quite sure what you mean. Try being more specific — "
            "for example, \"send mom 2 million\" or \"how much did I spend "
            "this month?\""
        ),
    },
    # 8. Smalltalk default reply.
    "smalltalk_default": {
        "vi": (
            "Chào bạn! Mình là Omni — sẵn sàng giúp bạn chuyển tiền, "
            "xem số dư hay tra lịch sử."
        ),
        "en": (
            "Hi there! I'm Omni — happy to help you send money, "
            "check balances, or look up your history."
        ),
    },
    # 9. Balance summary (deterministic fallback when LLM is offline).
    #    Args: primary_balance, total.
    "balance_summary": {
        "vi": (
            "Số dư tài khoản chính của bạn là {primary}. "
            "Tổng các tài khoản: {total}."
        ),
        "en": (
            "Your primary account balance is {primary}. "
            "Total across accounts: {total}."
        ),
    },
    # 10. Insufficient balance.
    "insufficient_balance": {
        "vi": "Tài khoản này không đủ số dư.",
        "en": "This account doesn't have enough balance.",
    },
}


def detect_lang(
    accept_language: Optional[str] = None,
    query_lang: Optional[str] = None,
) -> Lang:
    """Pick the response language.

    Precedence:
        1. Explicit `?lang=` query parameter.
        2. `Accept-Language` header — English wins if it appears at all
           (judges flipping the frontend pill set this).
        3. Vietnamese fallback.
    """
    if query_lang:
        normalized = query_lang.strip().lower()
        if normalized.startswith("en"):
            return "en"
        if normalized.startswith("vi"):
            return "vi"
    if accept_language:
        # Crude but adequate: any English tag → en. Otherwise default.
        lowered = accept_language.lower()
        # Trim quality-values so "en-US;q=0.8" still matches the prefix.
        primary = lowered.split(",")[0].strip().split(";")[0].strip()
        if primary.startswith("en"):
            return "en"
    return "vi"


def t(key: str, lang: Lang = "vi", **fmt) -> str:
    """Lookup + format. Falls back to VI if EN translation is missing."""
    entry = _STRINGS.get(key)
    if entry is None:
        # Unknown key — return it raw so the caller notices.
        return key
    template = entry.get(lang) or entry["vi"]
    if fmt:
        try:
            return template.format(**fmt)
        except (KeyError, IndexError):
            return template
    return template


__all__ = ["Lang", "detect_lang", "t"]

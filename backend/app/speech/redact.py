"""Generate spoken Vietnamese text from OmniResponse, with privacy rules.

Privacy rules:
- Never speak: account numbers (STK), full balance amounts, OTP codes.
- Always speak: intent, transfer amount (so user can verify), recipient name.
- Fallback: scrub the original text of any long digits / masked numbers / OTP."""

from __future__ import annotations

import re
from typing import Any

# Pattern for masked account display, e.g. "••••1234"
MASKED_ACCT = re.compile(r"[•\*]{3,}\s?\d+")
# Pattern for any 6+ digit sequence (account numbers, OTP codes, etc.)
LONG_DIGITS = re.compile(r"\b\d{6,}\b")
# Pattern for OTP mentions
OTP_MENTION = re.compile(
    r"(?:mã\s+(?:demo|otp)[:\s]*\d+|mã\s+xác\s+minh[:\s]*\d+)",
    re.IGNORECASE,
)
# Pattern for any Vietnamese money amount in text (e.g., "24.350.000đ")
VND_AMOUNT = re.compile(r"\d{1,3}(?:[\.,]\d{3})+\s?(?:đ|VND|vnd)?", re.IGNORECASE)


def amount_in_words(amount: int) -> str:
    """Convert a VND amount to a natural Vietnamese spoken form.

    Examples:
        2_000_000      -> "2 triệu đồng"
        2_500_000      -> "2 triệu 500 nghìn đồng"
        50_000_000     -> "50 triệu đồng"
        500_000        -> "500 nghìn đồng"
        1_000_000_000  -> "1 tỷ đồng"
    """
    if amount == 0:
        return "0 đồng"
    parts: list[str] = []
    billions, rem = divmod(amount, 1_000_000_000)
    millions, rem = divmod(rem, 1_000_000)
    thousands, units = divmod(rem, 1_000)
    if billions:
        parts.append(f"{billions} tỷ")
    if millions:
        parts.append(f"{millions} triệu")
    if thousands:
        parts.append(f"{thousands} nghìn")
    if units:
        parts.append(f"{units}")
    return " ".join(parts) + " đồng"


def _recipient_name(recipient: dict | None) -> str:
    if not recipient:
        return ""
    return recipient.get("label") or recipient.get("display_name") or ""


def _scrub(text: str) -> str:
    """Remove sensitive patterns from arbitrary text."""
    out = text
    out = OTP_MENTION.sub("", out)
    out = MASKED_ACCT.sub("tài khoản", out)
    out = LONG_DIGITS.sub("", out)
    # Tidy: drop empty parens, stray punctuation, collapse repeats.
    out = re.sub(r"\([\s,;]*\)", "", out)
    out = re.sub(r"\s+([\.,;:!?])", r"\1", out)
    out = re.sub(r"([\.,;:!?])\1+", r"\1", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def to_voice_text(resp: dict[str, Any]) -> str:
    """Produce the spoken Vietnamese text for an OmniResponse dict.

    Returns "" if there is nothing safe/useful to speak.
    """
    intent = resp.get("intent")
    text = (resp.get("text") or "").strip()
    draft = resp.get("draft")
    schedule_draft = resp.get("schedule_draft")
    contact_draft = resp.get("contact_draft")

    # --- Transfer flow ---------------------------------------------------
    if intent == "transfer" and draft:
        recipient = draft.get("recipient")
        amount = draft.get("amount")
        flags = draft.get("flags") or []
        block_msgs = [
            f.get("message", "")
            for f in flags
            if f.get("severity") == "block"
        ]

        if block_msgs and not draft.get("auth_required"):
            return _scrub("Giao dịch không thể thực hiện. " + " ".join(block_msgs))

        if recipient and amount:
            name = _recipient_name(recipient)
            amount_words = amount_in_words(int(amount))
            auth_required = draft.get("auth_required") or []
            auth_completed = draft.get("auth_completed") or []

            # Step-up authentication prompt
            if "biometric" in auth_required and "biometric" not in auth_completed:
                if "otp" in auth_required and "otp" not in auth_completed:
                    return (
                        f"Giao dịch chuyển {amount_words} cho {name} cần xác minh "
                        "khuôn mặt và mã OTP. Bạn quét sinh trắc học rồi nhập OTP nhé."
                    )
                return (
                    f"Vui lòng quét sinh trắc học để xác nhận chuyển "
                    f"{amount_words} cho {name}."
                )
            if "otp" in auth_required and "otp" not in auth_completed:
                return (
                    f"Bạn xác nhận chuyển {amount_words} cho {name}? "
                    "Mình sẽ gửi OTP để bạn nhập."
                )
            # No auth required — likely a completion or info-only confirmation
            return f"Đã chuyển {amount_words} cho {name} thành công."

        # Disambiguation
        if draft.get("candidates"):
            n = len(draft["candidates"])
            return f"Mình tìm thấy {n} người trùng tên. Bạn chọn ai trong danh sách nhé."

        # Missing info
        return _scrub(text) or "Bạn cho mình biết thêm thông tin về giao dịch nhé."

    # --- Balance ----------------------------------------------------------
    if intent == "balance" and resp.get("balance"):
        bal = resp["balance"]
        count = len(bal.get("accounts") or [])
        if count > 1:
            return (
                f"Bạn có {count} tài khoản. Chi tiết số dư mình đã hiển thị "
                "trên màn hình rồi nhé."
            )
        return "Mình đã hiển thị số dư trên màn hình rồi nhé."

    # --- History ----------------------------------------------------------
    if intent == "history" and resp.get("history"):
        h = resp["history"]
        count = h.get("count") or 0
        period = h.get("period") or ""
        if count == 0:
            return f"Trong {period}, bạn chưa có giao dịch nào." if period else "Chưa có giao dịch nào."
        return (
            f"Trong {period} có {count} giao dịch. "
            "Chi tiết mình đã hiển thị trên màn hình."
        ) if period else (
            f"Có {count} giao dịch. Chi tiết đã hiển thị trên màn hình."
        )

    # --- Schedule ---------------------------------------------------------
    if intent == "schedule" and schedule_draft:
        name = _recipient_name(schedule_draft.get("recipient"))
        amount_words = amount_in_words(int(schedule_draft.get("amount") or 0))
        cron_label = schedule_draft.get("cron_label") or "định kỳ"
        return (
            f"Bạn muốn đặt lịch chuyển {amount_words} cho {name} {cron_label}, "
            "đúng không?"
        )

    if intent == "schedule" and resp.get("schedule"):
        return "Đã đặt lịch chuyển khoản. Chi tiết hiển thị trên màn hình."

    # --- Add contact ------------------------------------------------------
    if intent == "add_contact" and contact_draft:
        name = contact_draft.get("display_name", "")
        bank = contact_draft.get("bank", "")
        return (
            f"Bạn muốn lưu danh bạ {name}{(' ngân hàng ' + bank) if bank else ''}, "
            "đúng không?"
        )

    # --- Fallback: scrub original text -----------------------------------
    if text:
        scrubbed = _scrub(text)
        # If the original text contains an OTP-style prompt, replace it with
        # a privacy-safe wording.
        if "otp" in text.lower() and "otp" not in scrubbed.lower():
            scrubbed = "Vui lòng nhập mã OTP để xác minh giao dịch."
        return scrubbed

    return ""

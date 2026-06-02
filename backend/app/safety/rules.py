"""Rule engine — business safety checks before a transfer is executed.

Mirrors the "Lớp bảo mật và an toàn" layer in the slide architecture:
  - missing-info checks (amount, recipient)
  - ambiguous recipient detection (multiple Minh's)
  - new-recipient + large-amount flag
  - statistical anomaly: amount > 10× user's average for this recipient
  - insufficient balance
"""

from __future__ import annotations

from statistics import mean
from typing import Optional

from ..models.schemas import (
    Account,
    Contact,
    ResolvedRecipient,
    SafetyFlag,
    Transaction,
)


ANOMALY_MULTIPLIER = 10
NEW_RECIPIENT_LARGE_THRESHOLD = 10_000_000  # 10M VND


def evaluate(
    *,
    amount: Optional[int],
    recipient_candidates: list[ResolvedRecipient],
    recipient: Optional[Contact],
    transactions: list[Transaction],
    account: Optional[Account],
) -> list[SafetyFlag]:
    flags: list[SafetyFlag] = []

    if not recipient_candidates and not recipient:
        flags.append(
            SafetyFlag(
                code="missing_recipient",
                severity="block",
                message="Mình chưa rõ bạn muốn chuyển cho ai. Bạn cho mình biết người nhận nhé?",
            )
        )

    if len(recipient_candidates) > 1 and recipient is None:
        names = ", ".join(c.contact.display_name for c in recipient_candidates)
        flags.append(
            SafetyFlag(
                code="ambiguous_recipient",
                severity="block",
                message=f"Có nhiều người trùng tên: {names}. Bạn chọn đúng người giúp mình nhé.",
            )
        )

    if amount is None or amount <= 0:
        flags.append(
            SafetyFlag(
                code="missing_amount",
                severity="block",
                message="Bạn muốn chuyển bao nhiêu tiền?",
            )
        )

    # If we have a chosen recipient + amount, run anomaly + balance checks.
    if recipient and amount:
        # New recipient + large amount
        if not recipient.frequent and amount >= NEW_RECIPIENT_LARGE_THRESHOLD:
            flags.append(
                SafetyFlag(
                    code="new_recipient_large_amount",
                    severity="warn",
                    message=(
                        "Người nhận chưa từng giao dịch và số tiền lớn — "
                        "mình sẽ yêu cầu xác thực thêm để bảo vệ bạn."
                    ),
                )
            )

        # Statistical anomaly vs user's own transaction average
        all_amounts = [t.amount for t in transactions if t.status == "completed"]
        if all_amounts:
            avg = mean(all_amounts)
            if amount >= avg * ANOMALY_MULTIPLIER:
                flags.append(
                    SafetyFlag(
                        code="amount_above_average",
                        severity="warn",
                        message=(
                            f"Khoan đã — số tiền này cao gấp ~{int(amount / max(avg, 1))}× "
                            f"mức thường ngày của bạn (~{int(avg):,}đ). Bạn cân nhắc lại nhé."
                        ).replace(",", "."),
                    )
                )

        # Balance check
        if account is not None and amount > account.balance:
            flags.append(
                SafetyFlag(
                    code="insufficient_balance",
                    severity="block",
                    message=(
                        f"Số dư tài khoản chính chỉ còn {account.balance:,}đ — "
                        "không đủ cho giao dịch này."
                    ).replace(",", "."),
                )
            )

    return flags


def requires_step_up(flags: list[SafetyFlag]) -> bool:
    """Whether OTP / step-up auth is required to proceed."""
    return any(
        f.code in ("new_recipient_large_amount", "amount_above_average")
        and f.severity == "warn"
        for f in flags
    )


def is_blocked(flags: list[SafetyFlag]) -> bool:
    return any(f.severity == "block" for f in flags)

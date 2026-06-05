"""Rule engine — business safety checks before a transfer is executed.

Mirrors the "Lớp bảo mật và an toàn" layer in the slide architecture:
  - missing-info checks (amount, recipient)
  - ambiguous recipient detection (multiple Minh's)
  - large amount and new-recipient + large-amount flags
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
        recipient_txs = [
            t
            for t in transactions
            if t.status == "completed" and t.contact_id == recipient.id
        ]
        has_transacted_with_recipient = len(recipient_txs) > 0

        # Large amount always needs a step-up path. If this is also a truly
        # new recipient, use the stronger warning. "Frequent" is only display
        # metadata; transaction history is the source of truth here.
        if amount >= NEW_RECIPIENT_LARGE_THRESHOLD and not has_transacted_with_recipient:
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
        elif amount >= NEW_RECIPIENT_LARGE_THRESHOLD:
            flags.append(
                SafetyFlag(
                    code="large_amount",
                    severity="warn",
                    message=(
                        "Số tiền trên 10.000.000đ — mình sẽ yêu cầu xác thực "
                        "thêm trước khi thực hiện."
                    ),
                )
            )

        # Statistical anomaly vs user's own transaction average. Keep this as
        # an extra warning for new recipients; known recipients already get the
        # clearer large-amount warning above, which is easier to explain in the
        # demo.
        all_amounts = [t.amount for t in transactions if t.status == "completed"]
        if all_amounts and not has_transacted_with_recipient:
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
        f.code in ("large_amount", "new_recipient_large_amount", "amount_above_average")
        and f.severity == "warn"
        for f in flags
    )


def auth_policy(flags: list[SafetyFlag]) -> list[str]:
    """Risk-based auth policy for MVP.

    Normal transfer: OTP.
    Warn-level risky transfer: OTP + mock biometric.
    Blocked transfer: no auth path until the user fixes the blocked state.
    """
    if is_blocked(flags):
        return []
    if requires_step_up(flags):
        return ["otp", "biometric"]
    return ["otp"]


def is_blocked(flags: list[SafetyFlag]) -> bool:
    return any(f.severity == "block" for f in flags)

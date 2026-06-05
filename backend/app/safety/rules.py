"""Rule engine — business safety checks before a transfer is executed.

Mirrors the "Lớp bảo mật và an toàn" layer in the slide architecture:
  - missing-info checks (amount, recipient)
  - ambiguous recipient detection (multiple Minh's)
  - new-recipient + large-amount flag
  - statistical anomaly: per-recipient median + MAD (modified z-score);
    falls back to a global × multiplier rule for cold contacts
  - insufficient balance
"""

from __future__ import annotations

from statistics import mean, median
from typing import Optional

from ..models.schemas import (
    Account,
    Contact,
    ResolvedRecipient,
    SafetyFlag,
    Transaction,
)
from ..nlp.amount import format_vnd
from .lookalike import detect_lookalike


# Global fallback: only triggers when we don't have enough per-recipient
# history to compute a meaningful baseline.
ANOMALY_MULTIPLIER = 10
NEW_RECIPIENT_LARGE_THRESHOLD = 10_000_000  # 10M VND

# Per-recipient anomaly: minimum tx count to trust a per-contact baseline,
# and modified z-score cutoff (3.5 ≈ industry-standard outlier threshold,
# Iglewicz & Hoaglin 1993).
_PER_CONTACT_MIN_SAMPLES = 3
_MODIFIED_Z_THRESHOLD = 3.5


def _per_contact_baseline(
    transactions: list[Transaction], contact_id: str
) -> Optional[tuple[int, float]]:
    """Return ``(median_amount, mad)`` for completed transfers to
    ``contact_id``, or ``None`` when there's too little history to be useful.

    Median Absolute Deviation (MAD) is preferred over std-dev here because
    one extreme tx (a 100M wire to mẹ for a property deposit) would inflate
    σ so much that subsequent normal transfers look ordinary again. MAD is
    robust to that one-shot.
    """
    peers = [
        t.amount
        for t in transactions
        if t.contact_id == contact_id and t.status == "completed" and t.amount > 0
    ]
    if len(peers) < _PER_CONTACT_MIN_SAMPLES:
        return None
    med = int(median(peers))
    mad = median(abs(a - med) for a in peers)
    return med, float(mad)


def evaluate(
    *,
    amount: Optional[int],
    recipient_candidates: list[ResolvedRecipient],
    recipient: Optional[Contact],
    transactions: list[Transaction],
    account: Optional[Account],
    contacts: Optional[list[Contact]] = None,
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

    # Look-alike check runs whenever we have a chosen recipient AND a contact
    # list to compare against — independent of amount, since the homograph
    # attack works at any amount (often a probe with a small one first).
    if recipient and contacts:
        twin = detect_lookalike(recipient, contacts)
        if twin is not None:
            flags.append(
                SafetyFlag(
                    code="lookalike_recipient",
                    severity="warn",
                    message=(
                        f"Tên người nhận trông rất giống {twin.display_name} "
                        f"({twin.bank}, {twin.account_masked}) — chắc bạn chọn "
                        "đúng người chứ? Mình sẽ yêu cầu xác thực thêm."
                    ),
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

        # Statistical anomaly: per-recipient baseline first (precise), global
        # fallback for cold contacts (catches first-time fraud where the
        # attacker chose a new contact specifically to evade per-contact stats).
        baseline = _per_contact_baseline(transactions, recipient.id)
        if baseline is not None:
            med, mad = baseline
            # Iglewicz–Hoaglin modified z. The 0.6745 constant makes mad
            # comparable to σ on a normal distribution. Falls back to a flat
            # multiplier when mad == 0 (every prior tx was the same amount).
            if mad > 0:
                mod_z = 0.6745 * (amount - med) / mad
                trips = mod_z >= _MODIFIED_Z_THRESHOLD
            else:
                # Tight history (every prior tx identical): flag any 3× jump.
                trips = amount >= med * 3 if med > 0 else False
            if trips and amount > med:
                ratio = amount / max(med, 1)
                flags.append(
                    SafetyFlag(
                        code="amount_above_average",
                        severity="warn",
                        message=(
                            f"Khoan đã — bạn thường chuyển cho {recipient.display_name} "
                            f"khoảng {format_vnd(med)}, lần này gấp ~{ratio:.1f} lần. "
                            "Bạn kiểm tra lại nhé."
                        ),
                    )
                )
        else:
            # Cold contact: use global mean × multiplier as before.
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
        f.code in (
            "new_recipient_large_amount",
            "amount_above_average",
            "lookalike_recipient",
        )
        and f.severity == "warn"
        for f in flags
    )


def is_blocked(flags: list[SafetyFlag]) -> bool:
    return any(f.severity == "block" for f in flags)

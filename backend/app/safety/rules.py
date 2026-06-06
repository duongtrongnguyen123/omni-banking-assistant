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

# Global fallback: only triggers when we don't have enough per-recipient
# history to compute a meaningful baseline.
ANOMALY_MULTIPLIER = 10
NEW_RECIPIENT_LARGE_THRESHOLD = 10_000_000  # 10M VND

# Per-recipient anomaly: minimum tx count to trust a per-contact baseline,
# and modified z-score cutoff (3.5 ≈ industry-standard outlier threshold,
# Iglewicz & Hoaglin 1993).
_PER_CONTACT_MIN_SAMPLES = 3
_MODIFIED_Z_THRESHOLD = 3.5

# Velocity check — "≥N transfers in W seconds" is the classic fraud-burst
# signature for compromised accounts (attacker drains in 1-2 minutes).
# Soft warn, never auto-block, because legitimate bursts exist (sending
# 4-5 transfers right after payday). 3 in 60s is conservative enough
# that demo runs won't false-positive.
_VELOCITY_N = 3
_VELOCITY_WINDOW_SEC = 60


def _per_contact_baseline(
    transactions: list[Transaction], contact_id: str
) -> Optional[tuple[int, float, int, int]]:
    """Return ``(median_amount, mad, p90, n_samples)`` for completed
    transfers to ``contact_id``, or ``None`` when there's too little
    history to be useful.

    Median Absolute Deviation (MAD) is preferred over std-dev here because
    one extreme tx (a 100M wire to mẹ for a property deposit) would inflate
    σ so much that subsequent normal transfers look ordinary again. MAD is
    robust to that one-shot.
    """
    peers = sorted(
        t.amount
        for t in transactions
        if t.contact_id == contact_id and t.status == "completed" and t.amount > 0
    )
    if len(peers) < _PER_CONTACT_MIN_SAMPLES:
        return None
    med = int(median(peers))
    mad = median(abs(a - med) for a in peers)
    # Cheap p90: nearest-rank, no interpolation needed for a UX hint.
    p90_idx = max(0, int(round(0.9 * (len(peers) - 1))))
    p90 = int(peers[p90_idx])
    return med, float(mad), p90, len(peers)


def evaluate(
    *,
    amount: Optional[int],
    recipient_candidates: list[ResolvedRecipient],
    recipient: Optional[Contact],
    transactions: list[Transaction],
    account: Optional[Account],
    user_id: Optional[str] = None,
    category: Optional[str] = None,
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
        # Any large transfer needs step-up auth. New recipients get a more
        # specific warning below, but known contacts still need biometric for
        # bank-grade high-value transfers.
        if amount >= NEW_RECIPIENT_LARGE_THRESHOLD:
            flags.append(
                SafetyFlag(
                    code=(
                        "new_recipient_large_amount"
                        if not recipient.frequent
                        else "large_amount"
                    ),
                    severity="warn",
                    message=(
                        "Số tiền trên 10.000.000đ — mình sẽ yêu cầu xác thực "
                        "sinh trắc học thêm trước khi thực hiện."
                    )
                    if recipient.frequent
                    else (
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
            med, mad, p90, n_samples = baseline
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
                        details={
                            "kind": "per_recipient",
                            "recipient_name": recipient.display_name,
                            "median": med,
                            "p90": p90,
                            "n_samples": n_samples,
                            "ratio": round(ratio, 2),
                            "current_amount": int(amount),
                        },
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

    # Isolation Forest fraud score (per-user, trained on the user's own
    # history). Soft dependency — returns None when no model is loaded
    # for this user, in which case the legacy z-score check above already
    # caught anything statistically interesting. Wrapped in try/except so
    # a broken model file can never break the safety contract.
    if user_id and recipient is not None and amount is not None:
        try:
            from . import fraud_model  # local import: keeps cold path cheap

            score = fraud_model.score_draft(
                user_id=user_id,
                amount=amount,
                recipient=recipient,
            )
            if score is not None and score >= fraud_model.FRAUD_RISK_THRESHOLD:
                # Suppress when an existing per-recipient warn already fired
                # so the user doesn't see two warnings about the same tx.
                already_warned = any(
                    f.code == "amount_above_average" and f.severity == "warn"
                    for f in flags
                )
                if not already_warned:
                    # Pull the per-user training stats so the frontend can
                    # render a "why" panel mirroring the amount_above_average
                    # detail block (kind=fraud_model).
                    fitted = fraud_model._models.get(user_id)
                    n_train = (
                        getattr(fitted, "n_train", None) if fitted else None
                    )
                    flags.append(
                        SafetyFlag(
                            code="fraud_risk_high",
                            severity="warn",
                            message=(
                                f"Mô hình bất thường (Isolation Forest) đánh giá "
                                f"giao dịch này có rủi ro cao ({int(score * 100)}%). "
                                "Bạn xác minh OTP để chắc chắn nhé."
                            ),
                            details={
                                "kind": "fraud_model",
                                "score": round(score, 3),
                                "threshold": fraud_model.FRAUD_RISK_THRESHOLD,
                                "n_train": n_train,
                                "current_amount": int(amount),
                            },
                        )
                    )
        except Exception:  # pragma: no cover — defensive
            pass

    # Budget overshoot — soft warn when the draft would push the user
    # past their monthly envelope for ``category``. Never gates the
    # transfer; the user already set the limit so we just remind them.
    if user_id and category and amount is not None and amount > 0:
        try:
            from ..banking.budgets import compute_status_for  # local import: cold path

            status = compute_status_for(user_id, category)
            if status is not None and status.monthly_limit_vnd > 0:
                projected = status.spent_vnd + amount
                if projected > status.monthly_limit_vnd:
                    overshoot = projected - status.monthly_limit_vnd
                    flags.append(
                        SafetyFlag(
                            code="budget_overshoot",
                            severity="warn",
                            message=(
                                f"Lưu ý — giao dịch này sẽ vượt ngân sách "
                                f"{status.category_label} tháng này "
                                f"{format_vnd(overshoot)}. Bạn vẫn tiếp tục nhé?"
                            ),
                            details={
                                "kind": "budget_overshoot",
                                "category": status.category,
                                "category_label": status.category_label,
                                "monthly_limit_vnd": status.monthly_limit_vnd,
                                "spent_vnd": status.spent_vnd,
                                "projected_vnd": projected,
                                "overshoot_vnd": overshoot,
                            },
                        )
                    )
        except Exception:  # pragma: no cover — defensive
            pass

    # Velocity check — count completed transfers in the last
    # _VELOCITY_WINDOW_SEC seconds. Catches the classic
    # account-compromise pattern (attacker drains in 1-2 minutes) and
    # the "demo double-confirm" footgun. Soft warn (OTP step-up),
    # never auto-block.
    if user_id and amount is not None:
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        cutoff = _dt.now(_tz.utc) - _td(seconds=_VELOCITY_WINDOW_SEC)
        recent_count = sum(
            1
            for t in transactions
            if t.status == "completed" and t.created_at >= cutoff
        )
        if recent_count >= _VELOCITY_N:
            flags.append(
                SafetyFlag(
                    code="transfer_velocity_high",
                    severity="warn",
                    message=(
                        f"Bạn vừa chuyển {recent_count} lần trong "
                        f"{_VELOCITY_WINDOW_SEC} giây. Mình tạm dừng "
                        "để xác minh OTP nhé."
                    ),
                    details={
                        "kind": "velocity",
                        "recent_count": recent_count,
                        "window_sec": _VELOCITY_WINDOW_SEC,
                        "threshold": _VELOCITY_N,
                    },
                )
            )

    # Persistent audit trail — every non-empty flag set lands in
    # ``backend/logs/audit-YYYY-MM-DD.log`` (JSONL) so an auditor / SBV
    # compliance team can reconstruct decisions post-hoc. Fail-open: a
    # disk error never blocks the live transfer path.
    if user_id and flags:
        try:
            from ..services.audit_log import record_safety_decision

            record_safety_decision(user_id=user_id, draft_id=None, flags=flags)
        except Exception:  # pragma: no cover
            pass

    # Push toast for anomaly warnings so the user sees the heads-up
    # even if the chat scroll has moved past the safety message. Only
    # fires when we know which user this is (the orchestrator passes
    # ``user_id``; callers that don't need toasts can omit it).
    if user_id:
        from ..services import events as _events  # local import: avoid cycle

        for f in flags:
            if f.code == "amount_above_average" and f.severity == "warn":
                _events.publish_anomaly_warning(user_id, message=f.message)

    # Metrics: one counter increment per fired flag, labelled by code +
    # severity. Wrapped in try/except so a broken metrics module can't
    # break the safety contract.
    try:
        from ..services import metrics as _m

        for f in flags:
            _m.safety_flag_total.inc(code=f.code, severity=f.severity)
    except Exception:
        pass

    return flags


def requires_step_up(flags: list[SafetyFlag]) -> bool:
    """Whether OTP / step-up auth is required to proceed."""
    return any(
        f.code in (
            "new_recipient_large_amount",
            "large_amount",
            "amount_above_average",
            "fraud_risk_high",
            "transfer_velocity_high",
        )
        and f.severity == "warn"
        for f in flags
    )


def is_blocked(flags: list[SafetyFlag]) -> bool:
    return any(f.severity == "block" for f in flags)


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

"""Per-decision "why" explainer.

Reconstructs the reasoning trail for a single AuditEvent — what the NLU
layer picked and why, how context resolved the recipient, which safety
flags fired, and what the banking layer ultimately did.

Strictly read-only: never mutates state, never calls the LLM. The output
is meant to ground judges / compliance reviewers in the deterministic
decisions Omni made for a given turn.
"""

from __future__ import annotations

from typing import Optional

from ..models.schemas import AuditEvent
from ..store import get_store


# Codes whose presence on a draft means "we never made it past safety".
_BLOCKING_CODES = {
    "missing_amount",
    "missing_recipient",
    "ambiguous_recipient",
    "account_hint_mismatch",
    "insufficient_balance",
}

_FLAG_LABELS: dict[str, tuple[str, str]] = {
    "missing_amount": (
        "Thiếu số tiền — bot không tự suy đoán.",
        "Amount missing — bot will not guess.",
    ),
    "missing_recipient": (
        "Không xác định được người nhận từ câu nói.",
        "Recipient could not be identified from the message.",
    ),
    "ambiguous_recipient": (
        "Có nhiều người trùng tên — cần người dùng chọn cụ thể.",
        "Multiple contacts match — disambiguation required.",
    ),
    "account_hint_mismatch": (
        "STK người dùng đọc không khớp với danh bạ.",
        "Account number does not match the contact on file.",
    ),
    "large_amount": (
        "Số tiền ≥ 10 triệu — kích hoạt step-up auth.",
        "Amount ≥ 10M VND — step-up auth triggered.",
    ),
    "new_recipient_large_amount": (
        "Người nhận chưa từng giao dịch + số tiền lớn — bắt OTP + sinh trắc.",
        "First-time recipient + large amount — OTP + biometric required.",
    ),
    "amount_above_average": (
        "Số tiền lớn gấp nhiều lần mức trung bình của bạn — cảnh báo bất thường.",
        "Amount is many times your usual average — anomaly warning.",
    ),
    "insufficient_balance": (
        "Số dư tài khoản nguồn không đủ — chặn giao dịch.",
        "Source account balance is insufficient — transfer blocked.",
    ),
    "ok": (
        "Không có cờ rủi ro nào.",
        "No risk flags raised.",
    ),
}


def _format_vnd(amount: Optional[int]) -> str:
    if amount is None:
        return "?"
    return f"{amount:,}đ".replace(",", ".")


def _nlu_step(event: AuditEvent) -> dict:
    src = event.nlu_source
    if src == "llm":
        rationale = (
            f"LLM trả về intent={event.intent} với entities "
            f"{list(event.entities.keys()) if event.entities else 'rỗng'}."
        )
        rationale_en = (
            f"LLM returned intent={event.intent} with entities "
            f"{list(event.entities.keys()) if event.entities else 'empty'}."
        )
    elif src == "rule":
        rationale = (
            f"LLM không khả dụng / không tự tin — rule classifier khớp "
            f"intent={event.intent} qua keyword/regex."
        )
        rationale_en = (
            f"LLM unavailable or low-confidence — rule classifier matched "
            f"intent={event.intent} via keyword/regex."
        )
    else:
        rationale = "Không ghi nhận được nguồn NLU — fallback unknown."
        rationale_en = "NLU source not recorded — falling back to unknown."

    return {
        "layer": "nlu",
        "decision": f"intent={event.intent}",
        "rationale": rationale,
        "rationale_en": rationale_en,
        "source": src,
    }


def _context_steps(event: AuditEvent) -> list[dict]:
    steps: list[dict] = []
    entities = event.entities or {}

    recipient_text = entities.get("recipient_text")
    if event.resolved_recipient:
        if recipient_text:
            rationale = (
                f"Alias resolver khớp \"{recipient_text}\" → "
                f"{event.resolved_recipient} (qua bảng alias / token name)."
            )
            rationale_en = (
                f"Alias resolver matched \"{recipient_text}\" → "
                f"{event.resolved_recipient} (via alias table / name token)."
            )
        else:
            rationale = (
                f"Người nhận {event.resolved_recipient} được suy ra từ ngữ cảnh "
                "(không có surface form trực tiếp trong câu)."
            )
            rationale_en = (
                f"Recipient {event.resolved_recipient} inferred from context "
                "(no explicit surface form in the message)."
            )
        steps.append(
            {
                "layer": "context",
                "decision": f"recipient={event.resolved_recipient}",
                "rationale": rationale,
                "rationale_en": rationale_en,
            }
        )
    elif recipient_text:
        steps.append(
            {
                "layer": "context",
                "decision": f"recipient=unresolved",
                "rationale": (
                    f"Không khớp được \"{recipient_text}\" với danh bạ — "
                    "yêu cầu disambiguation hoặc thêm thông tin."
                ),
                "rationale_en": (
                    f"Could not match \"{recipient_text}\" against contacts — "
                    "disambiguation or more info required."
                ),
            }
        )

    amount = entities.get("amount")
    amount_text = entities.get("amount_text")
    if amount is not None:
        if amount_text:
            rationale = (
                f"Trích xuất số tiền {_format_vnd(amount)} từ \"{amount_text}\" "
                "qua nlp/amount.py (rule, không LLM)."
            )
            rationale_en = (
                f"Parsed amount {_format_vnd(amount)} from \"{amount_text}\" "
                "via nlp/amount.py (rule, not LLM)."
            )
        else:
            rationale = (
                f"Số tiền {_format_vnd(amount)} được LLM trích từ câu."
            )
            rationale_en = (
                f"Amount {_format_vnd(amount)} extracted by the LLM."
            )
        steps.append(
            {
                "layer": "context",
                "decision": f"amount={amount}",
                "rationale": rationale,
                "rationale_en": rationale_en,
            }
        )

    temporal = entities.get("temporal_reference")
    if temporal:
        steps.append(
            {
                "layer": "context",
                "decision": f"temporal_ref={temporal}",
                "rationale": (
                    f"Trích xuất tham chiếu thời gian \"{temporal}\" — "
                    "được dùng để tra giao dịch trước đó hoặc xác định khoảng."
                ),
                "rationale_en": (
                    f"Extracted temporal reference \"{temporal}\" — used to "
                    "look up a prior transaction or scope a history window."
                ),
            }
        )

    return steps


def _account_step(event: AuditEvent) -> Optional[dict]:
    if not event.selected_account:
        return None
    store = get_store()
    user = store.get_user_or_none(event.user_id)
    label = event.selected_account
    if user:
        for acc in user.accounts:
            if acc.id == event.selected_account:
                mask = "••••" + acc.number[-4:]
                role = "tài khoản chính" if acc.primary else "tài khoản phụ"
                label = f"{acc.bank} {mask} ({role})"
                break
    return {
        "layer": "banking",
        "decision": f"source_account={event.selected_account}",
        "rationale": f"Chọn nguồn: {label}.",
        "rationale_en": f"Source account picked: {label}.",
    }


def _safety_step(event: AuditEvent) -> dict:
    flags = event.safety_flags or []
    if not flags:
        return {
            "layer": "safety",
            "decision": "no flags",
            "rationale": "Không có cờ rủi ro — đi đường thường (chỉ OTP).",
            "rationale_en": "No risk flags — standard path (OTP only).",
        }

    blocks = [f for f in flags if f in _BLOCKING_CODES]
    warnings = [f for f in flags if f not in _BLOCKING_CODES]
    pieces_vi: list[str] = []
    pieces_en: list[str] = []
    for code in flags:
        vi, en = _FLAG_LABELS.get(code, (f"Cờ \"{code}\".", f"Flag \"{code}\"."))
        pieces_vi.append(vi)
        pieces_en.append(en)

    if blocks:
        decision = "blocked"
    elif warnings:
        decision = "warn (step-up auth)"
    else:
        decision = "info"

    return {
        "layer": "safety",
        "decision": decision,
        "rationale": " ".join(pieces_vi),
        "rationale_en": " ".join(pieces_en),
    }


def _auth_step(event: AuditEvent) -> Optional[dict]:
    required = event.auth_required or []
    completed = event.auth_completed or []
    if not required and event.decision in {"executed", "blocked"}:
        return None
    if not required:
        return None
    missing = [m for m in required if m not in completed]
    if missing:
        rationale = (
            f"Yêu cầu xác thực: {', '.join(required)}. "
            f"Còn thiếu: {', '.join(missing)}."
        )
        rationale_en = (
            f"Auth required: {', '.join(required)}. "
            f"Still missing: {', '.join(missing)}."
        )
    else:
        rationale = (
            f"Đã hoàn tất xác thực: {', '.join(completed)}."
        )
        rationale_en = (
            f"Auth completed: {', '.join(completed)}."
        )
    return {
        "layer": "safety",
        "decision": f"auth={'+'.join(required) if required else 'none'}",
        "rationale": rationale,
        "rationale_en": rationale_en,
    }


def _banking_step(event: AuditEvent) -> dict:
    decision = event.decision
    if decision == "executed":
        return {
            "layer": "banking",
            "decision": "execute",
            "rationale": (
                "Mọi điều kiện xác thực đạt — execute_transfer ghi giao dịch "
                "và trừ số dư."
            ),
            "rationale_en": (
                "All auth conditions satisfied — execute_transfer recorded "
                "the transfer and debited the balance."
            ),
        }
    if decision == "draft_created":
        return {
            "layer": "banking",
            "decision": "draft",
            "rationale": (
                "Dừng ở bước draft — chờ người dùng xác nhận trước khi thực thi."
            ),
            "rationale_en": (
                "Stopped at draft stage — waiting for user confirmation "
                "before execution."
            ),
        }
    if decision == "blocked":
        return {
            "layer": "banking",
            "decision": "reject",
            "rationale": (
                "Bị chặn bởi rule engine — không gọi execute_transfer."
            ),
            "rationale_en": (
                "Blocked by the rule engine — execute_transfer never called."
            ),
        }
    if decision == "auth_required":
        return {
            "layer": "banking",
            "decision": "await_auth",
            "rationale": "Yêu cầu OTP/sinh trắc trước khi tiếp tục.",
            "rationale_en": "Awaiting OTP / biometric before continuing.",
        }
    if decision == "auth_partial":
        return {
            "layer": "banking",
            "decision": "auth_partial",
            "rationale": "Một bước xác thực đã pass — đang chờ bước còn lại.",
            "rationale_en": "One auth step passed — waiting on the next.",
        }
    if decision == "auth_failed":
        return {
            "layer": "banking",
            "decision": "auth_failed",
            "rationale": "OTP không khớp — yêu cầu nhập lại.",
            "rationale_en": "OTP mismatch — user must retry.",
        }
    if decision == "execute_failed":
        return {
            "layer": "banking",
            "decision": "execute_failed",
            "rationale": (
                "execute_transfer ném lỗi (ví dụ insufficient_balance khi "
                "kiểm tra lần cuối)."
            ),
            "rationale_en": (
                "execute_transfer raised an error (e.g. insufficient_balance "
                "at final check)."
            ),
        }
    if decision == "cancel":
        return {
            "layer": "banking",
            "decision": "cancel",
            "rationale": "Người dùng huỷ — draft bị xoá khỏi session.",
            "rationale_en": "User cancelled — draft removed from the session.",
        }
    return {
        "layer": "banking",
        "decision": decision or "unknown",
        "rationale": (
            "Quyết định không nằm trong bảng đã biết — hiển thị nguyên trạng."
        ),
        "rationale_en": (
            "Decision is not in the known table — surfaced as-is."
        ),
    }


def _summarise(event: AuditEvent) -> str:
    amount = (event.entities or {}).get("amount")
    who = event.resolved_recipient or "?"
    amount_str = _format_vnd(amount) if amount else "?đ"
    decision = event.decision
    if event.intent == "transfer":
        if decision == "executed":
            return f"Chuyển {amount_str} cho {who} — đã thực hiện."
        if decision == "draft_created":
            return f"Tạo draft chuyển {amount_str} cho {who} — chờ xác nhận."
        if decision == "blocked":
            return f"Chặn giao dịch {amount_str} cho {who}."
        if decision in {"auth_required", "auth_partial", "auth_failed"}:
            return f"Đang xác thực giao dịch {amount_str} cho {who} ({decision})."
        if decision == "execute_failed":
            return f"Thực thi thất bại {amount_str} cho {who}."
    if event.intent == "balance":
        return "Tra số dư."
    if event.intent == "history":
        return "Tra lịch sử giao dịch."
    if event.intent == "schedule":
        return "Đặt lịch chuyển tiền."
    if event.intent == "add_contact":
        return "Thêm danh bạ mới."
    return f"{event.intent} → {decision}."


def build_explanation(event: AuditEvent) -> dict:
    """Build the explain payload for one audit event.

    Returns a structure with `audit_id`, `summary`, ordered `steps`, and
    `raw_audit_event`. Every step has `layer`, `decision`, `rationale`,
    `rationale_en`; the NLU step additionally carries `source`.
    """
    steps: list[dict] = []
    steps.append(_nlu_step(event))
    steps.extend(_context_steps(event))

    acct_step = _account_step(event)
    safety = _safety_step(event)
    auth = _auth_step(event)

    # Display order: source account (banking pre-step) → safety → auth → banking.
    if acct_step is not None:
        steps.append(acct_step)
    steps.append(safety)
    if auth is not None:
        steps.append(auth)
    steps.append(_banking_step(event))

    return {
        "audit_id": event.id,
        "summary": _summarise(event),
        "steps": steps,
        "raw_audit_event": event.model_dump(mode="json"),
    }


def find_audit_event(user_id: str, audit_id: str) -> Optional[AuditEvent]:
    """Find an audit event by id, scoped to one user.

    Owner-scoping is important: explain endpoints would otherwise leak
    audit data across users.
    """
    for ev in get_store().audit_events:
        if ev.id == audit_id and ev.user_id == user_id:
            return ev
    return None


__all__ = ["build_explanation", "find_audit_event"]

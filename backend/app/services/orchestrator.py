"""Conversation orchestrator: NLU → Context → Safety → Banking.

The brain of Omni — turns a user utterance into an OmniResponse with the
appropriate side-effects (draft creation, history lookup, schedule creation).
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..banking.service import create_schedule, get_balance, get_history, next_run_for
from ..context import resolve_recipient, resolve_temporal_reference, session_for
from ..context.alias import filter_by_account_hint, resolve_by_account_hint
from ..models.schemas import (
    AuditEvent,
    Contact,
    ContactDraft,
    NLUResult,
    OmniResponse,
    SafetyFlag,
    ScheduleDraft,
    TransactionDraft,
)
from ..nlp.amount import format_vnd
from ..nlp.entities import normalize_alias
from ..nlp.llm import llm_phrase
from ..nlp.pipeline import understand
from ..safety.rules import auth_policy, evaluate, is_blocked, requires_step_up
from ..store import get_store, new_id, now

_CONFIRM_RE = re.compile(r"^(xac nhan|xacnhan|ok|đồng ý|dong y|y|yes|confirm|duyệt|duyet|lưu|luu)\b|^xác nhận", re.IGNORECASE)
_CANCEL_RE = re.compile(r"^(huỷ|huy|cancel|hủy|không|khong|no|stop|bỏ|bo)\b", re.IGNORECASE)


def _is_confirm(text: str) -> bool:
    return bool(_CONFIRM_RE.search(text.strip().lower()))


def _is_cancel(text: str) -> bool:
    return bool(_CANCEL_RE.search(text.strip().lower()))


_BIO_TARGETS = ["center", "sideA", "verticalA", "sideB", "center"]
_FACE_MATCH_THRESHOLD = 0.48


def _pose_value(step: dict[str, Any], key: str) -> float:
    pose = step.get("pose") or {}
    try:
        return float(pose.get(key, 0))
    except (TypeError, ValueError):
        return 0.0


def _face_distance(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 999.0
    return sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)) ** 0.5


def _face_match_valid(scan: dict[str, Any]) -> tuple[bool, str]:
    face_descriptor = scan.get("faceDescriptor") or []
    profile_descriptors = scan.get("profileDescriptors") or []
    if len(face_descriptor) != 128:
        return False, "Chưa nhận được đặc trưng khuôn mặt hiện tại."
    valid_profiles = [
        descriptor
        for descriptor in profile_descriptors
        if isinstance(descriptor, list) and len(descriptor) == 128
    ]
    if not valid_profiles:
        return False, "Chưa có hồ sơ sinh trắc học đã lưu cho tài khoản này."

    best_distance = min(_face_distance(face_descriptor, descriptor) for descriptor in valid_profiles)
    if best_distance > _FACE_MATCH_THRESHOLD:
        return False, "Khuôn mặt không khớp với hồ sơ sinh trắc học đã lưu của tài khoản."
    return True, ""


def _biometric_scan_valid(scan: dict[str, Any] | None, draft_id: str, otp: str | None) -> tuple[bool, str]:
    if not scan:
        return False, "Chưa có dữ liệu quét sinh trắc học 8D."

    challenge_id = str(scan.get("challengeId") or "")
    expected_challenge = f"{draft_id}:{(otp or '').strip() or 'no-otp'}"
    if challenge_id != expected_challenge:
        return False, "Phiên sinh trắc học không khớp giao dịch hiện tại."

    steps = scan.get("steps") or []
    if len(steps) != len(_BIO_TARGETS):
        return False, "Sinh trắc học chưa hoàn tất đủ vòng quay khuôn mặt."

    path = scan.get("path")
    if path not in {"clockwise", "counterClockwise"}:
        return False, "Thử thách sinh trắc học không hợp lệ."

    required_stable = int(scan.get("requiredStableFrames") or 0)
    if required_stable < 1:
        return False, "Dữ liệu sinh trắc học chưa đủ số frame ổn định."

    previous_elapsed = -1
    signatures: set[int] = set()
    for index, (step, target) in enumerate(zip(steps, _BIO_TARGETS)):
        if int(step.get("index", -1)) != index or step.get("target") != target:
            return False, "Thứ tự thử thách sinh trắc học không hợp lệ."
        if int(step.get("stableFrames") or 0) < required_stable:
            return False, "Một bước sinh trắc học chưa đủ ổn định."
        if float(step.get("detectionScore") or 0) < 0.5:
            return False, "Khuôn mặt chưa đủ rõ để xác minh."

        elapsed = int(step.get("elapsedMs") or 0)
        if elapsed <= previous_elapsed:
            return False, "Mốc thời gian sinh trắc học không hợp lệ."
        previous_elapsed = elapsed

        signatures.add(int(step.get("frameSignature") or 0))

    first_center_yaw = _pose_value(steps[0], "yaw")
    last_center_yaw = _pose_value(steps[-1], "yaw")
    first_center_pitch = _pose_value(steps[0], "pitch")
    last_center_pitch = _pose_value(steps[-1], "pitch")
    if abs(first_center_yaw) > 0.22 or abs(last_center_yaw) > 0.22:
        return False, "Khuôn mặt chưa nhìn gần thẳng trong khung."
    if abs(first_center_pitch) > 0.22 or abs(last_center_pitch) > 0.22:
        return False, "Khuôn mặt chưa nhìn gần thẳng trong khung."

    side_a_yaw = _pose_value(steps[1], "yaw")
    vertical_a_pitch = _pose_value(steps[2], "pitch")
    side_b_yaw = _pose_value(steps[3], "yaw")
    if abs(side_a_yaw) <= 0.065:
        return False, "Chưa xác nhận được hướng quay đầu tiên."
    if abs(side_b_yaw) <= 0.065 or side_a_yaw * side_b_yaw >= 0:
        return False, "Chưa xác nhận được hướng quay ngược lại."
    if path == "clockwise":
        if vertical_a_pitch >= -0.055:
            return False, "Video/ảnh không khớp chiều quay được yêu cầu."
    else:
        if vertical_a_pitch <= 0.055:
            return False, "Video/ảnh không khớp chiều quay được yêu cầu."

    yaw_span = abs(side_a_yaw - side_b_yaw)
    if yaw_span < 0.14:
        return False, "Biên độ chuyển động khuôn mặt chưa đủ để xác minh."

    if len(signatures) < 5:
        return False, "Các frame sinh trắc học quá giống nhau, vui lòng quét lại."

    if previous_elapsed < 550:
        return False, "Quá trình quét diễn ra quá nhanh, vui lòng thực hiện lại."

    samples = scan.get("samples") or []
    if len(samples) < 12:
        return False, "Cần quét chuyển động liên tục lâu hơn một chút."

    if int(scan.get("continuityBreaks") or 0) > 1:
        return False, "Chuyển động khuôn mặt bị ngắt quãng, vui lòng quay liên tục thay vì đổi ảnh."

    sample_signatures: set[int] = set()
    previous_sample_elapsed = -1
    max_pose_jump = 0.0
    total_motion = 0.0
    low_score_samples = 0
    previous_sample: dict[str, Any] | None = None
    for sample in samples:
        elapsed = int(sample.get("elapsedMs") or 0)
        if elapsed <= previous_sample_elapsed:
            return False, "Chuỗi frame sinh trắc học không hợp lệ."
        previous_sample_elapsed = elapsed

        if float(sample.get("detectionScore") or 0) < 0.5:
            low_score_samples += 1
        sample_signatures.add(int(sample.get("frameSignature") or 0))

        if previous_sample is not None:
            pose_jump = (
                abs(_pose_value(sample, "yaw") - _pose_value(previous_sample, "yaw"))
                + abs(_pose_value(sample, "pitch") - _pose_value(previous_sample, "pitch"))
                + abs(_pose_value(sample, "roll") - _pose_value(previous_sample, "roll"))
            )
            max_pose_jump = max(max_pose_jump, pose_jump)
            total_motion += pose_jump
        previous_sample = sample

    if low_score_samples > 2:
        return False, "Một số frame khuôn mặt chưa đủ rõ, vui lòng quét lại."
    if len(sample_signatures) < 7:
        return False, "Chuỗi hình ảnh quá giống ảnh tĩnh, vui lòng quay mặt thật liên tục."
    if max_pose_jump > 1:
        return False, "Pose khuôn mặt nhảy quá nhanh, nghi ngờ đổi ảnh theo từng hướng."
    if total_motion < 0.55:
        return False, "Chuyển động khuôn mặt chưa đủ liên tục để xác minh."

    face_ok, face_error = _face_match_valid(scan)
    if not face_ok:
        return False, face_error

    return True, ""


def _record_audit(
    user_id: str,
    *,
    message: str = "",
    nlu: NLUResult | None = None,
    draft: TransactionDraft | None = None,
    decision: str,
    nlu_source: str = "rule",
) -> None:
    store = get_store()
    store.add_audit_event(
        AuditEvent(
            id=new_id("ae"),
            created_at=now(),
            user_id=user_id,
            message=message or (nlu.raw_text if nlu else ""),
            nlu_source=nlu_source if nlu_source in {"rule", "llm"} else "unknown",
            intent=nlu.intent if nlu else (draft.source_text and "transfer") or "unknown",
            entities=nlu.entities.model_dump() if nlu else {},
            resolved_recipient=(
                draft.recipient.display_name if draft and draft.recipient else None
            ),
            selected_account=draft.source_account_id if draft else None,
            safety_flags=[f.code for f in draft.flags] if draft else [],
            auth_required=list(draft.auth_required) if draft else [],
            auth_completed=list(draft.auth_completed) if draft else [],
            decision=decision,
        )
    )


# A4: temporal-reference → history period. Strip diacritics so both
# "tháng trước" and "thang truoc" map correctly.
def _default_transfer_description(user_id: str) -> str:
    user = get_store().get_user(user_id)
    return f"{user.display_name} chuyển tiền"


def _period_from_temporal(temporal_ref: Optional[str]) -> str:
    if not temporal_ref:
        return "this_month"
    folded = normalize_alias(temporal_ref)
    if "thang truoc" in folded or "lan truoc" in folded:
        return "last_month"
    if "tuan truoc" in folded or "hom qua" in folded or "vua roi" in folded:
        return "recent_30d"
    return "this_month"


def handle_message(user_id: str, text: str) -> OmniResponse:
    text = text.strip()
    session = session_for(user_id)
    # Snapshot history *before* we append the current turn so the LLM
    # receives previous turns as context, with the current one as the new
    # user message.
    history_msgs = session.conversation_messages()

    # Direct continuation paths — for any in-flight draft, "xác nhận"/"huỷ"
    # acts on the matching draft rather than spawning a new NLU round.
    if session.current_draft is not None:
        cont = _try_continue_draft(user_id, text, session.current_draft)
        if cont is not None:
            session.append("user", text)
            session.append("omni", cont.text)
            return cont

    # A1: contact draft can be confirmed/cancelled by chat keyword.
    if session.current_contact_draft is not None:
        cont = _try_continue_contact_draft(
            user_id, text, session.current_contact_draft
        )
        if cont is not None:
            session.append("user", text)
            session.append("omni", cont.text)
            return cont

    # A2: same for schedule draft.
    if session.current_schedule_draft is not None:
        cont = _try_continue_schedule_draft(
            user_id, text, session.current_schedule_draft
        )
        if cont is not None:
            session.append("user", text)
            session.append("omni", cont.text)
            return cont

    nlu = understand(text, history=history_msgs)

    # Follow-up modify path: there's an active draft and the user is still
    # talking about transfer — treat as an edit, not a brand-new transaction.
    if (
        session.current_draft is not None
        and nlu.intent == "transfer"
        and _looks_like_modification(nlu, session.current_draft)
    ):
        resp = _modify_transfer_draft(user_id, session.current_draft, nlu)
        session.append("user", text)
        session.append("omni", resp.text)
        return resp

    resp = _dispatch_intent(user_id, nlu, history_msgs)
    session.append("user", text)
    session.append("omni", resp.text)
    return resp


def _dispatch_intent(
    user_id: str, nlu: NLUResult, history_msgs: list[dict]
) -> OmniResponse:
    if nlu.intent == "balance":
        return _handle_balance(user_id, nlu.raw_text, history_msgs)

    if nlu.intent == "history":
        return _handle_history(user_id, nlu, history_msgs)

    if nlu.intent == "schedule":
        return _handle_schedule(user_id, nlu)

    if nlu.intent == "add_contact":
        return _handle_add_contact(user_id, nlu)

    if nlu.intent == "transfer":
        return _handle_transfer(user_id, nlu)

    if nlu.intent == "smalltalk":
        fallback = "Chào bạn! Mình là Omni — sẵn sàng giúp bạn chuyển tiền, xem số dư hay tra lịch sử."
        text = llm_phrase(
            nlu.raw_text,
            {
                "intent": "smalltalk",
                "instruction": (
                    "Phản hồi lại cách chào của khách (trang trọng, suồng sã, "
                    "tiếng Việt hay tiếng Anh) theo đúng tông giọng họ dùng. "
                    "Mỗi lần phản hồi nên hơi khác nhau."
                ),
                "capabilities": [
                    "chuyển tiền bằng ngôn ngữ tự nhiên",
                    "xem số dư & lịch sử",
                    "đặt lịch định kỳ",
                    "cảnh báo giao dịch bất thường",
                ],
            },
            history=history_msgs,
            temperature=0.9,
        ) or fallback
        return OmniResponse(intent="smalltalk", text=text)

    # SAFETY: don't let the LLM phrase responses when there are no facts to
    # ground them in. With conversation history in scope, the model will
    # otherwise invent numbers based on earlier turns (e.g. fabricating a
    # secondary account balance). Use the deterministic fallback instead.
    return OmniResponse(
        intent="unknown",
        text=(
            "Mình chưa rõ ý bạn. Bạn thử nói cụ thể hơn nhé — ví dụ "
            "\"chuyển cho mẹ 2 triệu\" hoặc \"tháng này tiêu bao nhiêu?\""
        ),
    )


def _looks_like_modification(nlu: NLUResult, draft: TransactionDraft) -> bool:
    """Heuristic: if the user is changing amount, description, or recipient
    of an existing draft, route to the modify path instead of creating a new
    draft. We trigger when the new message contains at least one of those
    fields AND the user isn't repeating the same data verbatim."""
    e = nlu.entities
    if e.amount is not None and e.amount != draft.amount:
        return True
    if e.description and e.description != draft.description:
        return True
    if e.recipient_text:
        # Different name surface form
        if draft.recipient is None:
            return True
        # Best-effort: if the new surface doesn't match the draft recipient,
        # this is a recipient swap.
        from ..context.alias import _fold

        if _fold(e.recipient_text) not in _fold(draft.recipient.display_name):
            return True
    return False


def _modify_transfer_draft(
    user_id: str, draft: TransactionDraft, nlu: NLUResult
) -> OmniResponse:
    store = get_store()
    contacts = store.contacts_of(user_id)
    txs = store.transactions_of(user_id)
    account = (
        store.account_by_id(user_id, draft.source_account_id)
        if draft.source_account_id
        else store.primary_account(user_id)
    )
    e = nlu.entities

    if e.amount is not None and e.amount != draft.amount:
        draft.amount = e.amount
    if e.description:
        draft.description = e.description

    if e.recipient_text:
        candidates = resolve_recipient(e.recipient_text, contacts)
        if len(candidates) == 1:
            draft.recipient = candidates[0].contact
            draft.candidates = []
        elif len(candidates) > 1:
            draft.recipient = None
            draft.candidates = [c.contact for c in candidates]

    # Re-evaluate safety on the modified draft.
    draft.flags = evaluate(
        amount=draft.amount,
        recipient_candidates=[],
        recipient=draft.recipient,
        transactions=txs,
        account=account,
    )
    draft.requires_step_up = requires_step_up(draft.flags)
    draft.auth_required = auth_policy(draft.flags)
    draft.auth_completed = [
        a for a in draft.auth_completed if a in draft.auth_required
    ]

    session_for(user_id).set_draft(draft)
    return OmniResponse(
        intent="transfer",
        text=_compose_transfer_text(draft, None),
        draft=draft,
        needs_disambiguation=any(f.code == "ambiguous_recipient" for f in draft.flags),
    )


# ---------------------------------------------------------------------------
# Per-intent handlers
# ---------------------------------------------------------------------------


def _handle_balance(
    user_id: str, user_text: str = "", history_msgs: Optional[list[dict]] = None
) -> OmniResponse:
    bal = get_balance(user_id)
    primary = next((a for a in bal["accounts"] if a["primary"]), bal["accounts"][0])
    fallback = (
        f"Số dư tài khoản chính của bạn là {format_vnd(primary['balance'])}. "
        f"Tổng các tài khoản: {format_vnd(bal['total'])}."
    )
    text = (
        llm_phrase(
            user_text or "Số dư hiện tại của tôi?",
            {
                "intent": "balance",
                "primary_balance": primary["balance"],
                "total_balance": bal["total"],
                "accounts": [
                    {"bank": a["bank"], "masked": "•" * 4 + a["number"][-4:], "balance": a["balance"], "primary": a["primary"]}
                    for a in bal["accounts"]
                ],
            },
            history=history_msgs,
        )
        or fallback
    )
    return OmniResponse(intent="balance", text=text, balance=bal)


def _handle_history(
    user_id: str, nlu: NLUResult, history_msgs: Optional[list[dict]] = None
) -> OmniResponse:
    contacts = get_store().contacts_of(user_id)

    contact_id: Optional[str] = None
    contact_name: Optional[str] = None
    if nlu.entities.recipient_text:
        candidates = resolve_recipient(nlu.entities.recipient_text, contacts)
        if len(candidates) == 1:
            contact_id = candidates[0].contact.id
            contact_name = candidates[0].contact.display_name

    # A4: normalize temporal phrasing (handles "thang truoc" no-diacritic too).
    user_asked_specific_period = nlu.entities.temporal_reference is not None
    period = _period_from_temporal(nlu.entities.temporal_reference)

    hist = get_history(user_id=user_id, contact_id=contact_id, period=period)

    # A3: if the user didn't ask for a specific period and this_month is empty,
    # silently fall back to last_month (with a note in the reply).
    fell_back = False
    if (
        not user_asked_specific_period
        and period == "this_month"
        and hist["count"] == 0
    ):
        last_hist = get_history(
            user_id=user_id, contact_id=contact_id, period="last_month"
        )
        if last_hist["count"] > 0:
            hist = last_hist
            period = "last_month"
            fell_back = True

    period_label = {
        "this_month": "tháng này",
        "last_month": "tháng trước",
        "recent_30d": "30 ngày gần đây",
    }.get(period, period)

    if hist["count"] == 0:
        body = f"Bạn chưa có giao dịch nào {period_label}"
        if contact_name:
            body += f" với {contact_name}"
        body += "."
    else:
        target = f" cho {contact_name}" if contact_name else ""
        fallback = (
            f"{period_label.capitalize()}{target}: bạn đã chuyển "
            f"{format_vnd(hist['total'])} qua {hist['count']} giao dịch. "
            f"Trung bình {format_vnd(hist['average'])} mỗi lần."
        )
        # Let the LLM phrase the answer using the full breakdown so it can
        # respond to questions like "vào những chủ đề nào", "ai nhận nhiều
        # nhất", etc. — but the data it cites is whatever's in `facts`.
        facts = {
            "intent": "history",
            "period_label": period_label,
            "contact_filter": contact_name,
            "count": hist["count"],
            "total": hist["total"],
            "average": hist["average"],
            "by_category": hist["by_category"],
            "by_recipient": hist["by_recipient"],
            "fell_back_from_this_month": fell_back,
            "descriptions": [
                {
                    "recipient": t["contact"]["display_name"],
                    "amount": t["amount"],
                    "description": t["description"],
                    "category": t["category"],
                }
                for t in hist["items"]
            ],
        }
        # When we silently fell back from an empty this_month, the LLM sometimes
        # latches onto the "not enough info" stock answer despite FACTS being
        # populated. Use the deterministic template in that case — it reads
        # cleanly and never apologises for data we actually have.
        if fell_back:
            body = (
                "Tháng này bạn chưa có giao dịch nào, mình lấy dữ liệu tháng trước nhé. "
                + fallback
            )
        else:
            body = llm_phrase(nlu.raw_text, facts, history=history_msgs) or fallback
    return OmniResponse(intent="history", text=body, history=hist)


def _handle_schedule(user_id: str, nlu: NLUResult) -> OmniResponse:
    store = get_store()
    contacts = store.contacts_of(user_id)
    e = nlu.entities

    if not e.recipient_text or not e.amount or not e.schedule_cron:
        return OmniResponse(
            intent="schedule",
            text=(
                "Để đặt lịch, mình cần biết người nhận, số tiền và tần suất. "
                "Ví dụ: \"đặt lịch chuyển mẹ 2tr vào mùng 1 hàng tháng\"."
            ),
        )

    candidates = resolve_recipient(e.recipient_text, contacts)
    if len(candidates) != 1:
        return OmniResponse(
            intent="schedule",
            text="Bạn nói rõ người nhận giúp mình nhé.",
        )
    recipient = candidates[0].contact
    account = store.primary_account(user_id)

    # A2: stage a draft — persistence happens only on confirm.
    draft = ScheduleDraft(
        id=new_id("sd"),
        recipient=recipient,
        source_account_id=account.id,
        source_accounts=store.get_user(user_id).accounts,
        amount=e.amount,
        description=e.description or "Định kỳ",
        cron=e.schedule_cron,
        cron_label=_cron_label(e.schedule_cron),
        next_run=next_run_for(e.schedule_cron, now()),
    )
    session_for(user_id).set_schedule_draft(draft)
    return OmniResponse(
        intent="schedule",
        text=(
            f"Mình sẽ đặt lịch chuyển {format_vnd(draft.amount)} cho "
            f"{recipient.display_name} ({recipient.bank}) — "
            f"{draft.cron_label}. Lần đầu tiên: "
            f"{draft.next_run.strftime('%d/%m/%Y')}. Xác nhận giúp mình nhé."
        ),
        schedule_draft=draft,
    )


def _cron_label(cron: str) -> str:
    """Pretty-print the cron expressions we generate in the entity extractor."""
    parts = cron.split()
    if len(parts) == 5:
        _, _, dom, _, dow = parts
        if dom.isdigit():
            return f"vào ngày {int(dom)} hàng tháng"
        if dow.isdigit():
            names = ["thứ Hai", "thứ Ba", "thứ Tư", "thứ Năm", "thứ Sáu", "thứ Bảy", "Chủ Nhật"]
            return f"vào {names[int(dow) % 7]} hàng tuần"
    return "định kỳ"


def confirm_schedule_draft(
    user_id: str,
    draft_id: str,
    otp: str | None = None,
    source_account_id: str | None = None,
) -> OmniResponse:
    session = session_for(user_id)
    draft = session.current_schedule_draft
    if draft is None or draft.id != draft_id:
        return OmniResponse(intent="unknown", text="Không tìm thấy lịch chờ xác nhận.")

    if source_account_id:
        try:
            get_store().account_by_id(user_id, source_account_id)
        except KeyError:
            return OmniResponse(intent="schedule", text="Tài khoản nguồn không hợp lệ.", schedule_draft=draft)
        draft.source_account_id = source_account_id
        session.set_schedule_draft(draft)

    if otp is None:
        text = "Vui lòng nhập OTP để xác minh lịch chuyển định kỳ. Mã demo: 123456."
        session.append("user", "Xác nhận lịch định kỳ")
        session.append("omni", text)
        return OmniResponse(intent="schedule", text=text, schedule_draft=draft)

    if otp.strip() != "123456":
        text = "OTP chưa đúng. Bạn kiểm tra và nhập lại mã xác minh nhé."
        session.append("user", "Xác minh OTP lịch định kỳ")
        session.append("omni", text)
        return OmniResponse(intent="schedule", text=text, schedule_draft=draft)

    sched = create_schedule(
        user_id=user_id,
        recipient=draft.recipient,
        amount=draft.amount,
        cron=draft.cron,
        description=draft.description,
        source_account_id=draft.source_account_id,
    )
    session.clear_schedule_draft()
    text = (
        f"Đã tạo lịch định kỳ: chuyển {format_vnd(draft.amount)} cho "
        f"{draft.recipient.display_name} ({draft.recipient.bank}). "
        f"Lần kế: {sched.next_run.strftime('%d/%m/%Y')}. "
        "Mình sẽ nhắc bạn xác nhận trước mỗi lần."
    )
    session.append("user", "Xác nhận lịch định kỳ")
    session.append("omni", text)
    return OmniResponse(intent="schedule", text=text, schedule=sched)


def cancel_schedule_draft(user_id: str, draft_id: str) -> OmniResponse:
    session = session_for(user_id)
    if session.current_schedule_draft and session.current_schedule_draft.id == draft_id:
        session.clear_schedule_draft()
    text = "Đã huỷ đặt lịch."
    session.append("user", "Huỷ đặt lịch")
    session.append("omni", text)
    return OmniResponse(intent="schedule", text=text)


def _handle_add_contact(user_id: str, nlu: NLUResult) -> OmniResponse:
    """Stage a new-contact draft. Persistence happens only on user confirm."""
    e = nlu.entities
    store = get_store()
    flags: list[SafetyFlag] = []

    name = (e.recipient_text or "").strip()
    account = "".join(ch for ch in (e.account_hint or "") if ch.isdigit())
    bank = (e.bank_name or "").strip()
    alias = (e.alias or "").strip().lower() or None

    if not name:
        flags.append(SafetyFlag(code="missing_recipient", severity="block",
                                message="Bạn cho mình biết tên người cần lưu nhé."))
    if not account or len(account) < 6:
        flags.append(SafetyFlag(code="missing_recipient", severity="block",
                                message="Số tài khoản chưa hợp lệ. Mình cần dãy số đầy đủ (≥6 chữ số)."))
    if not bank:
        flags.append(SafetyFlag(code="missing_recipient", severity="block",
                                message="Ngân hàng của tài khoản này là gì?"))

    # Duplicate guard: don't allow saving an account that's already in the book.
    if account:
        existing = store.find_contact_by_account(user_id, account)
        if existing:
            flags.append(SafetyFlag(
                code="missing_recipient",
                severity="block",
                message=(
                    f"Số tài khoản này đã có trong danh bạ với tên "
                    f"{existing.display_name} ({existing.bank})."
                ),
            ))

    if any(f.severity == "block" for f in flags):
        return OmniResponse(
            intent="add_contact",
            text=" ".join(f.message for f in flags if f.severity == "block"),
        )

    draft = ContactDraft(
        id=new_id("cd"),
        display_name=name,
        bank=bank,
        account_number=account,
        account_masked="*" + account[-3:],
        aliases=[alias] if alias else [],
        label=None,
        flags=flags,
    )
    session_for(user_id).set_contact_draft(draft)
    text = (
        f"Mình sẽ lưu {name} — {bank} ({draft.account_masked}) vào danh bạ"
        + (f", với tên gọi tắt \"{alias}\"" if alias else "")
        + ". Xác nhận giúp mình nhé."
    )
    return OmniResponse(intent="add_contact", text=text, contact_draft=draft)


def confirm_contact_draft(user_id: str, draft_id: str) -> OmniResponse:
    session = session_for(user_id)
    draft = session.current_contact_draft
    if draft is None or draft.id != draft_id:
        return OmniResponse(intent="unknown", text="Không tìm thấy danh bạ chờ xác nhận.")

    contact = Contact(
        id=new_id("c"),
        owner_id=user_id,
        display_name=draft.display_name,
        bank=draft.bank,
        account_number=draft.account_number,
        account_masked=draft.account_masked,
        aliases=draft.aliases,
        label=draft.label,
        verified=False,
        frequent=False,
    )
    get_store().add_contact(contact)
    session.clear_contact_draft()
    text = (
        f"Đã thêm {contact.display_name} ({contact.bank} {contact.account_masked}) "
        "vào danh bạ. Lần sau bạn chỉ cần nhắc tên là mình tìm được."
    )
    session.append("user", "Lưu danh bạ")
    session.append("omni", text)
    return OmniResponse(intent="add_contact", text=text)


def cancel_contact_draft(user_id: str, draft_id: str) -> OmniResponse:
    session = session_for(user_id)
    if session.current_contact_draft and session.current_contact_draft.id == draft_id:
        session.clear_contact_draft()
    text = "Đã huỷ thêm danh bạ."
    session.append("user", "Huỷ lưu danh bạ")
    session.append("omni", text)
    return OmniResponse(intent="add_contact", text=text)


def _handle_transfer(user_id: str, nlu: NLUResult) -> OmniResponse:
    store = get_store()
    contacts = store.contacts_of(user_id)
    txs = store.transactions_of(user_id)
    account = store.primary_account(user_id)
    e = nlu.entities

    candidates = (
        resolve_recipient(e.recipient_text, contacts) if e.recipient_text else []
    )
    account_hint_mismatch = False
    if e.account_hint:
        if candidates:
            filtered_candidates = filter_by_account_hint(candidates, e.account_hint)
            account_hint_mismatch = not filtered_candidates
            candidates = filtered_candidates
        else:
            candidates = resolve_by_account_hint(e.account_hint, contacts)

    chosen: Optional[Contact] = None
    if len(candidates) == 1:
        chosen = candidates[0].contact

    # Resolve temporal reference using the chosen recipient (if any) for higher precision
    amount = e.amount
    description = e.description or ""
    reference_tx_id: Optional[str] = None
    referenced_tx = None
    if e.temporal_reference:
        referenced_tx = resolve_temporal_reference(
            e.temporal_reference,
            chosen.id if chosen else None,
            txs,
        )
        if referenced_tx is not None:
            reference_tx_id = referenced_tx.id
            if amount is None:
                amount = referenced_tx.amount
            if not description:
                description = referenced_tx.description
            # If recipient was implied but not matched, take it from the past tx.
            if chosen is None and not candidates:
                chosen = store.contacts.get(referenced_tx.contact_id)

    if not description:
        description = _default_transfer_description(user_id)

    flags = evaluate(
        amount=amount,
        recipient_candidates=candidates,
        recipient=chosen,
        transactions=txs,
        account=account,
    )
    if account_hint_mismatch:
        flags = [
            f
            for f in flags
            if f.code not in ("missing_recipient", "ambiguous_recipient")
        ]
        flags.append(
            SafetyFlag(
                code="account_hint_mismatch",
                severity="block",
                message=(
                    "Số tài khoản bạn nhập không khớp với người nhận trong danh bạ. "
                    "Mình sẽ không thực hiện giao dịch này."
                ),
            )
        )
    required_auth = auth_policy(flags)

    draft = TransactionDraft(
        id=new_id("d"),
        recipient=chosen,
        candidates=[c.contact for c in candidates] if chosen is None else [],
        source_account_id=account.id,
        source_accounts=store.get_user(user_id).accounts,
        amount=amount,
        description=description,
        source_text=nlu.raw_text,
        reference_transaction_id=reference_tx_id,
        flags=flags,
        requires_step_up=requires_step_up(flags),
        auth_required=required_auth,
    )

    session = session_for(user_id)
    session.set_draft(draft)
    _record_audit(
        user_id,
        nlu=nlu,
        draft=draft,
        decision="blocked" if is_blocked(flags) else "draft_created",
    )

    text = _compose_transfer_text(draft, referenced_tx)
    return OmniResponse(
        intent="transfer",
        text=text,
        draft=draft,
        needs_disambiguation=any(f.code == "ambiguous_recipient" for f in flags),
    )


# ---------------------------------------------------------------------------
# Draft continuation (confirm / cancel / pick candidate)
# ---------------------------------------------------------------------------


def _try_continue_draft(
    user_id: str, text: str, draft: TransactionDraft
) -> Optional[OmniResponse]:
    if _is_cancel(text):
        return cancel_draft(user_id, draft.id)

    if _is_confirm(text):
        return confirm_draft(user_id, draft.id)

    if re.fullmatch(r"\d{6}", text.strip()):
        return confirm_draft(user_id, draft.id, otp=text.strip())

    folded = normalize_alias(text)
    if "sinh trac" in folded or "biometric" in folded or "khuon mat" in folded:
        return confirm_draft(user_id, draft.id, biometric_verified=True)

    # "Chọn Trần Hoàng Minh" / "Trần Hoàng Minh"
    if draft.recipient is None and draft.candidates:
        candidate = _match_candidate(text, draft.candidates)
        if candidate is not None:
            return select_candidate(user_id, draft.id, candidate.id)

    return None


def _try_continue_contact_draft(
    user_id: str, text: str, draft: ContactDraft
) -> Optional[OmniResponse]:
    if _is_cancel(text):
        return cancel_contact_draft(user_id, draft.id)
    if _is_confirm(text):
        return confirm_contact_draft(user_id, draft.id)
    return None


def _try_continue_schedule_draft(
    user_id: str, text: str, draft: ScheduleDraft
) -> Optional[OmniResponse]:
    if _is_cancel(text):
        return cancel_schedule_draft(user_id, draft.id)
    if _is_confirm(text):
        return confirm_schedule_draft(user_id, draft.id)
    if re.fullmatch(r"\d{6}", text.strip()):
        return confirm_schedule_draft(user_id, draft.id, otp=text.strip())
    return None


def _match_candidate(text: str, candidates: list[Contact]) -> Optional[Contact]:
    from ..context.alias import _fold  # local import to avoid leaking helper

    folded = _fold(text)
    for c in candidates:
        if _fold(c.display_name) in folded or folded in _fold(c.display_name):
            return c
        for alias in c.aliases:
            if _fold(alias) and _fold(alias) in folded:
                return c
    return None


# ---------------------------------------------------------------------------
# Public draft actions
# ---------------------------------------------------------------------------


def confirm_draft(
    user_id: str,
    draft_id: str,
    otp: str | None = None,
    source_account_id: str | None = None,
    biometric_scan: dict[str, Any] | None = None,
    biometric_verified: bool = False,
) -> OmniResponse:
    session = session_for(user_id)
    draft = session.current_draft
    if draft is None or draft.id != draft_id:
        return OmniResponse(intent="unknown", text="Không tìm thấy giao dịch chờ xác nhận.")

    # B7: re-run safety with fresh balance/history, not the stale snapshot
    # captured at draft creation time.
    store = get_store()
    txs = store.transactions_of(user_id)
    if source_account_id:
        try:
            store.account_by_id(user_id, source_account_id)
        except KeyError:
            return OmniResponse(intent="transfer", text="Tài khoản nguồn không hợp lệ.", draft=draft)
        draft.source_account_id = source_account_id
        session.set_draft(draft)
    account = (
        store.account_by_id(user_id, draft.source_account_id)
        if draft.source_account_id
        else store.primary_account(user_id)
    )
    fresh_flags = evaluate(
        amount=draft.amount,
        recipient_candidates=[],
        recipient=draft.recipient,
        transactions=txs,
        account=account,
    )
    draft.flags = fresh_flags
    draft.requires_step_up = requires_step_up(fresh_flags)
    draft.auth_required = auth_policy(fresh_flags)
    draft.auth_completed = [
        a for a in draft.auth_completed if a in draft.auth_required
    ]

    if is_blocked(draft.flags):
        msg = " ".join(f.message for f in draft.flags if f.severity == "block")
        _record_audit(user_id, message="Xac nhan giao dich", draft=draft, decision="blocked")
        session.append("user", "Xác nhận giao dịch")
        session.append("omni", msg)
        return OmniResponse(intent="transfer", text=msg, draft=draft)

    if draft.recipient is None or draft.amount is None:
        text = "Giao dịch còn thiếu thông tin."
        _record_audit(user_id, message="Xac nhan giao dich", draft=draft, decision="blocked")
        session.append("user", "Xác nhận giao dịch")
        session.append("omni", text)
        return OmniResponse(intent="transfer", text=text, draft=draft)

    if "biometric" in draft.auth_required and "biometric" not in draft.auth_completed and (biometric_scan or biometric_verified):
        scan_ok, scan_error = _biometric_scan_valid(biometric_scan, draft.id, otp)
        if not scan_ok:
            text = scan_error
            _record_audit(user_id, message="Xac minh sinh trac hoc", draft=draft, decision="auth_failed")
            session.append("user", "Xác minh sinh trắc học")
            session.append("omni", text)
            return OmniResponse(intent="transfer", text=text, draft=draft)
        if "biometric" not in draft.auth_completed:
            draft.auth_completed.append("biometric")
        session.set_draft(draft)
        if "otp" in draft.auth_required and "otp" not in draft.auth_completed and otp is None:
            text = "Sinh trắc học đã xác minh. Vui lòng nhập OTP để hoàn tất. Mã demo: 123456."
            _record_audit(user_id, message="Xac minh sinh trac hoc", draft=draft, decision="auth_partial")
            session.append("user", "Xác minh sinh trắc học")
            session.append("omni", text)
            return OmniResponse(intent="transfer", text=text, draft=draft)

    if "otp" in draft.auth_required and "otp" not in draft.auth_completed and otp is None:
        text = "Vui lòng nhập OTP để xác minh giao dịch. Mã demo: 123456."
        if "biometric" in draft.auth_required:
            text = "Giao dịch rủi ro cần OTP và sinh trắc học. Vui lòng nhập OTP trước. Mã demo: 123456."
        _record_audit(user_id, message="Xac nhan giao dich", draft=draft, decision="auth_required")
        session.append("user", "Xác nhận giao dịch")
        session.append("omni", text)
        return OmniResponse(intent="transfer", text=text, draft=draft)

    if "otp" in draft.auth_required and "otp" not in draft.auth_completed and otp and otp.strip() != "123456":
        text = "OTP chưa đúng. Bạn kiểm tra và nhập lại mã xác minh nhé."
        _record_audit(user_id, message="Xac minh OTP", draft=draft, decision="auth_failed")
        session.append("user", "Xác minh OTP")
        session.append("omni", text)
        return OmniResponse(intent="transfer", text=text, draft=draft)

    if "otp" in draft.auth_required and otp and "otp" not in draft.auth_completed:
        draft.auth_completed.append("otp")

    missing_auth = [
        method for method in draft.auth_required if method not in draft.auth_completed
    ]
    if missing_auth:
        text = "Còn thiếu xác thực: " + ", ".join(missing_auth) + "."
        if "biometric" in missing_auth:
            text = "Còn thiếu xác minh sinh trắc học. Bạn bấm mock biometric rồi gửi lại xác nhận nhé."
        session.set_draft(draft)
        _record_audit(user_id, message="Xac minh giao dich", draft=draft, decision="auth_partial")
        session.append("user", "Xác minh giao dịch")
        session.append("omni", text)
        return OmniResponse(intent="transfer", text=text, draft=draft)

    from ..banking.service import execute_transfer

    try:
        tx = execute_transfer(
            user_id=user_id,
            recipient=draft.recipient,
            amount=draft.amount,
            description=draft.description,
            source_account_id=draft.source_account_id,
        )
    except ValueError as e:
        text = f"Giao dịch thất bại: {e}"
        _record_audit(user_id, message="Xac nhan giao dich", draft=draft, decision="execute_failed")
        session.append("user", "Xác nhận giao dịch")
        session.append("omni", text)
        return OmniResponse(intent="transfer", text=text, draft=draft)

    _record_audit(user_id, message="Xac nhan giao dich", draft=draft, decision="executed")
    session.clear_draft()
    text = (
        f"Đã chuyển {format_vnd(tx.amount)} cho {draft.recipient.display_name} "
        f"({draft.recipient.bank}). Mã giao dịch: {tx.id}."
    )
    # B5: record this turn so follow-up questions ("Mình vừa chuyển bao
    # nhiêu?") can be answered from context.
    session.append("user", "Xác nhận giao dịch")
    session.append("omni", text)
    return OmniResponse(intent="transfer", text=text)


def cancel_draft(user_id: str, draft_id: str) -> OmniResponse:
    session = session_for(user_id)
    if session.current_draft and session.current_draft.id == draft_id:
        session.clear_draft()
    text = "Đã huỷ giao dịch."
    session.append("user", "Huỷ giao dịch")
    session.append("omni", text)
    return OmniResponse(intent="transfer", text=text)


def select_candidate(user_id: str, draft_id: str, contact_id: str) -> OmniResponse:
    session = session_for(user_id)
    draft = session.current_draft
    if draft is None or draft.id != draft_id:
        return OmniResponse(intent="unknown", text="Không tìm thấy giao dịch chờ xác nhận.")

    chosen = next((c for c in draft.candidates if c.id == contact_id), None)
    if chosen is None:
        candidate = get_store().contacts.get(contact_id)
        if candidate and candidate.owner_id == user_id:
            chosen = candidate
    if chosen is None:
        return OmniResponse(intent="transfer", text="Người nhận chưa khớp danh bạ.", draft=draft)

    store = get_store()
    txs = store.transactions_of(user_id)
    account = (
        store.account_by_id(user_id, draft.source_account_id)
        if draft.source_account_id
        else store.primary_account(user_id)
    )
    flags = evaluate(
        amount=draft.amount,
        recipient_candidates=[],
        recipient=chosen,
        transactions=txs,
        account=account,
    )

    draft.recipient = chosen
    draft.candidates = []
    draft.flags = flags
    draft.requires_step_up = requires_step_up(flags)
    draft.auth_required = auth_policy(flags)
    draft.auth_completed = [
        a for a in draft.auth_completed if a in draft.auth_required
    ]
    session.set_draft(draft)

    text = _compose_transfer_text(draft, None)
    session.append("user", f"Chọn {chosen.display_name}")
    session.append("omni", text)
    return OmniResponse(intent="transfer", text=text, draft=draft)


# ---------------------------------------------------------------------------
# Text composition
# ---------------------------------------------------------------------------


def _compose_transfer_text(draft: TransactionDraft, referenced_tx) -> str:
    if any(f.code == "account_hint_mismatch" for f in draft.flags):
        return next(
            f.message for f in draft.flags if f.code == "account_hint_mismatch"
        )

    if any(f.code == "ambiguous_recipient" for f in draft.flags):
        names = ", ".join(c.display_name for c in draft.candidates)
        return f"Bạn muốn chuyển cho ai trong số: {names}?"

    if any(f.code == "missing_recipient" for f in draft.flags):
        return "Mình chưa rõ bạn muốn chuyển cho ai. Bạn cho mình biết người nhận nhé."

    if any(f.code == "missing_amount" for f in draft.flags):
        who = draft.recipient.display_name if draft.recipient else "người nhận"
        return f"Bạn muốn chuyển bao nhiêu cho {who}?"

    warn = next(
        (f for f in draft.flags if f.severity == "warn"),
        None,
    )
    if warn is not None:
        return warn.message + " Bạn xác nhận mình mới thực hiện nhé."

    if referenced_tx is not None and draft.recipient is not None and draft.amount is not None:
        return (
            f"Tháng trước bạn gửi {format_vnd(referenced_tx.amount)} cho "
            f"{draft.recipient.display_name} ({draft.recipient.bank}). Lặp lại?"
        )

    if draft.recipient is not None and draft.amount is not None:
        return (
            f"Đã hiểu! Xác nhận chuyển {format_vnd(draft.amount)} cho "
            f"{draft.recipient.display_name} ({draft.recipient.bank})."
        )

    return "Mình cần thêm thông tin để hoàn tất giao dịch."


__all__ = [
    "handle_message",
    "confirm_draft",
    "cancel_draft",
    "select_candidate",
    "confirm_contact_draft",
    "cancel_contact_draft",
    "confirm_schedule_draft",
    "cancel_schedule_draft",
]

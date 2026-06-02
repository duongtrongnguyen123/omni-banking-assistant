"""Conversation orchestrator: NLU → Context → Safety → Banking.

The brain of Omni — turns a user utterance into an OmniResponse with the
appropriate side-effects (draft creation, history lookup, schedule creation).
"""

from __future__ import annotations

import re
from typing import Optional

from ..banking.service import create_schedule, get_balance, get_history
from ..context import resolve_recipient, resolve_temporal_reference, session_for
from ..context.alias import filter_by_account_hint
from ..models.schemas import (
    Contact,
    ContactDraft,
    NLUResult,
    OmniResponse,
    SafetyFlag,
    TransactionDraft,
)
from ..nlp.amount import format_vnd
from ..nlp.llm import llm_phrase
from ..nlp.pipeline import understand
from ..safety.rules import evaluate, is_blocked, requires_step_up
from ..store import get_store, new_id

_CONFIRM_RE = re.compile(r"^(xac nhan|xacnhan|ok|đồng ý|dong y|y|yes|confirm|duyệt|duyet)\b|^xác nhận", re.IGNORECASE)
_CANCEL_RE = re.compile(r"^(huỷ|huy|cancel|hủy|không|khong|no|stop)\b", re.IGNORECASE)


def _is_confirm(text: str) -> bool:
    return bool(_CONFIRM_RE.search(text.strip().lower()))


def _is_cancel(text: str) -> bool:
    return bool(_CANCEL_RE.search(text.strip().lower()))


def handle_message(user_id: str, text: str) -> OmniResponse:
    text = text.strip()
    session = session_for(user_id)
    # Snapshot history *before* we append the current turn so the LLM
    # receives previous turns as context, with the current one as the new
    # user message.
    history_msgs = session.conversation_messages()

    # Direct continuation of an in-flight draft (confirm / cancel / select)
    if session.current_draft is not None:
        cont = _try_continue_draft(user_id, text, session.current_draft)
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
    account = store.primary_account(user_id)
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

    period = "this_month"
    if nlu.entities.temporal_reference and "tháng trước" in nlu.entities.temporal_reference.lower():
        period = "last_month"

    hist = get_history(user_id=user_id, contact_id=contact_id, period=period)
    period_label = "tháng này" if period == "this_month" else "tháng trước"
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
    sched = create_schedule(
        user_id=user_id,
        recipient=recipient,
        amount=e.amount,
        cron=e.schedule_cron,
        description=e.description or "Định kỳ",
    )
    return OmniResponse(
        intent="schedule",
        text=(
            f"Đã tạo lịch định kỳ: chuyển {format_vnd(e.amount)} cho "
            f"{recipient.display_name} ({recipient.bank}). "
            f"Lần kế: {sched.next_run.strftime('%d/%m/%Y')}. "
            "Mình sẽ nhắc bạn xác nhận trước mỗi lần."
        ),
        schedule=sched,
    )


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
    return OmniResponse(
        intent="add_contact",
        text=(
            f"Đã thêm {contact.display_name} ({contact.bank} {contact.account_masked}) "
            "vào danh bạ. Lần sau bạn chỉ cần nhắc tên là mình tìm được."
        ),
    )


def cancel_contact_draft(user_id: str, draft_id: str) -> OmniResponse:
    session = session_for(user_id)
    if session.current_contact_draft and session.current_contact_draft.id == draft_id:
        session.clear_contact_draft()
    return OmniResponse(intent="add_contact", text="Đã huỷ thêm danh bạ.")


def _handle_transfer(user_id: str, nlu: NLUResult) -> OmniResponse:
    store = get_store()
    contacts = store.contacts_of(user_id)
    txs = store.transactions_of(user_id)
    account = store.primary_account(user_id)
    e = nlu.entities

    candidates = (
        resolve_recipient(e.recipient_text, contacts) if e.recipient_text else []
    )
    if e.account_hint:
        candidates = filter_by_account_hint(candidates, e.account_hint)

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

    flags = evaluate(
        amount=amount,
        recipient_candidates=candidates,
        recipient=chosen,
        transactions=txs,
        account=account,
    )

    draft = TransactionDraft(
        id=new_id("d"),
        recipient=chosen,
        candidates=[c.contact for c in candidates] if chosen is None else [],
        amount=amount,
        description=description,
        source_text=nlu.raw_text,
        reference_transaction_id=reference_tx_id,
        flags=flags,
        requires_step_up=requires_step_up(flags),
    )

    session = session_for(user_id)
    session.set_draft(draft)

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

    # "Chọn Trần Hoàng Minh" / "Trần Hoàng Minh"
    if draft.recipient is None and draft.candidates:
        candidate = _match_candidate(text, draft.candidates)
        if candidate is not None:
            return select_candidate(user_id, draft.id, candidate.id)

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


def confirm_draft(user_id: str, draft_id: str) -> OmniResponse:
    session = session_for(user_id)
    draft = session.current_draft
    if draft is None or draft.id != draft_id:
        return OmniResponse(intent="unknown", text="Không tìm thấy giao dịch chờ xác nhận.")

    if is_blocked(draft.flags):
        msg = " ".join(f.message for f in draft.flags if f.severity == "block")
        return OmniResponse(intent="transfer", text=msg, draft=draft)

    if draft.recipient is None or draft.amount is None:
        return OmniResponse(intent="transfer", text="Giao dịch còn thiếu thông tin.", draft=draft)

    from ..banking.service import execute_transfer

    try:
        tx = execute_transfer(
            user_id=user_id,
            recipient=draft.recipient,
            amount=draft.amount,
            description=draft.description,
        )
    except ValueError as e:
        return OmniResponse(intent="transfer", text=f"Giao dịch thất bại: {e}", draft=draft)

    session.clear_draft()
    return OmniResponse(
        intent="transfer",
        text=(
            f"Đã chuyển {format_vnd(tx.amount)} cho {draft.recipient.display_name} "
            f"({draft.recipient.bank}). Mã giao dịch: {tx.id}."
        ),
        draft=draft,
    )


def cancel_draft(user_id: str, draft_id: str) -> OmniResponse:
    session = session_for(user_id)
    if session.current_draft and session.current_draft.id == draft_id:
        session.clear_draft()
    return OmniResponse(intent="transfer", text="Đã huỷ giao dịch.")


def select_candidate(user_id: str, draft_id: str, contact_id: str) -> OmniResponse:
    session = session_for(user_id)
    draft = session.current_draft
    if draft is None or draft.id != draft_id:
        return OmniResponse(intent="unknown", text="Không tìm thấy giao dịch chờ xác nhận.")

    chosen = next((c for c in draft.candidates if c.id == contact_id), None)
    if chosen is None:
        return OmniResponse(intent="transfer", text="Người nhận chưa khớp danh bạ.", draft=draft)

    store = get_store()
    txs = store.transactions_of(user_id)
    account = store.primary_account(user_id)
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
    session.set_draft(draft)

    text = _compose_transfer_text(draft, None)
    return OmniResponse(intent="transfer", text=text, draft=draft)


# ---------------------------------------------------------------------------
# Text composition
# ---------------------------------------------------------------------------


def _compose_transfer_text(draft: TransactionDraft, referenced_tx) -> str:
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
]

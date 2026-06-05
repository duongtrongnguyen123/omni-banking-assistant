"""Conversation orchestrator: NLU → Context → Safety → Banking.

The brain of Omni — turns a user utterance into an OmniResponse with the
appropriate side-effects (draft creation, history lookup, schedule creation).
"""

from __future__ import annotations

import re
from typing import Optional

from ..banking.recurring import detect_recurring
from ..banking.service import create_schedule, get_balance, get_history, next_run_for
from ..context import resolve_recipient, resolve_temporal_reference, session_for
from ..context.alias import filter_by_account_hint
from ..ml.amount_predictor import predict_amount
from ..models.schemas import (
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
from ..safety.rules import (
    NEW_RECIPIENT_LARGE_THRESHOLD,
    evaluate,
    is_blocked,
    requires_step_up,
)
from ..store import get_store, new_id, now

class _RawTx:
    """OPT-3 (bench): lightweight stand-in for ``models.schemas.Transaction``
    on hot paths that read a handful of fields off thousands of rows.
    Building real Pydantic models for the recurring miner cost ~5 s on
    contest data and we never used most of the fields.

    Mirrors the attribute surface the detector + safety baseline read:
    ``id``, ``contact_id``, ``amount``, ``description``, ``status``,
    ``created_at``. Comparable in memory but ~10× cheaper to construct."""

    __slots__ = ("id", "contact_id", "amount", "description",
                 "status", "created_at")

    def __init__(self, id, contact_id, amount, description, status, created_at):
        self.id = id
        self.contact_id = contact_id
        self.amount = amount
        self.description = description
        self.status = status
        self.created_at = created_at


def _txs_from_raw(rows: list[tuple]) -> list[_RawTx]:
    from datetime import datetime as _dt
    return [
        _RawTx(r[0], r[1], r[2], r[3], r[4], _dt.fromisoformat(r[5]))
        for r in rows
    ]


def _maybe_global_mean(
    user_id: str,
    recipient: Optional[Contact],
    amount: Optional[int],
    txs: list,
) -> Optional[float]:
    """OPT-3 (bench): the safety rule for cold-contact-anomaly only fires
    on a non-frequent recipient + large amount + too-thin per-contact
    history. Computing AVG(amount) on the contest dataset is ~380ms, so
    we lift that work behind a precondition check and skip it otherwise."""
    if (
        recipient is not None
        and amount is not None
        and not recipient.frequent
        and amount >= NEW_RECIPIENT_LARGE_THRESHOLD
        and len(txs) < 3
    ):
        return get_store().completed_amount_mean(user_id)
    return None


_CONFIRM_RE = re.compile(r"^(xac nhan|xacnhan|ok|đồng ý|dong y|y|yes|confirm|duyệt|duyet|lưu|luu)\b|^xác nhận", re.IGNORECASE)
_CANCEL_RE = re.compile(r"^(huỷ|huy|cancel|hủy|không|khong|no|stop|bỏ|bo)\b", re.IGNORECASE)
_OTP_RE = re.compile(r"^\s*(\d{4,6})\s*$")
_HELP_RE = re.compile(r"^\s*(/help|help|trợ giúp|tro giup|hướng dẫn|huong dan|menu)\s*$", re.IGNORECASE)


_HELP_TEXT = (
    "Mình có thể giúp bạn:\n"
    "• Chuyển tiền: \"chuyển cho mẹ 2 triệu\" — Omni hiểu biệt danh và "
    "lịch sử để gợi ý số tiền và lời nhắn.\n"
    "• Xem số dư: \"số dư\" hoặc gõ /balance.\n"
    "• Lịch sử chi tiêu: \"tháng này tiêu bao nhiêu?\" hoặc /history.\n"
    "• Lặp lại giao dịch trước: /repeat.\n"
    "• Đặt lịch định kỳ: \"đặt lịch chuyển mẹ 2tr mùng 1 hàng tháng\".\n"
    "• Tìm khoản trả định kỳ: \"mình có khoản nào trả đều?\".\n"
    "• Thêm danh bạ: \"lưu Nam STK 0123 MB Bank\".\n"
    "\n"
    "Phím tắt: Cmd/Ctrl+K (focus ô nhập), Cmd/Ctrl+/ (mở slash menu), "
    "Cmd/Ctrl+Enter (gửi lại tin nhắn vừa rồi), Esc (đóng popup), "
    "↑ (lịch sử tin nhắn), @ (gợi ý danh bạ)."
)


def _help_response() -> OmniResponse:
    """Structured help message — surfaced by the /help slash command and by
    typed help requests. Returns a smalltalk intent so the UI renders it
    without trying to attach a draft/balance/history payload."""
    return OmniResponse(intent="smalltalk", text=_HELP_TEXT)


def _is_confirm(text: str) -> bool:
    return bool(_CONFIRM_RE.search(text.strip().lower()))


def _is_cancel(text: str) -> bool:
    return bool(_CANCEL_RE.search(text.strip().lower()))


# A4: temporal-reference → history period. Strip diacritics so both
# "tháng trước" and "thang truoc" map correctly.
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

    # /help is a synthetic intent — no NLU, no LLM, deterministic copy. The
    # frontend slash palette dispatches it; users can also type "help" /
    # "trợ giúp" directly.
    if _HELP_RE.match(text):
        resp = _help_response()
        session.append("user", text)
        session.append("omni", resp.text)
        return resp

    # OTP step-up: if there's a draft awaiting OTP and the user typed digits,
    # treat the input as the OTP code and route to confirm_draft with the
    # typed code. confirm_draft validates it against the mock "123456" and
    # either executes or returns a "OTP chưa đúng" prompt.
    otp_match = _OTP_RE.match(text)
    if (
        session.current_draft is not None
        and session.current_draft.awaiting_otp
        and otp_match
    ):
        resp = confirm_draft(user_id, session.current_draft.id, otp=otp_match.group(1))
        # confirm_draft already appended; just record this user turn.
        session.append("user", text)
        return resp

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

    if nlu.intent == "recurring":
        return _handle_recurring(user_id, nlu, history_msgs)

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
    # OPT-3 (bench): scope to the recipient for the safety re-evaluation.
    # On modify the recipient may still change (see the swap branch
    # below); we reload txs there if needed.
    txs = (
        store.transactions_of(user_id, contact_id=draft.recipient.id)
        if draft.recipient is not None
        else []
    )
    account = (
        store.account_by_id(user_id, draft.source_account_id)
        if draft.source_account_id
        else store.primary_account(user_id)
    )
    e = nlu.entities

    if e.amount is not None and e.amount != draft.amount:
        draft.amount = e.amount
        # User just supplied an explicit amount — it's no longer predicted.
        draft.predicted_amount = False
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
        # OPT-3 (bench): recipient just changed — reload txs for the new
        # recipient so the per-contact baseline reflects the right peer set.
        if draft.recipient is not None:
            txs = store.transactions_of(user_id, contact_id=draft.recipient.id)

    # Re-evaluate safety on the modified draft.
    draft.flags = evaluate(
        amount=draft.amount,
        recipient_candidates=[],
        recipient=draft.recipient,
        transactions=txs,
        account=account,
        global_mean=_maybe_global_mean(user_id, draft.recipient, draft.amount, txs),
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


def _handle_recurring(
    user_id: str, nlu: NLUResult, history_msgs: Optional[list[dict]] = None
) -> OmniResponse:
    """Surface monthly recurring payments mined from history.

    Pure read intent — does not write a schedule row. The reply suggests
    the user can confirm one of the detected lines as a real schedule,
    but the actual ``schedule`` intent stays in charge of creation so the
    safety contract (rule-composed confirmation text) is preserved.
    """
    store = get_store()
    # OPT-3 (bench): recurring detection only meaningfully observes the
    # last ~12 months. Scanning further back wastes time on dormant
    # patterns that ``detect_recurring`` would drop as stale anyway
    # (its ``next_run`` filter rejects anything > 60 days behind ref_now).
    from datetime import timedelta as _td
    # Use the raw-tuple fetch path: building 520k Pydantic Transactions
    # only to read 4 fields off each was ~5s of pure Python overhead
    # before being handed to ``detect_recurring``.
    raw = store.transactions_raw(
        user_id, since=now() - _td(days=400), status="completed",
    )
    txs = _txs_from_raw(raw)
    contacts = store.contacts_of(user_id)
    contacts_by_id = {c.id: c for c in contacts}

    patterns = detect_recurring(txs)

    e = nlu.entities
    if e.recipient_text:
        candidates = resolve_recipient(e.recipient_text, contacts)
        wanted = {c.contact.id for c in candidates}
        if wanted:
            patterns = [p for p in patterns if p.contact_id in wanted]

    # Cap the slate so the LLM has a tight context and the UI stays readable.
    top = patterns[:5]

    def _name(cid: str) -> str:
        c = contacts_by_id.get(cid)
        return c.display_name if c else "(không rõ)"

    if not top:
        msg = (
            "Mình chưa thấy khoản nào đủ dữ liệu để khẳng định là định kỳ "
            "(cần ít nhất 3 tháng giao dịch giống nhau)."
        )
        return OmniResponse(intent="recurring", text=msg)

    facts = {
        "intent": "recurring",
        "instruction": (
            "Tóm tắt các khoản định kỳ trong PATTERNS. Mỗi khoản gồm "
            "tên người nhận, số tiền điển hình, ngày trong tháng. "
            "Đoạn ngắn, văn bản trần. Có thể gợi ý người dùng đặt lịch tự "
            "động nếu muốn — KHÔNG tự lập lịch."
        ),
        "patterns": [
            {
                "recipient": _name(p.contact_id),
                "description": p.description,
                "typical_amount": p.typical_amount,
                "typical_day": p.typical_day,
                "months_seen": p.month_count,
                "last_seen": p.last_seen.date().isoformat(),
                "next_expected": p.next_run.date().isoformat(),
                "confidence": p.confidence,
            }
            for p in top
        ],
    }

    # Deterministic fallback for when the LLM is unreachable.
    lines = []
    for p in top:
        lines.append(
            f"• {_name(p.contact_id)} — {format_vnd(p.typical_amount)} "
            f"({p.description}) khoảng ngày {p.typical_day} hàng tháng, "
            f"đã thấy {p.month_count} tháng."
        )
    fallback = "Mình thấy bạn có vài khoản trông như định kỳ:\n" + "\n".join(lines)
    if len(patterns) > len(top):
        fallback += f"\n…và {len(patterns) - len(top)} khoản khác."

    body = (
        llm_phrase(nlu.raw_text, facts, history=history_msgs)
        or fallback
    )

    # Surface the structured pattern list so the UI can render a recurring
    # card. We populate recipient_name / recipient_bank here (rather than in
    # the detector) so the detector stays a pure function of (tx, ref_now).
    enriched: list = []
    for p in top:
        c = contacts_by_id.get(p.contact_id)
        enriched.append(
            p.model_copy(
                update={
                    "recipient_name": c.display_name if c else None,
                    "recipient_bank": c.bank if c else None,
                }
            ).model_dump(mode="json")
        )

    return OmniResponse(intent="recurring", text=body, recurring_patterns=enriched)


def _handle_history(
    user_id: str, nlu: NLUResult, history_msgs: Optional[list[dict]] = None
) -> OmniResponse:
    contacts = get_store().contacts_of(user_id)
    e = nlu.entities

    contact_id: Optional[str] = None
    contact_name: Optional[str] = None
    if e.recipient_text:
        candidates = resolve_recipient(e.recipient_text, contacts)
        if len(candidates) == 1:
            contact_id = candidates[0].contact.id
            contact_name = candidates[0].contact.display_name

    # History-only fallback: when no verb/prep led the NLU extractor to
    # a recipient (e.g. "PT bao nhiêu tháng trước"), scan the raw message
    # for any contact alias as a whole word. Matching keeps the original
    # diacritics so "bảo" (Vũ Quốc Bảo) doesn't fold into "bao" of "bao
    # nhiêu" and match the wrong contact.
    if contact_id is None and not e.recipient_text:
        msg_lower = nlu.raw_text.lower()
        best: Optional[tuple[int, Contact]] = None
        for c in contacts:
            for alias in c.aliases:
                a = alias.lower()
                # Require the alias to appear as a whole word in the message.
                if re.search(rf"(?<!\w){re.escape(a)}(?!\w)", msg_lower):
                    # Prefer the longest alias (more specific).
                    if best is None or len(a) > best[0]:
                        best = (len(a), c)
                    break
        if best is not None:
            contact_id = best[1].id
            contact_name = best[1].display_name

    # A4: normalize temporal phrasing (handles "thang truoc" no-diacritic too).
    # Specific month / all_time override the temporal reference.
    user_asked_specific_period = (
        e.temporal_reference is not None
        or e.specific_month is not None
        or e.all_time
    )
    period = _period_from_temporal(e.temporal_reference)

    # When the user asked for "N most recent" without a period, search
    # the full history — defaulting to this_month silently turns "lần
    # cuối gửi mẹ" into a no-op when mẹ wasn't paid this month.
    if e.limit is not None and not user_asked_specific_period:
        e.all_time = True
        user_asked_specific_period = True

    # Same for semantic filter — "khoản chi liên quan đến sách" shouldn't
    # be artificially scoped to this month.
    if e.semantic_filter and not user_asked_specific_period:
        e.all_time = True
        user_asked_specific_period = True

    hist = get_history(
        user_id=user_id,
        contact_id=contact_id,
        period=period,
        specific_month=e.specific_month,
        specific_year=e.specific_year,
        all_time=e.all_time,
        limit=e.limit,
        semantic_filter=e.semantic_filter,
    )
    # get_history may have promoted the period (specific month, all_time);
    # sync our local var so the label matches what was actually queried.
    period = hist.get("period", period)

    # A3: if the user didn't ask for a specific period and this_month is empty,
    # silently fall back to last_month (with a note in the reply).
    fell_back = False
    if (
        not user_asked_specific_period
        and period == "this_month"
        and hist["count"] == 0
        and not e.semantic_filter
        and not e.limit
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
        "all_time": "tất cả thời gian",
    }.get(period, period)
    # Specific-month labels are emitted as YYYY-MM by get_history; render
    # them as "Tháng M/YYYY" so the reply reads naturally.
    if re.fullmatch(r"\d{4}-\d{2}", period):
        y, m = period.split("-")
        period_label = f"tháng {int(m)}/{y}"

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
        # Enrich the deterministic fallback with top-N highlights when the
        # user asked for them — important when the LLM is rate-limited
        # and can't phrase the answer itself.
        if e.top_recipient and hist["by_recipient"]:
            top = max(hist["by_recipient"].items(), key=lambda x: x[1])
            fallback += f" Người nhận nhiều nhất: {top[0]} ({format_vnd(top[1])})."
        if e.top_category and hist["by_category"]:
            top = max(hist["by_category"].items(), key=lambda x: x[1])
            fallback += f" Chủ đề nhiều nhất: {top[0]} ({format_vnd(top[1])})."
        # Let the LLM phrase the answer using the full breakdown so it can
        # respond to questions like "vào những chủ đề nào", "ai nhận nhiều
        # nhất", etc. — but the data it cites is whatever's in `facts`.
        # Top-N highlights for the LLM to cite when the user asks aggregations.
        top_recipient = (
            max(hist["by_recipient"].items(), key=lambda x: x[1])
            if hist["by_recipient"] else None
        )
        top_category = (
            max(hist["by_category"].items(), key=lambda x: x[1])
            if hist["by_category"] else None
        )
        facts = {
            "intent": "history",
            "period_label": period_label,
            "contact_filter": contact_name,
            "count": hist["count"],
            "total": hist["total"],
            "average": hist["average"],
            "by_category": hist["by_category"],
            "by_recipient": hist["by_recipient"],
            "top_recipient": (
                {"name": top_recipient[0], "total": top_recipient[1]}
                if top_recipient else None
            ),
            "top_category": (
                {"category": top_category[0], "total": top_category[1]}
                if top_category else None
            ),
            "fell_back_from_this_month": fell_back,
            "semantic_filter": e.semantic_filter,
            "limit_applied": e.limit,
            "descriptions": [
                {
                    "recipient": t["contact"]["display_name"],
                    "amount": t["amount"],
                    "description": t["description"],
                    "category": t["category"],
                    "created_at": t["created_at"],
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
        elif e.limit:
            # User asked for N most recent — list them, don't aggregate.
            lines = [
                f"{i+1}. {it['contact']['display_name']} — "
                f"{format_vnd(it['amount'])} ({it['description']})"
                for i, it in enumerate(hist["items"])
            ]
            body = (
                f"{e.limit} giao dịch gần nhất"
                + (f" với {contact_name}" if contact_name else "")
                + ":\n" + "\n".join(lines)
            )
            # Try LLM phrasing too — it sometimes produces nicer flow text.
            llm_text = llm_phrase(nlu.raw_text, facts, history=history_msgs)
            if llm_text:
                body = llm_text
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
    if account is None:
        return OmniResponse(
            intent="schedule",
            text="Chưa có tài khoản nguồn. Bạn liên kết tài khoản trước nhé.",
        )

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
    account = store.primary_account(user_id)
    if account is None:
        return OmniResponse(
            intent="transfer",
            text="Chưa có tài khoản nguồn. Bạn liên kết tài khoản trước nhé.",
        )
    e = nlu.entities

    candidates = (
        resolve_recipient(e.recipient_text, contacts) if e.recipient_text else []
    )
    if e.account_hint:
        candidates = filter_by_account_hint(candidates, e.account_hint)

    chosen: Optional[Contact] = None
    if len(candidates) == 1:
        chosen = candidates[0].contact

    # OPT-3 (bench): scope the tx fetch to the chosen recipient (when known)
    # or a small recent window otherwise. The previous code unconditionally
    # materialised every transaction the user has ever made (~520k Pydantic
    # objects on the contest dataset, ~16s on its own); only the per-contact
    # baseline + temporal-reference path actually need history.
    if chosen is not None:
        txs = store.transactions_of(user_id, contact_id=chosen.id)
    else:
        # Cold-contact path: we only need (a) "global mean" for the
        # anomaly check and (b) a small recent window for "lần trước"
        # temporal resolution. The mean is cheap from SQL so we lift it
        # out of ``evaluate``; for (b), ``limit=200`` covers every
        # temporal phrase we recognise (most recent in the last month).
        txs = store.transactions_of(user_id, limit=200)

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
                chosen = store.get_contact(referenced_tx.contact_id)
                # The newly resolved recipient may not be in our scoped
                # txs window; reload so the per-contact safety baseline
                # has the right peer set.
                if chosen is not None:
                    txs = store.transactions_of(user_id, contact_id=chosen.id)

    # Smart amount prediction: when the user named a recipient but no
    # amount, look at their history with this contact and pre-fill the
    # draft with the most likely figure. This must run *before* `evaluate`
    # so the `missing_amount` flag isn't raised on a draft that does have
    # an amount (a predicted one). The user can still override the value
    # in the confirm card or by saying "đổi sang 3 triệu thôi".
    prediction: Optional[dict] = None
    if amount is None and chosen is not None:
        prediction = predict_amount(user_id, chosen.id)
        if prediction is not None:
            amount = prediction["amount"]

    flags = evaluate(
        amount=amount,
        recipient_candidates=candidates,
        recipient=chosen,
        transactions=txs,
        account=account,
        global_mean=_maybe_global_mean(user_id, chosen, amount, txs),
    )

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
        predicted_amount=prediction is not None,
    )

    session = session_for(user_id)
    session.set_draft(draft)

    text = _compose_transfer_text(draft, referenced_tx)
    if prediction is not None and draft.recipient is not None:
        # Prepend the rationale so both the LLM-phrased and the
        # deterministic fallback responses surface the suggestion clearly.
        text = (
            f"Có vẻ bạn muốn gửi {format_vnd(draft.amount)} cho "  # type: ignore[arg-type]
            f"{draft.recipient.display_name} như thường lệ "
            f"({prediction['rationale']}). Đúng không? "
            + text
        )
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


# Ordinal patterns operate on the *folded* (diacritic-stripped) form of
# the user message — so "thứ 2" / "người đầu tiên" become "thu 2" /
# "nguoi dau tien" before matching.
_ORDINAL_PATTERNS: list[tuple[str, int]] = [
    (r"\b1\b|dau tien|dau hang|thu nhat|nhat\b|first", 0),
    (r"\b2\b|thu hai|second|kia\b|sau\b|con lai", 1),
    (r"\b3\b|thu ba|third", 2),
    (r"cuoi\b|cuoi cung|last|sau cung", -1),
]


def _match_candidate(text: str, candidates: list[Contact]) -> Optional[Contact]:
    from ..context.alias import _fold  # local import to avoid leaking helper

    folded = _fold(text)

    # 1) Ordinal pick — "người 1", "thứ hai", "kia", "cuối"
    for pattern, idx in _ORDINAL_PATTERNS:
        if re.search(pattern, folded, re.IGNORECASE):
            # Special-case "kia" / "còn lại": only meaningful with exactly 2
            # candidates ("the other one"). With 3+ it's ambiguous, so skip.
            if idx == 1 and ("kia" in folded or "con lai" in folded) and len(candidates) != 2:
                continue
            try:
                return candidates[idx]
            except IndexError:
                continue

    # 2) Name / alias token match
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
) -> OmniResponse:
    session = session_for(user_id)
    draft = session.current_draft
    if draft is None or draft.id != draft_id:
        return OmniResponse(intent="unknown", text="Không tìm thấy giao dịch chờ xác nhận.")

    # B7: re-run safety with fresh balance/history, not the stale snapshot
    # captured at draft creation time. OPT-3 (bench): scope to the
    # recipient — same reasoning as ``_handle_transfer``; on contest data
    # the unscoped fetch was a 16s tax we paid for nothing here, because
    # ``evaluate`` only consults the per-contact baseline at confirm time.
    store = get_store()
    txs = (
        store.transactions_of(user_id, contact_id=draft.recipient.id)
        if draft.recipient is not None
        else []
    )
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
        global_mean=_maybe_global_mean(user_id, draft.recipient, draft.amount, txs),
    )
    draft.flags = fresh_flags
    draft.requires_step_up = requires_step_up(fresh_flags)

    if is_blocked(draft.flags):
        msg = " ".join(f.message for f in draft.flags if f.severity == "block")
        session.append("user", "Xác nhận giao dịch")
        session.append("omni", msg)
        return OmniResponse(intent="transfer", text=msg, draft=draft)

    if draft.recipient is None or draft.amount is None:
        text = "Giao dịch còn thiếu thông tin."
        session.append("user", "Xác nhận giao dịch")
        session.append("omni", text)
        return OmniResponse(intent="transfer", text=text, draft=draft)

    # MVP step-up auth: every transfer must pass an OTP check before execution.
    # Two ways to provide the OTP:
    #   1) UI card sends it via the confirm endpoint body (`otp` param here).
    #   2) User types the digits in chat — handle_message catches that and
    #      re-routes to this function with `otp` filled in.
    if otp is None:
        # Mark the draft as awaiting OTP so handle_message can route the
        # next digit-only chat message back here.
        draft.awaiting_otp = True
        session.set_draft(draft)
        text = "Vui lòng nhập OTP để xác minh giao dịch. Mã demo: 123456."
        session.append("user", "Xác nhận giao dịch")
        session.append("omni", text)
        return OmniResponse(intent="transfer", text=text, draft=draft)

    if otp.strip() != "123456":
        text = "OTP chưa đúng. Bạn kiểm tra và nhập lại mã xác minh nhé."
        session.append("user", "Xác minh OTP")
        session.append("omni", text)
        return OmniResponse(intent="transfer", text=text, draft=draft)

    return _execute_and_record(user_id, draft, otp_used=True)


def _execute_and_record(
    user_id: str, draft: TransactionDraft, *, otp_used: bool
) -> OmniResponse:
    from ..banking.service import execute_transfer

    session = session_for(user_id)
    try:
        tx = execute_transfer(
            user_id=user_id,
            recipient=draft.recipient,  # type: ignore[arg-type]
            amount=draft.amount,  # type: ignore[arg-type]
            description=draft.description,
            source_account_id=draft.source_account_id,
        )
    except ValueError as e:
        text = f"Giao dịch thất bại: {e}"
        session.append("omni", text)
        return OmniResponse(intent="transfer", text=text, draft=draft)

    session.clear_draft()
    otp_note = " (đã xác minh OTP)" if otp_used else ""
    text = (
        f"Đã chuyển {format_vnd(tx.amount)} cho {draft.recipient.display_name} "  # type: ignore[union-attr]
        f"({draft.recipient.bank}){otp_note}. Mã giao dịch: {tx.id}."  # type: ignore[union-attr]
    )
    session.append("omni", text)

    # Retrain the next-recipient suggester so the sidebar reflects this
    # transfer immediately. Lightweight — fully trained on 35 rows in ~50ms.
    try:
        from ..ml.suggester import train_for

        train_for(user_id)
    except Exception:  # never block on the suggestion side-effect
        pass

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
        return OmniResponse(intent="transfer", text="Người nhận chưa khớp danh bạ.", draft=draft)

    store = get_store()
    # OPT-3 (bench): per-contact slice instead of the full history.
    txs = store.transactions_of(user_id, contact_id=chosen.id)
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
        global_mean=_maybe_global_mean(user_id, chosen, draft.amount, txs),
    )

    draft.recipient = chosen
    draft.candidates = []
    draft.flags = flags
    draft.requires_step_up = requires_step_up(flags)
    session.set_draft(draft)

    text = _compose_transfer_text(draft, None)
    session.append("user", f"Chọn {chosen.display_name}")
    session.append("omni", text)
    return OmniResponse(intent="transfer", text=text, draft=draft)


# ---------------------------------------------------------------------------
# Text composition
# ---------------------------------------------------------------------------


def _compose_transfer_text(draft: TransactionDraft, referenced_tx) -> str:
    has_amb = any(f.code == "ambiguous_recipient" for f in draft.flags)
    has_miss_rec = any(f.code == "missing_recipient" for f in draft.flags)
    has_miss_amt = any(f.code == "missing_amount" for f in draft.flags)

    # If any slot is missing/ambiguous, build a single combined question
    # that mentions every blank at once (amount AND recipient if both
    # missing — "Bạn muốn chuyển bao nhiêu cho Minh nào?").
    if has_amb or has_miss_rec or has_miss_amt:
        amount_part = "bao nhiêu" if has_miss_amt else format_vnd(draft.amount)  # type: ignore[arg-type]

        if has_amb:
            names = ", ".join(c.display_name for c in draft.candidates)
            common = _common_short_name(draft.candidates)
            recipient_part = (
                f"cho {common} nào (giữa {names})"
                if common
                else f"cho ai trong: {names}"
            )
        elif has_miss_rec:
            recipient_part = "cho ai"
        else:
            recipient_part = f"cho {draft.recipient.display_name}"  # type: ignore[union-attr]

        return f"Bạn muốn chuyển {amount_part} {recipient_part}?"

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


def _common_short_name(candidates: list[Contact]) -> Optional[str]:
    """If all candidates share the same last token of their display name
    (e.g. both "Nguyễn Văn Minh" and "Trần Hoàng Minh" → "Minh"), return it
    so the clarifier reads more naturally: "cho Minh nào" instead of
    "cho ai trong: ...". Returns None if no shared short name exists."""
    if not candidates:
        return None
    last_tokens = {c.display_name.split()[-1] for c in candidates}
    if len(last_tokens) == 1:
        return next(iter(last_tokens))
    return None


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

"""Conversation orchestrator: NLU → Context → Safety → Banking.

The brain of Omni — turns a user utterance into an OmniResponse with the
appropriate side-effects (draft creation, history lookup, schedule creation).
"""

from __future__ import annotations

import contextvars
import re
import time
from typing import Optional

# Per-request telemetry bucket. The chat route sets this to {} when the
# caller passes ``?dev=1``; the orchestrator and NLU layer fill it as
# they go. ``None`` means telemetry is OFF — production default.
_telemetry: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "omni_telemetry", default=None
)

# Sidecar bucket for *always-on* metric labels. Distinct from ``_telemetry``
# (which is gated on ``?dev=1``) so the Prometheus counters get the NLU
# source label every request, not just dev-mode ones.
_metric_labels: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "omni_metric_labels", default={}  # noqa: B039 — read-only sentinel; .set() replaces wholesale
)


def begin_telemetry() -> dict:
    """Mark the current async task / request as a telemetry caller.

    Returns the empty dict that will accumulate per-stage measurements.
    Safe to call multiple times — overwrites any prior bucket.
    """
    bucket: dict = {}
    _telemetry.set(bucket)
    return bucket


def get_telemetry() -> Optional[dict]:
    """Return the telemetry bucket for the current task, or None."""
    return _telemetry.get()


def end_telemetry() -> None:
    _telemetry.set(None)

import threading as _th

from ..banking.budgets import compute_statuses, label_for
from ..banking.recurring import detect_recurring
from ..banking.service import create_schedule, get_balance, get_history, next_run_for
from ..context import resolve_recipient, resolve_temporal_reference, session_for
from ..context.alias import filter_by_account_hint
from ..ml.amount_predictor import predict_amount
from ..ml.categorizer import categorize as categorize_description
from ..models.schemas import (
    Budget,
    BudgetDraft,
    Contact,
    ContactDraft,
    GoalDraft,
    NLUResult,
    OmniResponse,
    SafetyFlag,
    SavingsGoal,
    ScheduleDraft,
    TransactionDraft,
)
from ..nlp.amount import format_vnd
from ..nlp.entities import normalize_alias
from ..nlp.llm import llm_phrase
from ..nlp.pipeline import understand
from ..safety.rules import evaluate, is_blocked, requires_step_up
from ..store import get_store, new_id, now

# Insights chat handler — lives in a sibling module so the dispatch site
# here is the only thing that needs touching. Keeps the long handler body
# out of the way of in-flight merges to this file.
from .insights_handler import handle_insights as _handle_insights
from .goal_status_handler import handle_goal_status as _handle_goal_status

# In-memory budget / goal draft stash. We deliberately don't push these
# through the session backend (Redis-compatible) for two reasons:
#   1. They never require OTP step-up — the safety contract is enforced
#      by re-validating the limit/target at confirm time, not by a code.
#   2. They're confirmed by clicking a card the user just saw, so a
#      cross-process retrieval path is overkill for this MVP.
# Falls back to "draft not found" if a user confirms after a restart,
# which is the same UX the schedule draft has after its TTL expires.
_budget_drafts: dict[str, BudgetDraft] = {}
_goal_drafts: dict[str, GoalDraft] = {}
_drafts_lock = _th.Lock()


def _stash_budget_draft(user_id: str, draft: BudgetDraft) -> None:
    with _drafts_lock:
        _budget_drafts[user_id] = draft


def _pop_budget_draft(user_id: str, draft_id: str) -> Optional[BudgetDraft]:
    with _drafts_lock:
        d = _budget_drafts.get(user_id)
        if d and d.id == draft_id:
            return _budget_drafts.pop(user_id)
    return None


def _peek_budget_draft(user_id: str) -> Optional[BudgetDraft]:
    with _drafts_lock:
        return _budget_drafts.get(user_id)


def _stash_goal_draft(user_id: str, draft: GoalDraft) -> None:
    with _drafts_lock:
        _goal_drafts[user_id] = draft


def _pop_goal_draft(user_id: str, draft_id: str) -> Optional[GoalDraft]:
    with _drafts_lock:
        d = _goal_drafts.get(user_id)
        if d and d.id == draft_id:
            return _goal_drafts.pop(user_id)
    return None


def _peek_goal_draft(user_id: str) -> Optional[GoalDraft]:
    with _drafts_lock:
        return _goal_drafts.get(user_id)

_CONFIRM_RE = re.compile(
    # Plain confirmation tokens — must occur at message start.
    r"^(?:xac nhan|xacnhan|ok|đồng ý|dong y|y|yes|confirm|duyệt|duyet)\b"
    r"|^xác nhận"
    # "lưu" / "luu" alone OR followed by a confirming particle. CRITICAL:
    # bare "lưu <Name>" is the add-contact verb, NOT a confirm. So we
    # only treat it as confirm when it stands alone or pairs with a
    # continuation cue like "lại / đi / giúp / nha / nhé / cho".
    r"|^(?:lưu|luu)\s*[!.?]?\s*$"
    r"|^(?:lưu|luu)\s+(?:lại|lai|đi|di|giúp|giup|cho|nha|nhe|nhé)\b",
    re.IGNORECASE,
)
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


# Structured help — same content as ``_HELP_TEXT`` but addressable by
# the frontend ``<HelpCard />`` renderer. Each section is intent-aligned
# so the SkillsCard sidebar widget and the /help card share one source
# of truth (see ``frontend/src/components/SkillsCard.tsx`` /
# ``HelpCard.tsx``). Keep ``label`` short — used as the chip text — and
# ``example`` the actual phrasing the user types.
_HELP_SECTIONS: list[dict] = [
    {
        "id": "transfer",
        "title": "Chuyển tiền",
        "items": [
            {"label": "Chuyển nhanh", "example": "chuyển mẹ 2tr"},
            {"label": "Có nội dung", "example": "gửi tiền ăn cho An 500k"},
            {"label": "Slash command", "example": "/transfer Nam 1tr"},
        ],
    },
    {
        "id": "query",
        "title": "Truy vấn",
        "items": [
            {"label": "Chi tiêu tháng", "example": "tháng trước tiêu bao nhiêu"},
            {"label": "Top người nhận", "example": "ai nhận nhiều nhất"},
            {"label": "Số dư", "example": "/balance"},
        ],
    },
    {
        "id": "recurring",
        "title": "Định kỳ",
        "items": [
            {"label": "Đặt lịch", "example": "đặt lịch chuyển mẹ 2tr mùng 1"},
            {"label": "Tìm khoản đều", "example": "có khoản nào trả đều"},
        ],
    },
    {
        "id": "budget",
        "title": "Ngân sách",
        "items": [
            {"label": "Đặt ngân sách", "example": "đặt ngân sách ăn uống 3tr"},
            {"label": "Còn lại", "example": "tháng này còn bao nhiêu cho ăn uống"},
        ],
    },
    {
        "id": "tools",
        "title": "Công cụ",
        "items": [
            {"label": "Trợ giúp", "example": "/help"},
            {"label": "ATM gần nhất", "example": "ATM gần nhất"},
            {"label": "Lưu danh bạ", "example": "Lưu Nam STK 0123 MB Bank"},
        ],
    },
]

_HELP_SHORTCUTS: list[dict] = [
    {"keys": "Cmd/Ctrl+K", "label": "Focus ô nhập"},
    {"keys": "Cmd/Ctrl+/", "label": "Mở slash menu"},
    {"keys": "Cmd/Ctrl+Enter", "label": "Gửi lại tin nhắn vừa rồi"},
    {"keys": "Esc", "label": "Đóng popup"},
    {"keys": "↑", "label": "Lịch sử tin nhắn"},
    {"keys": "@", "label": "Gợi ý danh bạ"},
]


def help_sections_payload() -> list[dict]:
    """Public copy of the structured help payload — used by tests and by
    the orchestrator when composing the OmniResponse. We return a new
    list each call so callers cannot accidentally mutate module state."""
    sections: list[dict] = [dict(s, items=list(s["items"])) for s in _HELP_SECTIONS]
    sections.append({"id": "shortcuts", "title": "Phím tắt", "shortcuts": list(_HELP_SHORTCUTS)})
    return sections


def _help_response() -> OmniResponse:
    """Structured help message — surfaced by the /help slash command and by
    typed help requests. Returns a smalltalk intent so the UI renders it
    without trying to attach a draft/balance/history payload."""
    return OmniResponse(
        intent="smalltalk",
        text=_HELP_TEXT,
        help_sections=help_sections_payload(),
    )


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
    overall_t0 = time.perf_counter()
    # Capture the intent label even when the inner call raises — Prometheus
    # otherwise loses count of error-path latency. ``"error"`` is the
    # sentinel used for both unexpected exceptions and missing intents.
    intent_label = "error"
    try:
        resp = _handle_message_inner(user_id, text)
        intent_label = resp.intent or "unknown"
    finally:
        # Best-effort metric recording. The try/except inside .inc()/.observe()
        # already swallows errors, but we wrap again for defence in depth so
        # an unimported metric module (eg. partial install) can't break chat.
        try:
            from . import metrics as _m

            elapsed = time.perf_counter() - overall_t0
            _m.chat_latency_seconds.observe(elapsed, intent=intent_label)
        except Exception:
            pass
    # Notify the demo recorder if a recording is active. Imported lazily
    # to avoid a routes ↔ orchestrator cycle and to keep the production
    # hot path free of an extra import when recorder is unused.
    try:
        from ..routes.demo import record_turn

        record_turn(user_id, text, resp)
    except Exception:  # pragma: no cover — recorder must never break chat
        pass
    # Record the chat-requests counter once we know both intent and source.
    # ``nlu.source`` lives in the telemetry bucket when ``?dev=1`` was set;
    # otherwise we read it back off the bucket if the orchestrator set it
    # internally.
    try:
        from . import metrics as _m

        labels = _metric_labels.get() or {}
        source = labels.get("nlu_source") or "unknown"
        _m.chat_requests_total.inc(intent=intent_label, source=source)
        # Reset for the next request on this task.
        _metric_labels.set({})
    except Exception:
        pass
    # Attach telemetry only when the request context opted in.
    bucket = get_telemetry()
    if bucket is not None:
        bucket["total_latency_ms"] = int(
            (time.perf_counter() - overall_t0) * 1000
        )
        # Count safety flags on whatever draft came back.
        flags = []
        if resp.draft is not None:
            flags = resp.draft.flags
        elif resp.contact_draft is not None:
            flags = resp.contact_draft.flags
        elif resp.schedule_draft is not None:
            flags = resp.schedule_draft.flags
        bucket["safety_flags"] = len(flags)
        bucket["safety_codes"] = [f.code for f in flags]
        resp.telemetry = bucket
    return resp


def _handle_message_inner(user_id: str, text: str) -> OmniResponse:
    t0 = time.perf_counter()  # noqa: F841 — kept for ad-hoc latency probes
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

    # Budget / goal draft continuation. Both confirm flows are simple
    # (no OTP); the cancel / xác nhận shortcut applies just like contact.
    bd = _peek_budget_draft(user_id)
    if bd is not None:
        cont = _try_continue_budget_draft(user_id, text, bd)
        if cont is not None:
            session.append("user", text)
            session.append("omni", cont.text)
            return cont

    gd = _peek_goal_draft(user_id)
    if gd is not None:
        cont = _try_continue_goal_draft(user_id, text, gd)
        if cont is not None:
            session.append("user", text)
            session.append("omni", cont.text)
            return cont

    nlu_t0 = time.perf_counter()
    nlu = understand(text, history=history_msgs)
    nlu_ms = int((time.perf_counter() - nlu_t0) * 1000)
    bucket = get_telemetry()
    if bucket is not None:
        bucket["nlu_latency_ms"] = nlu_ms
        bucket["nlu_source"] = nlu.source
        bucket["intent"] = nlu.intent
        bucket["intent_confidence"] = nlu.confidence
    # Always-on metric labels — used by handle_message's Prometheus
    # exposition path even when the dev-only ``_telemetry`` bucket is None.
    try:  # noqa: SIM105 — explicit try/except keeps the failure site obvious
        _metric_labels.set({"nlu_source": nlu.source})
    except Exception:
        pass

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

    if nlu.intent == "insights":
        return _handle_insights(user_id, nlu, history_msgs)

    if nlu.intent == "add_contact":
        return _handle_add_contact(user_id, nlu)

    if nlu.intent == "set_budget":
        return _handle_set_budget(user_id, nlu)

    if nlu.intent == "set_goal":
        return _handle_set_goal(user_id, nlu)

    if nlu.intent == "budget_status":
        return _handle_budget_status(user_id, nlu)

    if nlu.intent == "goal_status":
        return _handle_goal_status(user_id, nlu, history_msgs)

    if nlu.intent == "transfer":
        return _handle_transfer(user_id, nlu)

    if nlu.intent == "atm_finder":
        return _handle_atm_finder(user_id, nlu)

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
        # User just supplied an explicit amount — it's no longer predicted.
        draft.predicted_amount = False
    if e.description:
        draft.description = e.description
        # Description changed — re-categorise.
        cat, conf = categorize_description(e.description)
        draft.category = cat if (cat != "other" and conf >= 0.5) else None

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
        user_id=user_id,
        contacts=get_store().contacts_of(user_id),
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


def _handle_atm_finder(user_id: str, nlu: NLUResult) -> OmniResponse:
    """ATM / branch finder.

    The chat path doesn't have access to the user's coordinates — the
    browser does. So this handler returns the full seed list (optionally
    filtered by bank) and a static prompt; the frontend then triggers
    ``navigator.geolocation`` and hits ``/api/atm/nearby`` to re-rank by
    real distance.
    """
    from ..banking.atm import find_by_bank, load_atms  # local import to avoid cycles

    bank = nlu.entities.atm_bank
    rows = find_by_bank(bank) if bank else load_atms()
    # Cap the chat-side preview at 5 — the frontend full list comes from
    # the dedicated endpoint once it has coords.
    preview = list(rows[:5])
    if bank:
        if rows:
            text = (
                f"Mình tìm thấy {len(rows)} điểm ATM/chi nhánh {bank}. "
                "Bạn cho phép Omni xem vị trí để xếp theo khoảng cách nhé."
            )
        else:
            text = (
                f"Mình chưa có dữ liệu ATM cho ngân hàng \"{bank}\" trong demo. "
                "Bạn thử ngân hàng khác xem sao?"
            )
    else:
        text = (
            "Omni đang gợi ý các điểm ATM trong dữ liệu mẫu. "
            "Bạn cho phép truy cập vị trí để xem cây nào gần bạn nhất nhé."
        )
    return OmniResponse(intent="atm_finder", text=text, atms=preview)


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
    txs = store.transactions_of(user_id)
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


# ---------------------------------------------------------------------------
# Budget envelope handlers
# ---------------------------------------------------------------------------


def _handle_set_budget(user_id: str, nlu: NLUResult) -> OmniResponse:
    """Stage a monthly budget draft. The user confirms (or cancels) via
    the budget card or by typing "xác nhận" / "huỷ" in chat."""
    e = nlu.entities
    if not e.budget_category:
        return OmniResponse(
            intent="set_budget",
            text=(
                "Bạn muốn đặt ngân sách cho hạng mục nào? Ví dụ: "
                "\"đặt ngân sách ăn uống 3 triệu\"."
            ),
        )
    if e.amount is None or e.amount <= 0:
        return OmniResponse(
            intent="set_budget",
            text=(
                f"Bạn muốn đặt hạn mức bao nhiêu cho {label_for(e.budget_category)} "
                "mỗi tháng?"
            ),
        )

    store = get_store()
    existing = store.get_budget_by_category(user_id, e.budget_category)
    draft = BudgetDraft(
        id=new_id("bd"),
        category=e.budget_category,
        category_label=label_for(e.budget_category),
        monthly_limit_vnd=e.amount,
        replaces_existing=existing is not None,
    )
    _stash_budget_draft(user_id, draft)

    verb = "cập nhật" if existing else "đặt"
    text = (
        f"Mình sẽ {verb} ngân sách {draft.category_label} là "
        f"{format_vnd(draft.monthly_limit_vnd)} mỗi tháng. Xác nhận giúp mình nhé."
    )
    return OmniResponse(intent="set_budget", text=text, budget_draft=draft)


def confirm_budget_draft(user_id: str, draft_id: str) -> OmniResponse:
    session = session_for(user_id)
    draft = _pop_budget_draft(user_id, draft_id)
    if draft is None:
        return OmniResponse(
            intent="unknown", text="Không tìm thấy ngân sách chờ xác nhận."
        )
    budget = Budget(
        id=new_id("b"),
        user_id=user_id,
        category=draft.category,
        monthly_limit_vnd=draft.monthly_limit_vnd,
        created_at=now(),
    )
    saved = get_store().add_budget(budget)
    text = (
        f"Đã lưu ngân sách {draft.category_label}: "
        f"{format_vnd(saved.monthly_limit_vnd)} mỗi tháng. Mình sẽ "
        "nhắc khi bạn sắp chạm hạn mức."
    )
    session.append("user", "Xác nhận ngân sách")
    session.append("omni", text)
    return OmniResponse(intent="set_budget", text=text)


def cancel_budget_draft(user_id: str, draft_id: str) -> OmniResponse:
    session = session_for(user_id)
    _pop_budget_draft(user_id, draft_id)
    text = "Đã huỷ đặt ngân sách."
    session.append("user", "Huỷ ngân sách")
    session.append("omni", text)
    return OmniResponse(intent="set_budget", text=text)


def _handle_budget_status(user_id: str, nlu: NLUResult) -> OmniResponse:
    """Read-only: this month's spending vs each budget."""
    e = nlu.entities
    statuses = compute_statuses(user_id)
    if not statuses:
        return OmniResponse(
            intent="budget_status",
            text=(
                "Bạn chưa đặt ngân sách nào. Thử nói \"đặt ngân sách ăn uống "
                "3 triệu\" để bắt đầu nhé."
            ),
        )

    if e.budget_category:
        match = next((s for s in statuses if s.category == e.budget_category), None)
        if match is None:
            return OmniResponse(
                intent="budget_status",
                text=(
                    f"Bạn chưa đặt ngân sách cho {label_for(e.budget_category)}. "
                    "Mình có thể đặt giúp — bạn muốn hạn mức bao nhiêu?"
                ),
                budget_statuses=statuses,
            )
        remaining = max(match.remaining_vnd, 0)
        if match.ratio >= 1.0:
            over = match.spent_vnd - match.monthly_limit_vnd
            body = (
                f"Tháng này {match.category_label} đã vượt ngân sách "
                f"{format_vnd(over)} (đã tiêu {format_vnd(match.spent_vnd)} / "
                f"{format_vnd(match.monthly_limit_vnd)})."
            )
        else:
            body = (
                f"{match.category_label}: còn {format_vnd(remaining)} "
                f"trong ngân sách (đã tiêu {format_vnd(match.spent_vnd)} / "
                f"{format_vnd(match.monthly_limit_vnd)})."
            )
        return OmniResponse(
            intent="budget_status", text=body, budget_statuses=[match]
        )

    # No category specified — summarise everything.
    lines = []
    for s in statuses:
        remaining = max(s.remaining_vnd, 0)
        if s.ratio >= 1.0:
            tag = "VƯỢT"
        elif s.ratio >= 0.8:
            tag = "Sắp hết"
        else:
            tag = "Ổn"
        lines.append(
            f"• {s.category_label}: {format_vnd(s.spent_vnd)}/"
            f"{format_vnd(s.monthly_limit_vnd)} ({tag}, còn {format_vnd(remaining)})"
        )
    body = "Tình trạng ngân sách tháng này:\n" + "\n".join(lines)
    return OmniResponse(intent="budget_status", text=body, budget_statuses=statuses)


# ---------------------------------------------------------------------------
# Savings goal handlers
# ---------------------------------------------------------------------------


def _handle_set_goal(user_id: str, nlu: NLUResult) -> OmniResponse:
    e = nlu.entities
    name = (e.goal_name or "").strip() or "Mục tiêu mới"
    if e.amount is None or e.amount <= 0:
        return OmniResponse(
            intent="set_goal",
            text=(
                f"Bạn muốn tiết kiệm bao nhiêu cho \"{name}\"? "
                "Ví dụ: \"mục tiêu Tết 50 triệu\"."
            ),
        )
    draft = GoalDraft(
        id=new_id("gd"),
        name=name,
        target_vnd=e.amount,
    )
    _stash_goal_draft(user_id, draft)
    text = (
        f"Mình sẽ tạo mục tiêu tiết kiệm \"{draft.name}\" với "
        f"{format_vnd(draft.target_vnd)}. Xác nhận giúp mình nhé."
    )
    return OmniResponse(intent="set_goal", text=text, goal_draft=draft)


def confirm_goal_draft(user_id: str, draft_id: str) -> OmniResponse:
    session = session_for(user_id)
    draft = _pop_goal_draft(user_id, draft_id)
    if draft is None:
        return OmniResponse(
            intent="unknown", text="Không tìm thấy mục tiêu chờ xác nhận."
        )
    goal = SavingsGoal(
        id=new_id("g"),
        user_id=user_id,
        name=draft.name,
        target_vnd=draft.target_vnd,
        current_vnd=0,
        deadline=draft.deadline,
        created_at=now(),
    )
    saved = get_store().add_goal(goal)
    text = (
        f"Đã tạo mục tiêu \"{saved.name}\": {format_vnd(saved.target_vnd)}. "
        "Mỗi lần chuyển khoản, bạn có thể chia một phần sang mục tiêu này."
    )
    session.append("user", "Xác nhận mục tiêu")
    session.append("omni", text)
    return OmniResponse(intent="set_goal", text=text)


def cancel_goal_draft(user_id: str, draft_id: str) -> OmniResponse:
    session = session_for(user_id)
    _pop_goal_draft(user_id, draft_id)
    text = "Đã huỷ mục tiêu."
    session.append("user", "Huỷ mục tiêu")
    session.append("omni", text)
    return OmniResponse(intent="set_goal", text=text)


def _try_continue_budget_draft(
    user_id: str, text: str, draft: BudgetDraft
) -> Optional[OmniResponse]:
    if _is_cancel(text):
        return cancel_budget_draft(user_id, draft.id)
    if _is_confirm(text):
        return confirm_budget_draft(user_id, draft.id)
    return None


def _try_continue_goal_draft(
    user_id: str, text: str, draft: GoalDraft
) -> Optional[OmniResponse]:
    if _is_cancel(text):
        return cancel_goal_draft(user_id, draft.id)
    if _is_confirm(text):
        return confirm_goal_draft(user_id, draft.id)
    return None


def _handle_transfer(user_id: str, nlu: NLUResult) -> OmniResponse:
    store = get_store()
    contacts = store.contacts_of(user_id)
    txs = store.transactions_of(user_id)
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
        user_id=user_id,
        contacts=get_store().contacts_of(user_id),
    )

    # Auto-categorise from the description (or, when blank, the raw
    # user utterance) so the UI can render a category chip and the saved
    # transaction is grouped correctly in history. Confidence floor of
    # 0.5 keeps weak TF-IDF guesses from polluting the dataset.
    category: Optional[str] = None
    cat_source = description or nlu.raw_text
    if cat_source:
        cat, conf = categorize_description(cat_source)
        if cat != "other" and conf >= 0.5:
            category = cat

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
        category=category,
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
        user_id=user_id,
        contacts=get_store().contacts_of(user_id),
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
            category=draft.category,
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

    # Score the A/B arm that produced the last suggestion list for this
    # user. ``correct = (chosen contact == top-1 we suggested)``. Never
    # blocks the confirmed transfer.
    try:
        from .suggester import consume_outcome

        consume_outcome(user_id, draft.recipient.id)  # type: ignore[union-attr]
    except Exception:
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
        user_id=user_id,
        contacts=get_store().contacts_of(user_id),
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
    "confirm_budget_draft",
    "cancel_budget_draft",
    "confirm_goal_draft",
    "cancel_goal_draft",
]

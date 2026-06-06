"""Conversation orchestrator: NLU → Context → Safety → Banking.

The brain of Omni — turns a user utterance into an OmniResponse with the
appropriate side-effects (draft creation, history lookup, schedule creation).
"""

from __future__ import annotations

import contextvars
import re
import time
from typing import Any, Optional

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
from ..nlp.llm import llm_draft_action, llm_phrase
from ..nlp.pipeline import understand
from ..safety.rules import auth_policy, evaluate, is_blocked, requires_step_up
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
# Per-user pending split-bill queue. When a user invokes split, the first
# draft becomes active in the session; the rest sit here until each
# confirm pops the next. Wiped on session reset or successful drain.
_split_queues: dict[str, list[TransactionDraft]] = {}
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
    # Plain confirmation tokens — must occur at message start. Expanded
    # to include polite forms (dạ / vâng), informal acks (ờ / ờ ơ), and
    # the slangy ok-variants judges actually type (okela / okie / oce).
    r"^(?:xac nhan|xacnhan|ok|okay|oki|okie|okela|oce|okê|oke|"
    r"đồng ý|dong y|y|yes|confirm|duyệt|duyet|"
    r"ừ|ừm|ư|um|uh|ờ|ờm|"
    r"dạ|da|vâng|vang|"
    r"chuẩn|chuan|"
    # Additional VN confirmations judges actually type:
    # "tất nhiên" (of course), "chắc chắn" (definitely), bare "có"
    # (yes). The bare "có" must be word-bounded so it doesn't match
    # "có gì" / "có thể" (question/modal continuations).
    r"tất nhiên|tat nhien|chắc chắn|chac chan)\b"
    r"|^xác nhận"
    # Bare "có" alone — must not be followed by question / modal words
    # ("có thể", "có gì", "có không", "có nên"). Bare "có" with
    # optional punctuation is a confirm.
    r"|^có(?!\s+(?:thể|the|gì|gi|nên|nen|không|khong|ko|sao|chuyện|chuyen|ai|cách|cach))\b"
    # "đúng" / "phải" — "right/correct/yes" confirm, but NOT when
    # followed by an action / question verb that would make the
    # sentence a question. "đúng" / "phải làm gì" must NOT route to
    # confirm.
    r"|^(?:đúng|dung|phải|phai)(?!\s+(?:làm|lam|đi|di|về|ve|đến|den|nào|nao|gì|gi|không|khong|ko))\b"
    # "được" / "duoc" alone or with a continuation particle ("được rồi",
    # "được luôn", "được nha"). The plain word means "OK / fine" in
    # Vietnamese — judges use it constantly.
    r"|^(?:được|duoc)\s*[!.?]?\s*$"
    r"|^(?:được|duoc)\s+(?:rồi|roi|luôn|luon|nha|nhe|nhé|đó|do|đấy|day|chứ|chu)\b"
    # "lưu" / "luu" alone OR followed by a confirming particle. CRITICAL:
    # bare "lưu <Name>" is the add-contact verb, NOT a confirm. So we
    # only treat it as confirm when it stands alone or pairs with a
    # continuation cue like "lại / đi / giúp / nha / nhé / cho".
    r"|^(?:lưu|luu)\s*[!.?]?\s*$"
    r"|^(?:lưu|luu)\s+(?:lại|lai|đi|di|giúp|giup|cho|nha|nhe|nhé)\b",
    re.IGNORECASE,
)
# "thôi" / "đừng" / "khoan" are everyday cancel particles missing from
# the original list. ``khong`` already covered "không, …" via word-boundary;
# the leading punctuation case ("không, huỷ đi") is matched too because
# ``\b`` succeeds before the comma.
#
# CRITICAL guards (round 6): "không thay đổi gì cả" / "không có gì thay
# đổi" mean "no change, proceed" — they were silently cancelling valid
# draft confirms. "thôi cứ thế đi" / "thôi vậy đi" mean "just go with
# it" — same trap. Negative lookahead blocks these phrases from the
# cancel routing while keeping bare "không" / "thôi" as cancel.
_CANCEL_RE = re.compile(
    r"^(?:"
    r"huỷ|huy|cancel|hủy|no|stop|bỏ|bo|đừng|dung|khoan|"
    # "không" — cancel UNLESS followed by "thay đổi" / "có gì" / "vấn đề" /
    # "ổn" / "sao" / "phải" / "ai" / "việc gì" → those are reassurances,
    # not cancellations. ALSO: "không, X" where X is anything that
    # ISN'T itself a cancel word ("không, bố" / "không, 5tr cho mẹ") is
    # a CORRECTION not a cancel — negative lookahead blocks the cancel
    # match so the pivot routes via slot-fill. But "không, huỷ đi" /
    # "không, bỏ" still cancel because the follow-up IS a cancel word.
    r"không(?!\s*,\s*(?!huỷ|huy|hủy|cancel|bỏ|bo|đừng|dung|thôi|thoi|stop)\S)(?!\s+(?:thay\s+đổi|thay\s+doi|có\s+gì|co\s+gi|vấn\s+đề|van\s+de|ổn|on|sao|phải|phai|ai|việc\s+gì|viec\s+gi))|"
    r"khong(?!\s*,\s*(?!huy|huỷ|hủy|cancel|bỏ|bo|đừng|dung|thôi|thoi|stop)\S)(?!\s+(?:thay\s+doi|co\s+gi|van\s+de|on|sao|phai|ai|viec\s+gi))|"
    # "thôi" — cancel UNLESS followed by "cứ thế" / "vậy đi" / "ok" /
    # "thế" → those mean "just go with it", not cancel.
    r"thôi(?!\s+(?:cứ|cu|vậy|vay|thế|the|ok|được|duoc))|"
    r"thoi(?!\s+(?:cu|vay|the|ok|duoc))"
    r")\b",
    re.IGNORECASE,
)
_OTP_RE = re.compile(r"^\s*(\d{4,6})\s*$")
_HELP_RE = re.compile(
    # Exact-phrase commands (preserved from origin, plus optional ?!.).
    r"^\s*(?:/help|help|trợ\s+giúp|tro\s+giup|hướng\s+dẫn|huong\s+dan|menu)\s*[?!.]*\s*$"
    # "How do I / how to do" — judges asking how to use the assistant.
    # Substring-safe in a banking app: "làm sao" only appears in
    # help-shaped questions, never in a transfer / history command.
    r"|\b(?:làm|lam)\s+(?:sao|thế\s+nào|the\s+nao|cách\s+nào|cach\s+nao)\b"
    # "What can you do" — "omni làm gì", "bạn có thể làm gì", "có thể
    # làm gì". The "<subject> + làm gì" shape is unambiguous.
    # Allows "biết làm gì" / "có thể làm gì" / "làm được gì" — anything
    # between the subject and the final "gì" that's still a help shape.
    r"|\b(?:omni|bạn|ban)\s+(?:có\s+thể\s+|co\s+the\s+)?(?:biết\s+|biet\s+)?(?:làm|lam)(?:\s+(?:được|duoc))?\s+gì\b"
    r"|\b(?:omni|bạn|ban)\s+(?:biết|biet)\s+gì\b"
    r"|\b(?:có\s+thể|co\s+the)\s+(?:làm|lam)\s+gì\b"
    # "Guide / instructions / how to use".
    r"|\b(?:hướng\s+dẫn|huong\s+dan)\b"
    r"|\b(?:cách|cach)\s+(?:dùng|dung|sử\s+dụng|su\s+dung)\b"
    # Bare help asks — anchored to avoid eating "giúp mình kiểm tra số
    # dư" (which is a balance query with a polite prefix).
    r"|^\s*giúp\s+(?:mình|tôi|minh|toi)?\s*(?:với|voi|ơi|oi)?\s*[?!.]*\s*$"
    r"|^\s*giúp\s+(?:đỡ|do)\s*[?!.]*\s*$"
    r"|^\s*help\s+me\s*[?!.]*\s*$",
    re.IGNORECASE,
)


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
    # Single-day windows. "hôm qua" used to fall into recent_30d (last
    # 30 days), which silently broadened "tôi tiêu gì hôm qua" into a
    # month total. Scope it to yesterday.
    if "hom nay" in folded:
        return "today"
    if "hom qua" in folded:
        return "yesterday"
    if "tuan nay" in folded:
        return "this_week"
    if "tuan truoc" in folded:
        return "last_week"
    if "nam nay" in folded:
        return "this_year"
    if "nam ngoai" in folded:
        return "last_year"
    if "vua roi" in folded:
        return "recent_30d"
    return "this_month"


from collections import OrderedDict as _OrderedDict
_USER_LOCKS_MAX = 2048
_user_locks: "_OrderedDict[str, _th.Lock]" = _OrderedDict()
_user_locks_lock = _th.Lock()


def _user_lock(user_id: str) -> "_th.Lock":
    """Per-user mutex for the chat turn — serialises read-modify-write of
    the session draft so two rapid-fire requests from the same user
    can't race. Round-9 stress reproduced wrong-recipient + wrong-amount
    OTP prompts when /api/chat fired ~250ms apart: request A wrote a
    draft, request B's NLU classify hit before A's draft write landed,
    and B picked up a stale draft from a prior turn.

    Backed by an ``OrderedDict`` LRU bounded at ``_USER_LOCKS_MAX`` so
    the process doesn't accumulate one lock per unique user_id
    forever. Eviction risk: a tx-in-flight user whose lock is evicted
    would race the next turn — but the eviction policy LRU-touches the
    user on every access, so any user with current activity stays in
    the front. Only stale users get evicted.
    """
    with _user_locks_lock:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = _th.Lock()
            _user_locks[user_id] = lock
            if len(_user_locks) > _USER_LOCKS_MAX:
                _user_locks.popitem(last=False)
        else:
            # Touch — move to back so this user stays "fresh" for the LRU.
            _user_locks.move_to_end(user_id)
        return lock


def handle_message(user_id: str, text: str) -> OmniResponse:
    overall_t0 = time.perf_counter()
    # Capture the intent label even when the inner call raises — Prometheus
    # otherwise loses count of error-path latency. ``"error"`` is the
    # sentinel used for both unexpected exceptions and missing intents.
    intent_label = "error"
    try:
        with _user_lock(user_id):
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


def _handle_message_inner(
    user_id: str, text: str, session_id: Optional[str] = None
) -> OmniResponse:
    t0 = time.perf_counter()  # noqa: F841 — kept for ad-hoc latency probes
    text = text.strip()
    session = session_for(user_id)
    # Conversation context for the NLU + phrasing layers. When the request
    # carries a durable conversation id, source it from the permanent
    # archive (chat_log) scoped to that conversation: this is what the user
    # actually sees on screen, survives reloads / restarts / TTL expiry,
    # and never bleeds across conversations. The chat route appends the
    # current turn to chat_log *after* we return, so what we read here is
    # exactly the prior turns. Fall back to the ephemeral, user-scoped
    # session history for clients that don't pass a session id (WebSocket,
    # scripts, tests).
    if session_id:
        from ..db import chat_log

        history_msgs = chat_log.recent_messages(session_id, user_id)
    else:
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

    # Direct continuation paths — for any in-flight transfer draft the user
    # is reviewing, the LLM decides what they want (confirm / cancel / edit /
    # redirect / relative-amount), with the deterministic rules as the
    # offline fallback. Returns None only when the message isn't about the
    # draft (e.g. "số dư bao nhiêu?"), in which case we continue to the
    # normal NLU dispatch below.
    if session.current_draft is not None:
        cont = _continue_draft_llm_first(
            user_id, text, session.current_draft, history_msgs
        )
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

    # CRITICAL OTP-LOCK guard. When the draft is awaiting OTP, the only
    # legitimate text turns are: numeric OTP code, cancel ("huỷ"), or
    # confirm-repeat ("xác nhận lại"). A round-9 stress reproduced an
    # exploit: after "chuyển mẹ 50tr → ok" (awaiting_otp=True), typing
    # "bố" silently swapped the recipient to Lê Văn Hùng while keeping
    # awaiting_otp=True and the same draft_id — user's OTP went toward
    # a transfer to bố instead of mẹ. Closes by refusing ANY modify-
    # path mutation on an awaiting-OTP draft; the user is told to
    # cancel and restart if they want a different recipient/amount.
    if (
        session.current_draft is not None
        and session.current_draft.awaiting_otp
        and _OTP_RE.match(text) is None
        and not _is_confirm(text)
        and not _is_cancel(text)
    ):
        return OmniResponse(
            intent="transfer",
            text=(
                "Giao dịch đang chờ xác minh OTP. Bạn nhập mã OTP để hoàn tất, "
                "hoặc gõ \"huỷ\" để bắt đầu lại nếu muốn đổi người nhận / số tiền."
            ),
            draft=session.current_draft,
        )

    # Fresh-transfer guard: if the user types a NEW verb-led command
    # AND provides BOTH the recipient and the amount, treat it as a
    # truly fresh request and wipe the previous draft. Pre-fix, the
    # wipe fired on any "chuyển …" prefix, so the user editing one
    # slot mid-flow ("chuyển cho Y" while draft sits at (X, 1tr)) lost
    # the other slot (amount evaporated to None) — the bug reported by
    # the user as "cứ đổi người là quên mất số tiền".
    #
    # Single-slot updates now fall through to ``_looks_like_modification``
    # below, which mutates the existing draft in place. The original
    # bug this guard was added for (stale 2tr leaking after a finished
    # turn) only manifests when the previous turn never confirmed nor
    # cancelled — covered separately by the missing_recipient flag the
    # safety layer raises on the modified draft.
    e = nlu.entities
    provides_both_slots = e.amount is not None and bool(e.recipient_text)
    prev_draft_complete = (
        session.current_draft is not None
        and session.current_draft.recipient is not None
        and session.current_draft.amount is not None
    )
    # Wipe on a fresh verb-led command in either of two cases:
    #   1. The new message provides BOTH recipient and amount — truly
    #      a brand-new request, anything carried over from the prior
    #      draft would be a stale leak.
    #   2. The previous draft was incomplete (missing recipient OR
    #      missing amount). The user is restarting after an unfinished
    #      slot-prompt — e.g. "chuyển 2tr" (no recipient) then "chuyển
    #      tiền cho t". Inheriting the abandoned 2tr would surface as
    #      "Bạn muốn chuyển 2tr cho t?" which the user never asked for.
    #
    # Otherwise (single-slot edit on a complete previous draft) fall
    # through to the modify path — the user is swapping one slot and
    # expects the other to carry over. That's the "đổi người là quên
    # mất số tiền" scenario from user feedback.
    if (
        session.current_draft is not None
        and _looks_like_fresh_transfer_command(text)
        and (provides_both_slots or not prev_draft_complete)
    ):
        session.clear_draft()

    # Follow-up modify path: there's an active draft and the user is
    # editing one of the slots (amount / recipient / description).
    # CRITICAL: don't gate on nlu.intent == "transfer" — judges saying
    # "nội dung là tiền học cho em" against an existing draft get
    # nlu.intent="unknown" because the description anchor is the only
    # signal. Without this broadening they fell to the guess-correction
    # page mid-flow. _looks_like_modification still gates so non-edit
    # messages don't accidentally route here.
    if (
        session.current_draft is not None
        and nlu.intent in ("transfer", "unknown", "smalltalk")
        and _looks_like_modification(nlu, session.current_draft)
    ):
        resp = _modify_transfer_draft(user_id, session.current_draft, nlu)
        session.append("user", text)
        session.append("omni", resp.text)
        return resp

    # Missing-slot fill OR mid-draft recipient swap: when the assistant
    # just asked "Bạn muốn chuyển X cho ai?" and the user types a short
    # bare name as the next turn ("Nam"), the rule classifier sees a
    # single token / no verb / no digit → intent=unknown → user gets
    # "Mình chưa rõ ý bạn". Slot-fill that.
    #
    # Also fires when the draft DOES have a recipient already but the
    # user types a different bare name — the resolver had matched the
    # user's original surface to an unintended contact (e.g. user typed
    # "abc" → matched seed "Công ty ABC" → confirm card shows ABC) and
    # the user is now naming the real recipient. Without this branch,
    # the bare "Nam" reply fell through to NLU and got "chưa rõ ý"
    # despite an active draft sitting in session — visible
    # "không giữ ngữ cảnh" UX bug.
    if (
        session.current_draft is not None
        and nlu.intent in ("unknown", "transfer", "smalltalk")
        and _looks_like_bare_recipient(text)
    ):
        from ..models.schemas import ExtractedEntities, NLUResult as _NLU

        # Vietnamese prepositions that frequently lead a slot-fill answer
        # ("cho Nam", "tới Nam", "gửi Nam") but aren't part of the actual
        # name. Strip them before feeding to the resolver so the embedding
        # match doesn't latch onto the preposition and return random
        # contacts. _strip_relational handles family prefixes
        # (anh/chị/em/…); these are pure money-flow words.
        import re as _re
        recipient_surface = text
        # Strip leading interjections / fillers / hesitation markers in
        # a loop until no more change — user pivots stack: "à mà bố" /
        # "ờ nhầm bố" / "à không bố" all share this shape. Without the
        # loop a single sub stops after the first match and the
        # resolver sees "mà bố" → 0 → recipient erased on PR #24 swap.
        # "không" / "khong" is a leading discourse particle here only —
        # the bare "không" cancel case was already caught by _is_cancel
        # in the continuation path above; if we reach the slot-fill
        # branch with "không, X" / "à không X", "không" is a negation
        # prefix to the correction ("no, [I meant] X"), not a cancel.
        _LEADING_FILLER_RE = _re.compile(
            r"^\s*(?:à|ờ|ơ|nhầm|nham|mà|ma|ờm|umm?|không|khong)\b[\s,.!]*",
            _re.IGNORECASE,
        )
        for _ in range(5):  # bounded to avoid pathological inputs
            new = _LEADING_FILLER_RE.sub("", recipient_surface).lstrip()
            if new == recipient_surface:
                break
            recipient_surface = new
        # Strip leading money-flow prepositions / verbs.
        recipient_surface = _re.sub(
            r"^\s*(?:cho|tới|toi|đến|den|gửi|gui|sang|qua|đổi\s+sang|doi\s+sang)\s+",
            "",
            recipient_surface,
            flags=_re.IGNORECASE,
        )
        # Strip trailing softener / commit particles. User says
        # "bố thôi" / "bố nhé" / "bố ạ" / "bố chứ" — the trailing token
        # is not part of the name. Same for "bố đi" / "bố nha".
        recipient_surface = _re.sub(
            r"\s+(?:thôi|thoi|nhé|nhe|nha|nhi|ạ|a|chứ|chu|đi|di|đó|do|ơi|oi)\s*[!.?]*\s*$",
            "",
            recipient_surface,
            flags=_re.IGNORECASE,
        )
        recipient_surface = recipient_surface.strip(" ,.;-?!\"'“”‘’:")
        # If stripping took everything (user typed just "cho"), fall back
        # to the original — we'd rather show a clarification than silently
        # turn it into a name lookup of empty string.
        if not recipient_surface:
            recipient_surface = text

        synth = _NLU(
            intent="transfer",
            confidence=0.7,
            entities=ExtractedEntities(recipient_text=recipient_surface),
            raw_text=text,
            source="rule",
        )
        resp = _modify_transfer_draft(user_id, session.current_draft, synth)
        session.append("user", text)
        session.append("omni", resp.text)
        return resp

    # Rule-path context rescue. A bare temporal follow-up after a history
    # turn — "còn tháng trước?", "tháng này thì sao?" — carries no intent
    # keyword, so the (context-blind) rule classifier returns "unknown"
    # whenever the LLM is unavailable / rate-limited. The LLM would have
    # inherited the prior intent + recipient; replicate that here so the
    # assistant still remembers context on the fallback path.
    _rule_history_followup_rescue(nlu, history_msgs)

    resp = _dispatch_intent(user_id, nlu, history_msgs)
    session.append("user", text)
    session.append("omni", resp.text)
    return resp


def _rule_history_followup_rescue(
    nlu: NLUResult, history_msgs: list[dict]
) -> None:
    """Mutate ``nlu`` in place: turn an unknown bare temporal follow-up into
    a history query that inherits the previous turn's recipient.

    Only fires when (a) the NLU came back ``unknown`` (so we never override
    a real classification — including the LLM's), (b) the current message
    actually references a time window, and (c) the immediately prior user
    turn was itself a history query. Conservative by design: misclassifying
    a transfer as history is read-only and harmless, but we still gate on
    all three to avoid surprises."""
    if nlu.intent != "unknown" or not nlu.entities.temporal_reference:
        return
    prev_user = next(
        (m.get("content", "") for m in reversed(history_msgs) if m.get("role") == "user"),
        None,
    )
    if not prev_user:
        return
    from ..nlp.entities import extract as _extract
    from ..nlp.intent import classify as _classify

    prev_intent, _ = _classify(prev_user)
    if prev_intent != "history":
        return
    nlu.intent = "history"
    if not nlu.entities.recipient_text:
        prev_recipient = _extract(prev_user).recipient_text
        if prev_recipient:
            nlu.entities.recipient_text = prev_recipient


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

    if nlu.intent == "my_account":
        return _handle_my_account(user_id)

    if nlu.intent == "receive_qr":
        return _handle_receive_qr(user_id, nlu)

    if nlu.intent == "recap":
        return _handle_recap(user_id)

    if nlu.intent == "smalltalk":
        # Pick the fallback line by the type of smalltalk the user wrote
        # — judges who say "cảm ơn" deserve a "không có chi" not a
        # robotic re-greeting; "tạm biệt" / "bye" gets a sign-off line.
        # The LLM (when reachable) still does the variable phrasing; the
        # fallback only fires when both providers are 429 / offline.
        from ..nlp.entities import normalize_alias
        _folded = normalize_alias(nlu.raw_text or "")
        if any(t in _folded for t in ("cam on", "cám ơn", "thank")):
            fallback = "Không có chi! Cần gì bạn cứ nhắn mình nhé."
        elif any(
            t in _folded
            for t in ("tam biet", "tạm biệt", "bye", "goodbye", "good night")
        ):
            fallback = "Hẹn gặp lại bạn! Có việc gì cứ gọi Omni nhé."
        else:
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


_BARE_RECIPIENT_COMMANDS = {
    "huy", "huỷ", "hủy", "cancel", "ok", "okay", "đồng ý", "dong y",
    "yes", "no", "không", "khong", "stop", "xác nhận", "xac nhan",
    "y", "n", "lưu", "luu",
}


_FRESH_TRANSFER_VERB_RE = re.compile(
    r"^\s*(?:chuyển|chuyen|gửi|gui|trả|tra|nạp|nap|"
    r"thanh\s+toán|thanh\s+toan|send|transfer)\s+",
    re.IGNORECASE,
)
_MODIFY_VERB_RE = re.compile(
    # Words that signal "edit the current draft, don't start a new one".
    # Includes: đổi/sửa/thành/sang for amount-or-recipient edits, and
    # cộng/thêm/giảm/bớt/tăng for additive amount tweaks.
    r"^\s*(?:đổi|doi|sửa|sua|thay\s+đổi|thay\s+doi|"
    r"thành|thanh\b|sang\b|"
    r"cộng|cong|thêm|them|giảm|giam|bớt|bot|tăng|tang)\s+",
    re.IGNORECASE,
)


def _looks_like_fresh_transfer_command(text: str) -> bool:
    """Does the message start with a fresh verb-led transfer command?

    Used to decide whether to wipe the previous draft's amount /
    recipient before the modify path runs. "chuyển tiền cho t" /
    "gửi 500k cho ai đó" / "trả 200k mẹ" are fresh — they shouldn't
    inherit the previous draft's amount.

    "đổi sang 5tr" / "cộng thêm 500k" are NOT fresh — those are
    explicit edits on the existing draft.
    """
    if _MODIFY_VERB_RE.search(text):
        return False
    return bool(_FRESH_TRANSFER_VERB_RE.search(text))


def _looks_like_bare_recipient(text: str) -> bool:
    """Heuristic: is the user's short reply a recipient name rather than
    a command? Used only when an active draft is waiting for a recipient.

    Conservative — we'd rather miss a recipient hint and ask again than
    treat "ok" or "huỷ" as someone's name. Rejects:
      - longer than 30 chars (commands stay short; long → free-text)
      - matches an amount span ("100k", "2tr", "1tr5") — the user is
        editing the amount, not naming the recipient
      - all-digit short input (probably an OTP / amount)
      - matches a known command word after diacritic-fold
      - empty / whitespace-only

    Bare digits inside a label ("bạn cấp 3", "lớp 12") are KEPT — the
    pre-fix rejected the whole message because of the "3" and slot-fill
    never fired for these labels.
    """
    import re as _re
    import unicodedata as _u

    s = text.strip()
    if not s or len(s) > 30:
        return False
    # Amount-shape rejection — digit followed by VN money unit. Plain
    # digits without unit (a label like "Khoá 5" / "bạn cấp 3") are
    # NOT amounts and stay eligible as recipient surfaces.
    if _re.search(
        r"\d+\s*(?:tr|triệu|trieu|k|nghìn|nghin|ngàn|ngan|"
        r"tỷ|ty|tỉ|ti|đ|vnd|dong|đồng)\b",
        s,
        flags=_re.IGNORECASE,
    ):
        return False
    # All-digit short input is almost certainly an OTP or a typed
    # amount, not a name. Reject so it doesn't slot-fill.
    if _re.fullmatch(r"\s*\d{1,6}\s*", s):
        return False
    folded = "".join(
        c for c in _u.normalize("NFKD", s.lower())
        if not _u.combining(c)
    ).replace("đ", "d")
    if folded in _BARE_RECIPIENT_COMMANDS:
        return False
    return True


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
    # Source-account switch ("dùng tài khoản phụ") — only when it resolves
    # to one of the draft's own accounts, so a recipient-side account hint
    # doesn't spuriously route here.
    if e.account_hint and _resolve_source_account(e.account_hint, draft.source_accounts):
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


def _resolve_source_account(hint: Optional[str], accounts: list) -> Optional[str]:
    """Resolve a chat reference to one of the *user's own* accounts (the
    sender / "người gửi") → its id, or ``None`` if the hint doesn't clearly
    name one. Handles "tài khoản phụ/chính", "tiết kiệm", "chính/main",
    digit tails ("…7891"), and ordinals ("1"/"2"). Only matches against the
    supplied account list so a recipient-account hint can't hijack it."""
    if not hint or not accounts:
        return None
    folded = normalize_alias(hint)
    digits = re.sub(r"\D", "", hint)
    # Explicit digit tail — strongest signal.
    if digits:
        for a in accounts:
            if a.number.endswith(digits) or digits in a.number:
                return a.id
    primary = next((a for a in accounts if getattr(a, "primary", False)), None)
    secondary = next((a for a in accounts if not getattr(a, "primary", False)), None)
    if any(k in folded for k in ("phu", "tiet kiem", "saving", "tk2", "thu 2", "thu hai")):
        return secondary.id if secondary else None
    if any(k in folded for k in ("chinh", "main", "tk1", "thu 1", "thu nhat")):
        return primary.id if primary else None
    return None


def _modify_transfer_draft(
    user_id: str, draft: TransactionDraft, nlu: NLUResult
) -> OmniResponse:
    store = get_store()
    contacts = store.contacts_of(user_id)
    txs = store.transactions_of(user_id)
    e = nlu.entities

    # Source-account ("người gửi") switch — "dùng tài khoản phụ nhé",
    # "chuyển từ tài khoản chính". Resolve against the draft's own account
    # list so a recipient-side account_hint can't move the sender.
    if e.account_hint:
        switched = _resolve_source_account(e.account_hint, draft.source_accounts)
        if switched is not None:
            draft.source_account_id = switched

    account = (
        store.account_by_id(user_id, draft.source_account_id)
        if draft.source_account_id
        else store.primary_account(user_id)
    )

    # Amount edit — but ONLY trust a sum the user actually typed THIS turn.
    # The LLM's FOLLOW-UP rule re-emits the previous turn's amount, so
    # ``e.amount`` is non-null even when the user said nothing about money;
    # ``parse_amount`` over the raw text is the deterministic "did they type
    # a number now?" gate. When they did, apply it; when they didn't, leave
    # ``draft.amount`` untouched so the rest of the transaction is
    # remembered (changing only the recipient must not wipe the sum).
    from ..nlp.amount import parse_amount as _parse_amount_raw

    parsed_now, _ = _parse_amount_raw(nlu.raw_text)
    # "Did the user mention a number THIS turn?" — a parseable amount, or
    # any bare digit (covers "chỉ 5 thôi" where the unit is dropped and the
    # bare-amount continuation already resolved it into ``e.amount``). No
    # digit at all ⇒ ``e.amount`` is an LLM-inherited leftover ⇒ ignore it.
    typed_number = parsed_now is not None or bool(re.search(r"\d", nlu.raw_text))
    if typed_number:
        new_amount = e.amount if e.amount is not None else parsed_now
        if new_amount is not None and new_amount != draft.amount:
            draft.amount = new_amount
            # User-typed → no longer a prediction/suggestion.
            draft.predicted_amount = False
            draft.suggested_amount = None
            draft.amount_prediction_reason = None
            draft.amount_prediction_confidence = None
    if e.description:
        draft.description = e.description
        # Description changed — re-categorise.
        cat, conf = categorize_description(e.description)
        draft.category = cat if (cat != "other" and conf >= 0.5) else None

    # ``eval_candidates`` (resolved, >1) lets the safety engine raise
    # ``ambiguous_recipient`` (with the disambig list) instead of a
    # dead-end ``missing_recipient`` — from origin/main's slot-fill fix.
    recipient_changed = False
    eval_candidates: list = []
    if e.recipient_text:
        candidates = resolve_recipient(e.recipient_text, contacts, kind=e.recipient_kind)
        # Does the raw message actually name a recipient ("cho / tới / gửi /
        # đến <X>")? This separates a real recipient change from the rule
        # extractor hallucinating one out of filler/verbs during an
        # amount-only edit ("à thôi chỉ chuyển 10 triệu", "chỉ 10 triệu
        # thôi"). The latter must NOT disturb the existing recipient — else
        # Omni asks "chuyển cho ai?" mid-edit (the rate-limited-fallback bug).
        # Require a NON-digit after the preposition: "cho 8 triệu" ("make
        # it 8 million") is an amount edit, not "cho <name>".
        named_explicitly = bool(
            re.search(
                r"\b(?:cho|tới|toi|gửi|gui|đến|den)\s+(?!\d)\S",
                nlu.raw_text,
                re.IGNORECASE,
            )
        )
        if len(candidates) == 1:
            # Clean single match — honour the swap ("đổi sang chị Thảo").
            new_recipient = candidates[0].contact
            recipient_changed = (
                draft.recipient is None or new_recipient.id != draft.recipient.id
            )
            draft.recipient = new_recipient
            draft.candidates = []
        elif len(candidates) > 1:
            # Ambiguous — ALWAYS surface the candidate list (even a bare-name
            # slot-fill "Minh" with no "cho" marker), so the safety engine
            # raises ``ambiguous_recipient`` with the disambiguation choices
            # instead of a dead-end "cho ai?". (origin/main slot-fill fix.)
            recipient_changed = True
            draft.recipient = None
            draft.candidates = [c.contact for c in candidates]
            eval_candidates = candidates
        elif named_explicitly:
            # User explicitly named someone (cho/gửi/…) but nobody resolved.
            # Don't keep the old recipient with the new amount (money-touching:
            # "cho Bố" → "cho bạn thân") — clear so the engine asks for clarity.
            recipient_changed = True
            draft.recipient = None
            draft.candidates = []
        # else: no naming marker + no clean match → likely an amount-only edit
        # where the extractor hallucinated a name → PRESERVE existing recipient.

    # Remember the WHOLE transaction. Changing only the recipient must KEEP
    # the amount the user already set — "chuyển cho mẹ 1tr" → "à thôi chuyển
    # cho minh" should stay 1tr for Minh, not make the user re-enter it.
    # We never auto-invent a NEW figure for the new recipient (that was the
    # rejected "tự set 220.000đ" behaviour). Only when NO amount has been
    # set at all do we OFFER a history-based suggestion for the (new)
    # recipient via a tappable chip — ``amount`` stays None so it still
    # asks "bao nhiêu", and only the user's tap commits the figure.
    if recipient_changed and draft.amount is None:
        draft.predicted_amount = False
        draft.suggested_amount = None
        draft.amount_prediction_reason = None
        draft.amount_prediction_confidence = None
        if draft.recipient is not None:
            prediction = predict_amount(user_id, draft.recipient.id)
            if prediction is not None:
                draft.suggested_amount = prediction.get("amount")
                draft.amount_prediction_reason = prediction.get("rationale")
                draft.amount_prediction_confidence = prediction.get("confidence")

    return _finalize_transfer_draft(user_id, draft, txs, account, eval_candidates)


def _finalize_transfer_draft(
    user_id: str,
    draft: TransactionDraft,
    txs,
    account,
    eval_candidates: Optional[list] = None,
) -> OmniResponse:
    """Re-run safety on a mutated draft, refresh the mini-ledger, persist it
    and compose the reply. Shared by the rule-based modify path and the
    LLM-driven draft-action path so both go through the SAME deterministic
    safety gate (anomaly / balance / step-up). ``eval_candidates`` (resolved
    candidates when the recipient is ambiguous) lets the engine raise
    ``ambiguous_recipient`` with the disambiguation list."""
    draft.flags = evaluate(
        amount=draft.amount,
        recipient_candidates=eval_candidates or [],
        recipient=draft.recipient,
        transactions=txs,
        account=account,
        user_id=user_id,
    )
    draft.requires_step_up = requires_step_up(draft.flags)
    draft.auth_required = auth_policy(draft.flags)
    draft.auth_completed = [a for a in draft.auth_completed if a in draft.auth_required]
    # Recipient may have changed in this edit; refresh the mini-ledger so the
    # chat card matches the new recipient.
    draft.recent_to_recipient = _recent_to_recipient(user_id, draft.recipient)

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


def _handle_my_account(user_id: str) -> OmniResponse:
    """Show the user's own bank accounts so they can share for inbound
    transfers. Read-only — no money movement, no draft."""
    store = get_store()
    user = store.get_user(user_id)
    primary = next((a for a in user.accounts if a.primary), user.accounts[0] if user.accounts else None)
    if primary is None:
        return OmniResponse(
            intent="my_account",
            text="Bạn chưa có tài khoản nào trong Omni. Liên kết tài khoản trước nhé.",
        )
    accounts = [
        {
            "id": a.id,
            "bank": a.bank,
            "number": a.number,
            "masked": f"****{a.number[-4:]}",
            "holder_name": user.display_name,
            "primary": a.primary,
        }
        for a in user.accounts
    ]
    # Compose a friendly text summary so screen-reader / replay paths
    # still get the info even without the my_accounts card.
    primary_line = (
        f"Tài khoản chính của bạn: {primary.bank} · {primary.number} "
        f"(chủ: {user.display_name})."
    )
    if len(user.accounts) > 1:
        extra = ", ".join(
            f"{a.bank} {a.number[-4:]}"
            for a in user.accounts
            if not a.primary
        )
        primary_line += f" Tài khoản khác: {extra}."
    return OmniResponse(
        intent="my_account",
        text=primary_line,
        my_accounts=accounts,
    )


def _handle_receive_qr(user_id: str, nlu: NLUResult) -> OmniResponse:
    """Generate a VietQR-style payload for the user's primary account.

    Optional amount / description from the NLU result get baked into the
    QR so the sender's banking app pre-fills both fields after the scan.
    """
    from ..banking.qr import encode_payload, generate_payment_qr

    store = get_store()
    user = store.get_user(user_id)
    primary = next((a for a in user.accounts if a.primary), user.accounts[0] if user.accounts else None)
    if primary is None:
        return OmniResponse(
            intent="receive_qr",
            text="Bạn chưa có tài khoản nào để tạo QR. Liên kết tài khoản trước nhé.",
        )
    e = nlu.entities
    amount = e.amount
    desc = (e.description or "").strip() or None
    payload = encode_payload(
        bank=primary.bank,
        account_number=primary.number,
        amount=amount,
        message=desc,
    )
    png_b64 = generate_payment_qr(
        bank=primary.bank,
        account=primary.number,
        amount=amount,
        message=desc,
    )
    amount_line = (
        f" số tiền {format_vnd(amount)}." if amount else "."
    )
    desc_line = f" Nội dung: {desc}." if desc else ""
    text = (
        f"Đây là QR nhận tiền của bạn — quét bằng app ngân hàng để chuyển vào "
        f"{primary.bank} · {primary.number} (chủ {user.display_name}){amount_line}"
        f"{desc_line}"
    )
    return OmniResponse(
        intent="receive_qr",
        text=text,
        receive_qr={
            "bank": primary.bank,
            "account": primary.number,
            "holder_name": user.display_name,
            "amount": amount,
            "description": desc,
            "payload": payload,
            "png_base64": png_b64,
        },
    )


def _handle_recap(user_id: str) -> OmniResponse:
    """Surface the user's CURRENT session state so questions like
    "tôi vừa nói gì" / "lúc nãy số tiền bao nhiêu" / "đang chuyển cho
    ai" stop falling through to the generic history fallback.

    Priority:
      1. An active TRANSFER draft → describe its slots verbatim (amount,
         recipient, description if set).
      2. An active SCHEDULE / CONTACT / BUDGET / GOAL draft → describe it.
      3. The most recent COMPLETED transaction in the last 24h → summarise.
      4. Nothing relevant → polite "không có giao dịch nào đang chờ".
    """
    session = session_for(user_id)
    draft = session.current_draft
    if draft is not None:
        # Build a deterministic Vietnamese recap of the active draft.
        parts: list[str] = ["Bạn đang chuẩn bị giao dịch:"]
        if draft.amount is not None:
            parts.append(f"• Số tiền: {format_vnd(draft.amount)}")
        else:
            parts.append("• Số tiền: chưa rõ")
        if draft.recipient is not None:
            parts.append(
                f"• Người nhận: {draft.recipient.display_name} "
                f"({draft.recipient.bank})"
            )
        elif draft.candidates:
            names = ", ".join(c.display_name for c in draft.candidates[:3])
            parts.append(f"• Người nhận: chọn 1 trong {names}")
        else:
            parts.append("• Người nhận: chưa rõ")
        if draft.description:
            parts.append(f"• Nội dung: {draft.description}")
        if draft.awaiting_otp:
            parts.append("• Trạng thái: đang chờ OTP")
        text = "\n".join(parts)
        return OmniResponse(intent="recap", text=text, draft=draft)

    sched = session.current_schedule_draft
    if sched is not None:
        text = (
            f"Bạn đang đặt lịch chuyển {format_vnd(sched.amount)} cho "
            f"{sched.recipient.display_name} — {sched.cron_label}."
        )
        return OmniResponse(intent="recap", text=text, schedule_draft=sched)

    contact = session.current_contact_draft
    if contact is not None:
        return OmniResponse(
            intent="recap",
            text=(
                f"Bạn đang lưu danh bạ: {contact.display_name} · "
                f"{contact.bank} · STK {contact.account_number}."
            ),
            contact_draft=contact,
        )

    # No active draft — show the most recent completed transfer (last 24h).
    txs = get_store().transactions_of(user_id, status="completed")
    if txs:
        last = max(txs, key=lambda t: t.created_at)
        from datetime import timedelta as _td

        if now() - last.created_at <= _td(hours=24):
            contact_obj = get_store().get_contact(last.contact_id)
            who = contact_obj.display_name if contact_obj else "—"
            return OmniResponse(
                intent="recap",
                text=(
                    f"Giao dịch gần nhất: đã chuyển {format_vnd(last.amount)} "
                    f"cho {who} lúc "
                    f"{last.created_at.strftime('%H:%M %d/%m')}. "
                    f"Bạn cần làm gì tiếp?"
                ),
            )

    return OmniResponse(
        intent="recap",
        text=(
            "Hiện chưa có giao dịch nào đang chờ. "
            "Bạn gõ 'chuyển cho mẹ 2 triệu' để bắt đầu nhé."
        ),
    )


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
        candidates = resolve_recipient(e.recipient_text, contacts, kind=e.recipient_kind)
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
        candidates = resolve_recipient(e.recipient_text, contacts, kind=e.recipient_kind)
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
        "today": "hôm nay",
        "yesterday": "hôm qua",
        "this_week": "tuần này",
        "last_week": "tuần trước",
        "this_month": "tháng này",
        "last_month": "tháng trước",
        "this_year": "năm nay",
        "last_year": "năm ngoái",
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

    candidates = resolve_recipient(e.recipient_text, contacts, kind=e.recipient_kind)
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
    """Pretty-print the cron expressions we generate in the entity extractor.

    Cron DOW: 0=Sun, 1=Mon, 2=Tue, ..., 6=Sat. The previous mapping
    indexed Monday (DOW=1) into ``names[1] = "thứ Ba"`` (Tuesday) —
    every weekly schedule rendered with the wrong day label. Now keyed
    by the standard cron DOW directly.
    """
    parts = cron.split()
    if len(parts) == 5:
        _, _, dom, _, dow = parts
        if dom.isdigit():
            return f"vào ngày {int(dom)} hàng tháng"
        if dow == "*":
            return "mỗi ngày"
        if dow.isdigit():
            names = {
                0: "Chủ Nhật",
                1: "thứ Hai",
                2: "thứ Ba",
                3: "thứ Tư",
                4: "thứ Năm",
                5: "thứ Sáu",
                6: "thứ Bảy",
            }
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


def _recent_to_recipient(
    user_id: str, recipient: Optional[Contact]
) -> Optional[list[dict]]:
    """Mini-history payload for the confirm card — last 3 completed
    transfers to ``recipient``. Returned as plain dicts so the schema
    keeps the same shape across every TransactionDraft producer
    (_handle_transfer, _modify_transfer_draft, select_candidate).

    Lifted out of ``_handle_transfer`` so the modify-draft and
    disambiguation-select code paths don't keep returning ``None`` —
    judges who say "đổi sang 3 triệu" or pick a candidate from KB3
    should still see the per-recipient ledger that KB1/KB2 show.
    """
    if recipient is None:
        return None
    recent_txs = get_store().transactions_of(
        user_id, contact_id=recipient.id, status="completed", limit=3,
    )
    if not recent_txs:
        return None
    return [
        {
            "amount": t.amount,
            "created_at": t.created_at.isoformat(),
            "description": t.description,
            "category": t.category,
        }
        for t in recent_txs
    ]


_TRANSFER_VERB_RE = re.compile(
    r"\b(?:chuyển|chuyen|gửi|gui|gởi|goi|nạp|nap|transfer|send)\b",
    re.IGNORECASE,
)


def _msg_has_transfer_signal(nlu: NLUResult) -> bool:
    """Did the CURRENT message actually carry a transfer cue, or is the
    intent purely inherited from conversation history?

    The LLM's FOLLOW-UP rule re-emits the previous turn's intent + amount +
    recipient, so a vague "ờ cái kia" / "chắc vậy" with stale history comes
    back as a fully-populated ``transfer`` — fabricating a draft (amount +
    recipient) the user never stated this turn. This is the deterministic
    backstop: a *fresh* transfer draft may only be built when the current
    raw text contributes something concrete — a money-movement verb, a
    typed amount, a temporal reference ("như tháng trước"), or a recipient
    surface that genuinely appears in the text. Otherwise we ask instead of
    inventing. Never trust ``e.amount`` / ``e.recipient_text`` alone here —
    those can be inherited; verify against the raw text."""
    raw = nlu.raw_text or ""
    if _TRANSFER_VERB_RE.search(raw):
        return True
    from ..nlp.amount import parse_amount as _pa

    if _pa(raw)[0] is not None:
        return True
    if nlu.entities.temporal_reference:
        return True
    # Recipient surface must actually occur in this message — the rule
    # extractor only ever reads the raw text, so a hit there is proof the
    # name was typed now (not inherited).
    from ..nlp.entities import extract as _rule_extract

    if _rule_extract(raw).recipient_text:
        return True
    return False


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

    # Anti-fabrication backstop: a brand-new transfer draft requires the
    # current message to actually express a transfer (see
    # ``_msg_has_transfer_signal``). When the LLM inherited ``transfer``
    # from history but the user said nothing transfer-shaped this turn,
    # ask instead of inventing a recipient + amount from stale context.
    if not _msg_has_transfer_signal(nlu):
        return OmniResponse(
            intent="transfer",
            text="Bạn muốn chuyển bao nhiêu và cho ai ạ?",
        )

    e = nlu.entities

    candidates = (
        resolve_recipient(e.recipient_text, contacts, kind=e.recipient_kind) if e.recipient_text else []
    )
    if e.account_hint:
        candidates = filter_by_account_hint(candidates, e.account_hint)

    chosen: Optional[Contact] = None
    if len(candidates) == 1:
        chosen = candidates[0].contact

    # Resolve temporal reference using the chosen recipient (if any) for higher precision
    amount = e.amount
    # Drop an LLM-inherited amount on a *fresh* transfer: if the user didn't
    # type a sum this turn (and isn't referencing a past one via "như tháng
    # trước"), ``e.amount`` came from the FOLLOW-UP rule re-emitting an older
    # turn — not from what they just said. Leaving it would fabricate a
    # figure; drop it so the predictor offers a suggestion instead.
    if amount is not None and not e.temporal_reference:
        from ..nlp.amount import parse_amount as _pa_raw

        if _pa_raw(nlu.raw_text)[0] is None and not re.search(r"\d", nlu.raw_text):
            amount = None
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

    # Sanity check on user-supplied amount. Zero and negatives are
    # nonsense in a transfer context. Critically, DON'T let the
    # predictor below "fix" them — silently swapping a user's "0đ" or
    # "-5tr" for a median-from-history while the user typed an
    # explicit (if absurd) number is the worst UX the stress test
    # found (predictor wrote 750k over user-typed 100M). Clear the
    # amount AND flag it as user-invalid so the predictor branch skips
    # and the safety engine raises ``missing_amount`` for clarification.
    user_invalid_amount = False
    if amount is not None and amount <= 0:
        amount = None
        user_invalid_amount = True
    # The amount-parser regexes don't include a sign, so "-5tr" parses
    # to 5_000_000 with no marker. Detect a leading minus in the raw
    # message and reject that too.
    if amount is not None and re.search(r"-\s*\d", nlu.raw_text):
        amount = None
        user_invalid_amount = True

    # Smart amount SUGGESTION: when the user named a recipient but no amount,
    # OFFER the most likely figure via ``suggested_amount`` (a tappable chip)
    # — but do NOT apply it to ``amount`` (stays None → ``evaluate`` raises
    # ``missing_amount`` → asks "bao nhiêu"; only the user's tap commits it).
    # Offer-don't-decide. ``user_invalid_amount`` skips the offer for a
    # nonsense typed amount (0 / negative) so the user is told it's invalid.
    prediction: Optional[dict] = None
    if amount is None and not user_invalid_amount and chosen is not None:
        prediction = predict_amount(user_id, chosen.id)

    # Auto-categorise BEFORE evaluate() so the safety layer can check
    # this draft against the user's monthly budget for ``category``.
    # Confidence floor of 0.5 keeps weak TF-IDF guesses from polluting
    # the dataset OR producing spurious budget warnings.
    category: Optional[str] = None
    cat_source = description or nlu.raw_text
    if cat_source:
        cat, conf = categorize_description(cat_source)
        if cat != "other" and conf >= 0.5:
            category = cat

    flags = evaluate(
        amount=amount,
        recipient_candidates=candidates,
        recipient=chosen,
        transactions=txs,
        account=account,
        user_id=user_id,
        category=category,
    )

    recent_to_recipient = _recent_to_recipient(user_id, chosen)

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
        auth_required=auth_policy(flags),
        # amount is NOT auto-filled from the predictor anymore, so
        # predicted_amount stays False; the figure is offered via
        # suggested_amount instead.
        predicted_amount=False,
        suggested_amount=(
            prediction.get("amount") if prediction is not None else None
        ),
        amount_prediction_reason=(
            prediction.get("rationale") if prediction is not None else None
        ),
        amount_prediction_confidence=(
            prediction.get("confidence") if prediction is not None else None
        ),
        category=category,
        recent_to_recipient=recent_to_recipient,
    )

    session = session_for(user_id)
    session.set_draft(draft)

    # ``_compose_transfer_text`` surfaces the suggestion hint when the
    # amount is the only missing slot — no override needed here.
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


# A bare integer that, in an active-draft edit, is an amount rather than
# an OTP / ordinal. Capped at 3 digits so a 6-digit OTP never inherits a
# unit. The leading edit/command frame ("chỉ … thôi", "chuyển", "đổi
# sang", "còn") keeps a stray number in unrelated text from being read as
# a new amount.
_BARE_AMOUNT_RE = re.compile(
    r"(?:^|\bchi\b|\bchỉ\b|\bcon\b|\bcòn\b|\bchuyen\b|\bchuyển\b|\bgui\b|"
    r"\bgửi\b|\bdoi\b|\bđổi\b|\bsang\b|\bthanh\b|\bthành\b|\bsua\b|\bsửa\b)"
    r"[^0-9]*?\b(\d{1,3})\b",
    re.IGNORECASE,
)


def _draft_unit(amount: int) -> int:
    """The conversational unit a bare number inherits from the current
    draft. On a 20-triệu draft "5" means 5 triệu (unit 1e6); on a 500k
    draft "200" means 200 nghìn (unit 1e3)."""
    if amount >= 1_000_000_000:
        return 1_000_000_000
    if amount >= 1_000_000:
        return 1_000_000
    if amount >= 1_000:
        return 1_000
    return 1


def _restated_amount(text: str, draft: TransactionDraft) -> Optional[int]:
    """The new transfer amount when a mid-draft message (re)states one.

    Two cases:
      * Explicit, unit-bearing — "5 triệu", "5tr", "500k": delegate to
        ``parse_amount``.
      * Bare number in an edit frame — "thôi chỉ chuyển 20 thôi",
        "đổi sang 5", "còn 10 thôi": the user dropped the unit because
        the conversation is already at a known scale, so inherit the
        draft's unit (see ``_draft_unit``).

    Returns ``None`` when the message names no amount. The bare-number
    branch only fires once the draft already has a committed recipient —
    otherwise a lone digit is a candidate pick ("người 2"), not a sum."""
    from ..nlp.amount import parse_amount

    amount, _ = parse_amount(text)
    if amount is not None:
        return amount
    if draft.recipient is None:
        return None
    m = _BARE_AMOUNT_RE.search(text)
    if not m:
        return None
    return int(m.group(1)) * _draft_unit(draft.amount or 0)


def _draft_context_for_llm(user_id: str, draft: TransactionDraft) -> dict:
    """Human-readable snapshot of the pending draft for the LLM prompt."""
    store = get_store()
    account = (
        store.account_by_id(user_id, draft.source_account_id)
        if draft.source_account_id
        else store.primary_account(user_id)
    )
    if draft.recipient is not None:
        recipient = draft.recipient.display_name
    elif draft.candidates:
        recipient = "chưa rõ (" + ", ".join(c.display_name for c in draft.candidates) + ")"
    else:
        recipient = "chưa chọn"
    return {
        "recipient": recipient,
        "amount": format_vnd(draft.amount) if draft.amount else "chưa nhập",
        "balance": format_vnd(account.balance) if account else "không rõ",
    }


# When the user starts a NEW transfer while an unconfirmed draft is still
# open, we don't silently discard the old one — we ASK first and stash the
# new request text here, keyed by user, until they answer có/không.
_PENDING_RESTART: dict[str, str] = {}

_RESTART_YES_RE = re.compile(
    r"^\s*(?:có|co|ừ|u|um|ừm|vâng|vang|đồng\s*ý|dong\s*y|ok|okay|được|duoc|"
    r"huỷ|huy|hủy|đúng|dung\s*rồi|yes|y|chuyển\s*mới|moi)\b",
    re.IGNORECASE,
)
_RESTART_NO_RE = re.compile(
    r"^\s*(?:không|khong|ko|đừng|giữ|giu|khỏi|khoi|thôi\s*khỏi|no|giữ\s*lại)\b",
    re.IGNORECASE,
)


def _describe_draft_short(draft: TransactionDraft) -> str:
    """One-line human summary of a pending draft for the discard prompt."""
    if draft.recipient is not None:
        who = draft.recipient.display_name
    elif draft.candidates:
        who = " / ".join(c.display_name for c in draft.candidates)
    else:
        who = "chưa chọn người nhận"
    amt = format_vnd(draft.amount) if draft.amount else "chưa nhập số tiền"
    return f"{amt} cho {who}"


def _start_fresh_transfer(
    user_id: str, text: str, history_msgs: list[dict]
) -> OmniResponse:
    """Run a brand-new transfer request through the normal NLU + dispatch
    (old draft already cleared by the caller)."""
    nlu = understand(text, history=history_msgs)
    return _dispatch_intent(user_id, nlu, history_msgs)


def _continue_draft_llm_first(
    user_id: str, text: str, draft: TransactionDraft, history_msgs: list[dict]
) -> Optional[OmniResponse]:
    # First: are we waiting for the user to answer "huỷ giao dịch cũ?" — if
    # so this turn is that answer, not a draft edit.
    pending = _PENDING_RESTART.get(user_id)
    if pending is not None:
        if _RESTART_YES_RE.search(text):
            _PENDING_RESTART.pop(user_id, None)
            cancel_draft(user_id, draft.id)  # discard the old draft
            # If the answer itself carries a fuller new command, use it;
            # otherwise replay the original new request we stashed.
            new_text = text if _TRANSFER_VERB_RE.search(text) else pending
            return _start_fresh_transfer(user_id, new_text, history_msgs)
        if _RESTART_NO_RE.search(text):
            _PENDING_RESTART.pop(user_id, None)
            return OmniResponse(
                intent="transfer",
                text=(
                    "Được, mình giữ lại giao dịch cũ. Bạn xác nhận, sửa, "
                    "hoặc nói 'huỷ' nếu muốn bỏ nhé."
                ),
                draft=draft,
            )
        # Unclear answer → keep the old draft, drop the pending (don't loop),
        # and ask them to be explicit. Safe default: never discard without a
        # clear yes.
        _PENDING_RESTART.pop(user_id, None)
        return OmniResponse(
            intent="transfer",
            text=(
                f"Mình vẫn giữ giao dịch đang chờ ({_describe_draft_short(draft)}). "
                "Bạn muốn tiếp tục giao dịch này, hay nói 'huỷ' để bỏ nó nhé."
            ),
            draft=draft,
        )

    return _continue_draft_llm_first_inner(user_id, text, draft, history_msgs)


def _continue_draft_llm_first_inner(
    user_id: str, text: str, draft: TransactionDraft, history_msgs: list[dict]
) -> Optional[OmniResponse]:
    """LLM-first continuation for an active transfer draft.

    The LLM reads the pending draft + history + this message and returns a
    structured decision (confirm / cancel / otp / edit / unclear / other),
    so novel phrasings work ("khoan đổi sang sếp", "gấp đôi", "chuyển hết số
    dư") instead of needing a hand-written rule each time. Returns:

      * an ``OmniResponse`` when the LLM resolved the turn,
      * ``None`` when the message isn't about the draft (action="other") —
        the caller continues to the normal NLU dispatch,
      * the rule-based :func:`_try_continue_draft` result when no LLM is
        reachable (deterministic offline fallback).
    """
    decision = llm_draft_action(text, _draft_context_for_llm(user_id, draft), history_msgs)
    if decision is None:
        # No LLM → deterministic rules.
        return _try_continue_draft(user_id, text, draft)

    action = decision.get("action")
    if action == "other":
        return None  # not about this draft → let normal NLU handle it
    if action == "restart":
        # Fresh standalone transfer command ("chuyển cho mẹ") while a draft
        # is still open and UNCONFIRMED. Don't silently discard it — ask the
        # user to confirm cancelling the old one first, and stash the new
        # request to replay once they say yes.
        _PENDING_RESTART[user_id] = text
        return OmniResponse(
            intent="transfer",
            text=(
                f"Bạn đang có một giao dịch chưa hoàn tất: "
                f"{_describe_draft_short(draft)}. Huỷ giao dịch này để bắt đầu "
                f"giao dịch mới chứ? (có / không)"
            ),
            draft=draft,
        )
    if action == "confirm":
        return confirm_draft(user_id, draft.id)
    if action == "cancel":
        return cancel_draft(user_id, draft.id)
    if action == "otp":
        code = (decision.get("otp_code") or "").strip()
        if re.fullmatch(r"\d{4,6}", code):
            return confirm_draft(user_id, draft.id, otp=code)
        # malformed → fall back to rules
        return _try_continue_draft(user_id, text, draft)
    if action == "edit":
        return _apply_llm_draft_action(user_id, draft, decision)
    # "unclear" (or anything unexpected) → ask, never fabricate.
    return OmniResponse(
        intent="transfer",
        text=(
            "Bạn muốn đổi gì cho giao dịch này — số tiền, người nhận, "
            "hay tài khoản nguồn? Hoặc nói 'xác nhận' / 'huỷ' nhé."
        ),
        draft=draft,
    )


def _apply_llm_draft_action(
    user_id: str, draft: TransactionDraft, decision: dict
) -> OmniResponse:
    """Apply an LLM-resolved ``edit`` decision to the draft, deterministically.

    Recipient/amount/account are taken ONLY from the structured decision
    (which the prompt forbids from inventing). Relative amount ops are
    computed here from the draft's own amount / the source balance, so the
    LLM never has to do arithmetic. The shared safety gate then re-runs via
    :func:`_finalize_transfer_draft`."""
    store = get_store()
    contacts = store.contacts_of(user_id)
    txs = store.transactions_of(user_id)

    # 1) Source account switch.
    if decision.get("account_hint"):
        switched = _resolve_source_account(
            decision["account_hint"], draft.source_accounts
        )
        if switched is not None:
            draft.source_account_id = switched
    account = (
        store.account_by_id(user_id, draft.source_account_id)
        if draft.source_account_id
        else store.primary_account(user_id)
    )

    # 2) Recipient change.
    recipient_changed = False
    rt = decision.get("recipient_text")
    if rt:
        cands = resolve_recipient(rt, contacts)
        if len(cands) == 1:
            new_rec = cands[0].contact
            recipient_changed = (
                draft.recipient is None or new_rec.id != draft.recipient.id
            )
            draft.recipient = new_rec
            draft.candidates = []
        else:
            # ambiguous (>1) or unknown (0): surface it, don't keep the old
            # recipient with a new edit — the user named someone else.
            recipient_changed = True
            draft.recipient = None
            draft.candidates = [c.contact for c in cands]

    # 3) Amount change — absolute or relative. Computed deterministically.
    new_amount: Optional[int] = None
    if decision.get("amount_vnd") is not None:
        try:
            new_amount = int(decision["amount_vnd"])
        except (TypeError, ValueError):
            new_amount = None
    else:
        op = decision.get("amount_op")
        operand = decision.get("amount_operand")
        base = draft.amount or 0
        try:
            if op == "add" and operand is not None:
                new_amount = base + int(operand)
            elif op == "subtract" and operand is not None:
                new_amount = max(0, base - int(operand))
            elif op == "multiply" and operand is not None:
                new_amount = int(base * float(operand))
            elif op == "fraction" and operand is not None:
                new_amount = int(base * float(operand))
            elif op == "all_balance":
                new_amount = account.balance if account else base
        except (TypeError, ValueError):
            new_amount = None

    if new_amount is not None and new_amount > 0:
        draft.amount = new_amount
        draft.predicted_amount = False
        draft.suggested_amount = None
        draft.amount_prediction_reason = None
        draft.amount_prediction_confidence = None

    # 4) Redirected to a new person with no amount on record yet → offer a
    # history suggestion for THEM (chip, not auto-set). If an amount is
    # already set, it's remembered across the recipient change.
    if recipient_changed and draft.amount is None and draft.recipient is not None:
        draft.suggested_amount = None
        draft.amount_prediction_reason = None
        draft.amount_prediction_confidence = None
        prediction = predict_amount(user_id, draft.recipient.id)
        if prediction is not None:
            draft.suggested_amount = prediction.get("amount")
            draft.amount_prediction_reason = prediction.get("rationale")
            draft.amount_prediction_confidence = prediction.get("confidence")

    return _finalize_transfer_draft(user_id, draft, txs, account)


def _try_continue_draft(
    user_id: str, text: str, draft: TransactionDraft
) -> Optional[OmniResponse]:
    # An amount edit takes priority over a leading cancel particle: a
    # *pure* cancel ("thôi", "huỷ", "không cần nữa") never names a sum, so
    # "thôi chỉ chuyển 20 thôi" / "thôi chỉ 5 triệu thôi" is an edit, not a
    # kill. Resolve the (possibly unit-less) restated amount up front so
    # the cancel short-circuit can step aside for it.
    new_amount = _restated_amount(text, draft)

    # Does this message name a REAL (resolvable) recipient? Distinguishes a
    # redirect ("thôi chuyển cho bố" → switch to bố, keep the amount) from a
    # pure cancel ("thôi", "thôi không chuyển nữa"). We resolve the surface
    # form so a filler word the extractor grabs ("nữa") doesn't masquerade
    # as a recipient and block a genuine cancel.
    from ..nlp.entities import extract as _rule_extract

    _rt = _rule_extract(text).recipient_text
    names_real_recipient = bool(
        _rt and resolve_recipient(_rt, get_store().contacts_of(user_id))
    )

    # A leading cancel particle only cancels when it's a PURE cancel — no
    # new amount AND no new recipient. "thôi chuyển cho bố" / "thôi chỉ 5
    # triệu thôi" are edits, not kills.
    if _is_cancel(text) and new_amount is None and not names_real_recipient:
        return cancel_draft(user_id, draft.id)

    if _is_confirm(text):
        return confirm_draft(user_id, draft.id)

    if re.fullmatch(r"\d{6}", text.strip()):
        return confirm_draft(user_id, draft.id, otp=text.strip())

    # Amount-only edit on an active draft. Route through the modify path
    # (recipient preserved, safety re-evaluated). A no-op restatement —
    # the same amount the draft already holds — falls out as an unchanged
    # re-render of the card, which is exactly right: don't cancel, don't
    # spawn a new transaction.
    #
    # GUARD — this fast path is ONLY for a *bare* number whose unit was
    # dropped ("thôi chỉ 5 thôi", "đổi sang 5") and which the full NLU
    # pipeline's amount parser would therefore miss. Two exclusions:
    #   1. A unit-bearing amount ("2 triệu", "10 triệu") → fall through to
    #      the full pipeline; the LLM resolves any simultaneous recipient
    #      swap that the (pre-LLM) rule extractor here can't ("đổi sang chị
    #      Thảo 2 triệu").
    #   2. A recipient named in this message → fall through too, so the
    #      swap + amount apply together ("chuyển cho mẹ 10 triệu" must go to
    #      mẹ, not silently keep the old recipient — the reported bug).
    from ..nlp.amount import parse_amount as _pa_continue

    parseable_amount = _pa_continue(text)[0] is not None
    if new_amount is not None and not parseable_amount and not names_real_recipient:
        from ..models.schemas import ExtractedEntities

        synth = NLUResult(
            intent="transfer",
            confidence=0.85,
            entities=ExtractedEntities(amount=new_amount),
            raw_text=text,
            source="rule",
        )
        return _modify_transfer_draft(user_id, draft, synth)

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
# Biometric (8D face scan) verification
# ---------------------------------------------------------------------------

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
        user_id=user_id,
    )
    draft.flags = fresh_flags
    draft.requires_step_up = requires_step_up(fresh_flags)
    draft.auth_required = auth_policy(fresh_flags)
    draft.auth_completed = [a for a in draft.auth_completed if a in draft.auth_required]

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

    # Step-up auth state machine. draft.auth_required is set from
    # auth_policy() above:
    #   normal transfer        -> ["otp"]
    #   risky (warn) transfer  -> ["otp", "biometric"]
    # The confirm endpoint sends otp + biometric_scan together, but each
    # method is verified independently so partial-auth retries also work.
    # awaiting_otp stays True until execution clears the draft, so the
    # chat.py idempotency cache never memoises an intermediate auth step.
    draft.awaiting_otp = True
    session.set_draft(draft)

    # --- OTP preflight ---
    # When OTP + biometric arrive together, validate OTP first so a wrong
    # code stops immediately instead of making the user complete a face scan.
    if "otp" in draft.auth_required and "otp" not in draft.auth_completed and otp is not None:
        if not otp.strip() or otp.strip() != "123456":
            draft.otp_attempts += 1
            session.set_draft(draft)
            if draft.otp_attempts >= 5:
                session.clear_draft()
                text = "Xác minh OTP thất bại. Bạn đã nhập sai OTP quá 5 lần, giao dịch đã được huỷ để bảo vệ tài khoản."
                session.append("user", "Xác minh OTP")
                session.append("omni", text)
                try:
                    from .audit_log import record_otp
                    record_otp(user_id=user_id, draft_id=draft.id, action="failed")
                except Exception:  # pragma: no cover
                    pass
                return OmniResponse(intent="transfer", text=text)
            text = "OTP chưa đúng. Bạn kiểm tra và nhập lại mã xác minh nhé."
            session.append("user", "Xác minh OTP")
            session.append("omni", text)
            try:
                from .audit_log import record_otp
                record_otp(user_id=user_id, draft_id=draft.id, action="failed")
            except Exception:  # pragma: no cover
                pass
            return OmniResponse(intent="transfer", text=text, draft=draft)
        try:
            from .audit_log import record_otp
            record_otp(user_id=user_id, draft_id=draft.id, action="verified")
        except Exception:  # pragma: no cover
            pass
        draft.auth_completed.append("otp")
        draft.otp_attempts = 0
        session.set_draft(draft)

    # --- Biometric (8D face scan) ---
    if (
        "biometric" in draft.auth_required
        and "biometric" not in draft.auth_completed
        and (biometric_scan or biometric_verified)
    ):
        scan_ok, scan_error = _biometric_scan_valid(biometric_scan, draft.id, otp)
        if not scan_ok:
            session.append("user", "Xác minh sinh trắc học")
            session.append("omni", scan_error)
            return OmniResponse(intent="transfer", text=scan_error, draft=draft)
        draft.auth_completed.append("biometric")
        session.set_draft(draft)
        if "otp" in draft.auth_required and "otp" not in draft.auth_completed and otp is None:
            text = "Sinh trắc học đã xác minh. Vui lòng nhập OTP để hoàn tất. Mã demo: 123456."
            session.append("user", "Xác minh sinh trắc học")
            session.append("omni", text)
            return OmniResponse(intent="transfer", text=text, draft=draft)

    # --- OTP ---
    if "otp" in draft.auth_required and "otp" not in draft.auth_completed and otp is None:
        text = "Vui lòng nhập OTP để xác minh giao dịch. Mã demo: 123456."
        if "biometric" in draft.auth_required and "biometric" not in draft.auth_completed:
            text = (
                "Giao dịch rủi ro cần OTP và sinh trắc học. "
                "Vui lòng quét khuôn mặt và nhập OTP. Mã demo: 123456."
            )
        session.append("user", "Xác nhận giao dịch")
        session.append("omni", text)
        try:
            from .audit_log import record_otp
            record_otp(user_id=user_id, draft_id=draft.id, action="requested")
        except Exception:  # pragma: no cover
            pass
        return OmniResponse(intent="transfer", text=text, draft=draft)

    if (
        "otp" in draft.auth_required
        and "otp" not in draft.auth_completed
        and otp
        and otp.strip() != "123456"
    ):
        draft.otp_attempts += 1
        session.set_draft(draft)
        if draft.otp_attempts >= 5:
            session.clear_draft()
            text = "Xác minh OTP thất bại. Bạn đã nhập sai OTP quá 5 lần, giao dịch đã được huỷ để bảo vệ tài khoản."
            session.append("user", "Xác minh OTP")
            session.append("omni", text)
            try:
                from .audit_log import record_otp
                record_otp(user_id=user_id, draft_id=draft.id, action="failed")
            except Exception:  # pragma: no cover
                pass
            return OmniResponse(intent="transfer", text=text)
        text = "OTP chưa đúng. Bạn kiểm tra và nhập lại mã xác minh nhé."
        session.append("user", "Xác minh OTP")
        session.append("omni", text)
        try:
            from .audit_log import record_otp
            record_otp(user_id=user_id, draft_id=draft.id, action="failed")
        except Exception:  # pragma: no cover
            pass
        return OmniResponse(intent="transfer", text=text, draft=draft)

    if "otp" in draft.auth_required and otp and "otp" not in draft.auth_completed:
        # OTP verified — log before execute so the audit trail captures
        # the verify event even if execute throws.
        try:
            from .audit_log import record_otp
            record_otp(user_id=user_id, draft_id=draft.id, action="verified")
        except Exception:  # pragma: no cover
            pass
        draft.auth_completed.append("otp")
        draft.otp_attempts = 0

    # --- Any required method still outstanding? ---
    missing_auth = [m for m in draft.auth_required if m not in draft.auth_completed]
    if missing_auth:
        if "biometric" in missing_auth:
            text = "Còn thiếu xác minh sinh trắc học. Vui lòng quét khuôn mặt 8D rồi gửi lại xác nhận nhé."
        else:
            text = "Còn thiếu xác thực: " + ", ".join(missing_auth) + "."
        session.set_draft(draft)
        session.append("user", "Xác minh giao dịch")
        session.append("omni", text)
        return OmniResponse(intent="transfer", text=text, draft=draft)

    return _execute_and_record(user_id, draft, otp_used="otp" in draft.auth_completed)


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

    # File-based audit trail — SBV-style "5-year immutable record".
    # The service module documents that transfer execution should
    # land in audit.log but the call site was orphaned. Wrap in
    # try/except so a disk-full / permissions failure can never roll
    # back the transfer the user already saw confirmed.
    try:
        from .audit_log import record_transfer_executed

        record_transfer_executed(
            user_id=user_id,
            draft_id=draft.id,
            amount=tx.amount,
            recipient_name=draft.recipient.display_name,  # type: ignore[union-attr]
            source_account_id=draft.source_account_id or "",
            category=draft.category,
        )
    except Exception:  # pragma: no cover — audit must never break chat
        pass

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

    # Split-bill queue: if this draft was part of a split, advance the
    # queue and surface the next draft. The user confirms each split
    # share one tap at a time; "Đã chia tiền với 3 người" is the demo
    # closing line once the queue drains.
    next_split = None
    with _drafts_lock:
        queue = _split_queues.get(user_id)
        if queue:
            next_split = queue.pop(0)
            if not queue:
                _split_queues.pop(user_id, None)
    if next_split is not None:
        session.set_draft(next_split)
        remaining = len(_split_queues.get(user_id, [])) + 1
        body = (
            f"{text}\nCòn {remaining} người trong yêu cầu chia tiền — "
            f"xác nhận chuyển {format_vnd(next_split.amount or 0)} cho "  # type: ignore[arg-type]
            f"{next_split.recipient.display_name}?"  # type: ignore[union-attr]
        )
        return OmniResponse(intent="transfer", text=body, draft=next_split)

    return OmniResponse(intent="transfer", text=text)


def start_split_bill(
    user_id: str,
    *,
    total_amount: int,
    description: str,
    recipient_ids: list[str],
) -> OmniResponse:
    """Create N transfer drafts splitting ``total_amount`` evenly across
    ``recipient_ids``. First draft becomes active; the rest queue.

    Each draft is a regular TransactionDraft — the existing confirm /
    cancel paths handle them. After each successful confirm the queue
    advances automatically (see ``_execute_and_record``).
    """
    if not recipient_ids:
        return OmniResponse(
            intent="transfer",
            text="Cần ít nhất 1 người để chia tiền.",
        )
    n = len(recipient_ids)
    per_person = total_amount // n
    if per_person <= 0:
        return OmniResponse(
            intent="transfer",
            text="Số tiền chia ra nhỏ hơn 0đ — kiểm tra lại nhé.",
        )

    store = get_store()
    contacts = {c.id: c for c in store.contacts_of(user_id)}
    primary = store.primary_account(user_id)
    accounts = store.get_user(user_id).accounts

    drafts: list[TransactionDraft] = []
    for cid in recipient_ids:
        contact = contacts.get(cid)
        if contact is None:
            continue
        drafts.append(
            TransactionDraft(
                id=new_id("d"),
                recipient=contact,
                candidates=[],
                source_account_id=primary.id if primary else None,
                source_accounts=accounts,
                amount=per_person,
                description=f"Chia tiền: {description}" if description else "Chia tiền",
                source_text=f"split:{description}",
                flags=[],
                requires_step_up=False,
                category="omni",
            )
        )

    if not drafts:
        return OmniResponse(
            intent="transfer",
            text="Không tìm thấy người nhận hợp lệ.",
        )

    # First draft active, rest queued.
    session = session_for(user_id)
    session.set_draft(drafts[0])
    with _drafts_lock:
        _split_queues[user_id] = drafts[1:]

    names = [d.recipient.display_name for d in drafts]  # type: ignore[union-attr]
    text = (
        f"Đã tạo {len(drafts)} yêu cầu chia tiền — "
        f"mỗi người {format_vnd(per_person)}.\n"
        f"Người nhận: {', '.join(names)}.\n"
        f"Xác nhận chuyển cho {names[0]} trước nhé."
    )
    return OmniResponse(intent="transfer", text=text, draft=drafts[0])


def cancel_draft(user_id: str, draft_id: str) -> OmniResponse:
    session = session_for(user_id)
    if session.current_draft and session.current_draft.id == draft_id:
        session.clear_draft()
    text = "Đã huỷ giao dịch."
    session.append("user", "Huỷ giao dịch")
    session.append("omni", text)
    # Audit trail — same fail-open pattern as the execute path. The
    # service module documents that cancel events belong in the log
    # but the call site was orphaned.
    try:
        from .audit_log import record_cancel
        record_cancel(user_id=user_id, draft_id=draft_id)
    except Exception:  # pragma: no cover — audit must never break chat
        pass
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
    )

    draft.recipient = chosen
    draft.candidates = []
    draft.flags = flags
    draft.requires_step_up = requires_step_up(flags)
    draft.auth_required = auth_policy(flags)
    draft.auth_completed = [a for a in draft.auth_completed if a in draft.auth_required]
    # KB3: the user just picked one of the candidate "Minh"s — fill in
    # the mini-ledger so the confirm card matches what the no-ambiguity
    # path (KB1) shows.
    draft.recent_to_recipient = _recent_to_recipient(user_id, chosen)
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

        question = f"Bạn muốn chuyển {amount_part} {recipient_part}?"
        # Offer (don't apply) a history-based figure when the amount is the
        # only thing missing. The UI renders ``suggested_amount`` as a
        # tappable chip; this line tells the user it's there.
        if has_miss_amt and draft.suggested_amount is not None:
            question += (
                f" Mình gợi ý {format_vnd(draft.suggested_amount)} dựa trên lịch sử"
                " — bấm vào gợi ý nếu đúng nhé."
            )
        return question

    warn = next(
        (f for f in draft.flags if f.severity == "warn"),
        None,
    )
    if warn is not None:
        return warn.message + " Bạn xác nhận mình mới thực hiện nhé."

    if referenced_tx is not None and draft.recipient is not None and draft.amount is not None:
        # "Lặp lại?" only makes sense when the draft amount actually matches
        # the prior transaction. If the user said "gửi mẹ 5 triệu như tháng
        # trước" but tháng trước was 3tr, asking "Lặp lại?" against a 5tr
        # draft is misleading — surface the diff and confirm the explicit
        # amount instead.
        if draft.amount == referenced_tx.amount:
            return (
                f"Tháng trước bạn gửi {format_vnd(referenced_tx.amount)} cho "
                f"{draft.recipient.display_name} ({draft.recipient.bank}). Lặp lại?"
            )
        return (
            f"Tháng trước bạn gửi {format_vnd(referenced_tx.amount)} cho "
            f"{draft.recipient.display_name} — lần này {format_vnd(draft.amount)}. "
            f"Xác nhận chuyển nhé?"
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

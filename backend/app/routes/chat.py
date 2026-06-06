from __future__ import annotations

import threading
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator

from ..context.session import session_for
from ..db import chat_log
from ..models.schemas import OmniResponse
from ..services.orchestrator import (
    begin_telemetry,
    cancel_contact_draft,
    cancel_draft,
    cancel_schedule_draft,
    clear_user_module_state,
    confirm_contact_draft,
    confirm_draft,
    confirm_schedule_draft,
    end_telemetry,
    handle_message,
    select_candidate,
    start_split_bill,
)
from ._ratelimit import enforce_user_rate_limit
from ._sanitize import sanitize_text
from .deps import current_user

# Per-field length caps. Tuned per surface:
#   * chat message — 4000 chars, comfortably above any genuine VN prompt.
#   * session id / contact id / source id — 256 chars (UUIDs are 36).
#   * rename title — 120 chars (matches the Field max_length below).
#   * otp code — 16 chars, ample for digit + separator pastes.
#   * description — 280 chars, microblog-shaped.
_MAX_MESSAGE_LEN = 4000
_MAX_ID_LEN = 256
_MAX_TITLE_LEN = 120
_MAX_OTP_LEN = 16
_MAX_DESC_LEN = 280

router = APIRouter(prefix="/api", tags=["chat"])

_LAST_SESSION_BY_USER: dict[str, str] = {}
"""Per-user record of the chat-conversation id last seen on /api/chat.

The orchestrator's ``session_for(user_id)`` is keyed by user only, so
its in-memory ``current_draft`` would otherwise persist across
*conversation* boundaries — opening a fresh chat in the left drawer
and asking "chuyển tiền cho bố" would inherit the abandoned amount
from the prior conversation's draft (user report: "tự gán số tiền
đang lưu ở trong bộ nhớ"). When the active session_id changes we
clear the draft so each conversation starts with a clean slot."""


def _enter_chat_session(user_id: str, session_id: str) -> None:
    """Clear the orchestrator's in-memory draft when switching chats.
    Called both from /api/chat (every turn) and the new-session route
    so any path that adopts a new conversation gets a fresh draft."""
    if _LAST_SESSION_BY_USER.get(user_id) != session_id:
        session_for(user_id).clear_draft()
        _LAST_SESSION_BY_USER[user_id] = session_id


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    # Which persisted conversation this turn belongs to. Optional: when
    # absent (or stale) the backend opens a fresh conversation so a
    # message is never lost. Returned to the client via the
    # `X-Chat-Session-Id` response header.
    session_id: str | None = None

    @field_validator("message")
    @classmethod
    def _clean_message(cls, v: str) -> str:
        return sanitize_text(v, max_len=_MAX_MESSAGE_LEN)

    @field_validator("session_id")
    @classmethod
    def _clean_session_id(cls, v: str | None) -> str | None:
        return None if v is None else sanitize_text(v, max_len=_MAX_ID_LEN)


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def _clean_title(cls, v: str) -> str:
        return sanitize_text(v, max_len=_MAX_TITLE_LEN)


class SelectCandidateRequest(BaseModel):
    contact_id: str
    session_id: str | None = None

    @field_validator("contact_id")
    @classmethod
    def _clean_contact_id(cls, v: str) -> str:
        return sanitize_text(v, max_len=_MAX_ID_LEN)

    @field_validator("session_id")
    @classmethod
    def _clean_session_id(cls, v: str | None) -> str | None:
        return None if v is None else sanitize_text(v, max_len=_MAX_ID_LEN)


class BiometricPose(BaseModel):
    yaw: float
    pitch: float
    roll: float
    faceCenterX: float
    faceCenterY: float


class BiometricStepResult(BaseModel):
    index: int
    target: Literal["center", "sideA", "verticalA", "sideB"]
    stableFrames: int
    detectionScore: float
    elapsedMs: int
    pose: BiometricPose
    frameSignature: int


class BiometricSample(BaseModel):
    elapsedMs: int
    detectionScore: float
    pose: BiometricPose
    frameSignature: int


class BiometricScanResult(BaseModel):
    challengeId: str
    path: Literal["clockwise", "counterClockwise"]
    requiredStableFrames: int
    startedAt: str
    finishedAt: str
    continuityBreaks: int = 0
    faceDescriptor: list[float]
    profileDescriptors: list[list[float]]
    samples: list[BiometricSample] = Field(default_factory=list)
    steps: list[BiometricStepResult]


class ConfirmTransactionRequest(BaseModel):
    otp: str | None = None
    source_account_id: str | None = None
    # Audit Bug A: there is intentionally no ``biometric_verified`` boolean
    # here. Biometric auth is gated by a real scan payload only — a
    # client-supplied "I already verified" flag is a trust-the-attacker
    # primitive even when the current ``_biometric_scan_valid`` happens to
    # reject ``None``. Future refactors must not be able to flip a bit
    # in the request body to bypass the 8D scan.
    biometric_scan: BiometricScanResult | None = None
    session_id: str | None = None

    @field_validator("otp")
    @classmethod
    def _clean_otp(cls, v: str | None) -> str | None:
        return None if v is None else sanitize_text(v, max_len=_MAX_OTP_LEN)

    @field_validator("source_account_id")
    @classmethod
    def _clean_source_account_id(cls, v: str | None) -> str | None:
        return None if v is None else sanitize_text(v, max_len=_MAX_ID_LEN)

    @field_validator("session_id")
    @classmethod
    def _clean_session_id(cls, v: str | None) -> str | None:
        return None if v is None else sanitize_text(v, max_len=_MAX_ID_LEN)


class ActionSessionRequest(BaseModel):
    session_id: str | None = None

    @field_validator("session_id")
    @classmethod
    def _clean_session_id(cls, v: str | None) -> str | None:
        return None if v is None else sanitize_text(v, max_len=_MAX_ID_LEN)


@router.post("/chat", response_model=OmniResponse)
def chat(
    req: ChatRequest,
    response: Response,
    user_id: str = Depends(current_user),
    dev: int = Query(default=0, description="Set dev=1 to populate response.telemetry"),
) -> OmniResponse:
    enforce_user_rate_limit(user_id)
    # Resolve (or open) the durable conversation this turn lands in, and
    # tell the client which one it was so a freshly-opened conversation
    # gets adopted by the UI.
    #
    # CRITICAL UX guard: when the client doesn't send a session_id
    # (very first message, header dropped by proxy, raw curl), reuse
    # the last-seen session for this user instead of minting a NEW one
    # every turn. Without this reuse, ``resolve_session(None)`` returns
    # a fresh session_id each turn → ``_enter_chat_session`` sees the
    # change → wipes the draft → user reports "context không giữ giữa
    # các turn" / "tự đặt 2tr ở turn mới rồi quên người". Sticky reuse
    # keeps the user inside the same conversation until they explicitly
    # send a different session_id (e.g. by clicking another chat in the
    # sidebar).
    if req.session_id is None:
        reuse = _LAST_SESSION_BY_USER.get(user_id)
        if reuse and chat_log.get_session(reuse, user_id) is not None:
            session_id = reuse
        else:
            session_id = chat_log.resolve_session(user_id, None)
    else:
        session_id = chat_log.resolve_session(user_id, req.session_id)
    response.headers["X-Chat-Session-Id"] = session_id
    _enter_chat_session(user_id, session_id)
    # Archive the USER turn BEFORE invoking the orchestrator. If
    # ``handle_message`` raises (LLM blew up, downstream banking error)
    # the user's typed message must still land in the sidebar — losing
    # both the reply AND the user's own message is the worst possible UX
    # (the user can't even see what they sent). Best-effort: a logging
    # failure must never break the user's transfer flow.
    try:
        chat_log.append_message(session_id, user_id, "user", req.message)
    except Exception:  # noqa: BLE001 — archival is non-critical
        pass
    if dev:
        begin_telemetry()
    try:
        resp = handle_message(user_id, req.message)
    finally:
        if dev:
            end_telemetry()
    # Archive the omni reply only on success. (If ``handle_message``
    # raised, control never reaches here — FastAPI surfaces the 500 to
    # the client, the user message is already archived above.)
    try:
        chat_log.append_message(
            session_id,
            user_id,
            "omni",
            resp.text,
            intent=resp.intent,
            response=resp.model_dump(mode="json"),
        )
    except Exception:  # noqa: BLE001 — archival is non-critical
        pass
    return resp


def _persist_action_turn(
    *,
    user_id: str,
    session_id: str | None,
    user_text: str,
    resp: OmniResponse,
) -> None:
    if not session_id:
        return
    sid = chat_log.resolve_session(user_id, session_id)
    try:
        chat_log.append_message(sid, user_id, "user", user_text)
        chat_log.append_message(
            sid,
            user_id,
            "omni",
            resp.text,
            intent=resp.intent,
            response=resp.model_dump(mode="json"),
        )
    except Exception:  # noqa: BLE001 — archival must never break banking flow
        pass


def _otp_log_label(req: ConfirmTransactionRequest | None, resp: OmniResponse) -> str:
    if req and req.biometric_scan:
        return "Xác minh sinh trắc học"
    if req and req.otp is not None:
        text = (resp.text or "").lower()
        if "otp thất bại" in text or "quá 5 lần" in text:
            return "Xác minh OTP thất bại"
        if resp.intent == "transfer" and resp.draft is None:
            return "Xác minh OTP thành công"
    return "Xác minh OTP"


# ---------------------------------------------------------------------------
# Conversation history (the left-hand sidebar)
# ---------------------------------------------------------------------------


@router.get("/chat/sessions")
def list_chat_sessions(user_id: str = Depends(current_user)) -> list[dict]:
    """All of the caller's saved conversations, newest activity first."""
    return chat_log.list_sessions(user_id)


@router.post("/chat/sessions")
def create_chat_session(user_id: str = Depends(current_user)) -> dict:
    """Open a fresh conversation and return its row."""
    row = chat_log.create_session(user_id)
    # Clear any in-flight draft so the new conversation doesn't inherit
    # the previous one's amount/recipient through the orchestrator's
    # per-user session cache.
    _enter_chat_session(user_id, row["id"])
    return row


@router.get("/chat/sessions/{session_id}")
def get_chat_session(
    session_id: str, user_id: str = Depends(current_user)
) -> dict:
    """A conversation's full ordered message list."""
    messages = chat_log.get_messages(session_id, user_id)
    if messages is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc trò chuyện")
    return {"id": session_id, "messages": messages}


@router.patch("/chat/sessions/{session_id}")
def rename_chat_session(
    session_id: str,
    req: RenameSessionRequest,
    user_id: str = Depends(current_user),
) -> dict:
    if not chat_log.rename_session(session_id, user_id, req.title):
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc trò chuyện")
    return {"ok": True}


@router.delete("/chat/sessions/{session_id}")
def delete_chat_session(
    session_id: str, user_id: str = Depends(current_user)
) -> dict:
    if not chat_log.delete_session(session_id, user_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc trò chuyện")
    return {"ok": True}


from collections import OrderedDict as _OD
_CONFIRMED_DRAFTS_MAX = 4096
_CONFIRMED_DRAFT_RESPONSES: "_OD[str, OmniResponse]" = _OD()
_CONFIRMED_DRAFT_LOCK = threading.Lock()
"""Idempotency cache: ``{user_id}:{draft_id}`` → first successful response.

Prevents double-fire from double-clicks / network retries on the
confirm button. Each draft_id is consumed once then never reused
(orchestrator clears the session draft after confirm), but a process
that lives for weeks across thousands of users would still accumulate
entries. ``OrderedDict`` + bounded eviction caps it at the most recent
4096 confirmed drafts; the lock makes the eviction thread-safe so
concurrent confirms can't corrupt the dict ordering."""


def _confirmed_get(key: str) -> "OmniResponse | None":
    with _CONFIRMED_DRAFT_LOCK:
        resp = _CONFIRMED_DRAFT_RESPONSES.get(key)
        if resp is not None:
            _CONFIRMED_DRAFT_RESPONSES.move_to_end(key)
        return resp


def _confirmed_set(key: str, resp: "OmniResponse") -> None:
    with _CONFIRMED_DRAFT_LOCK:
        _CONFIRMED_DRAFT_RESPONSES[key] = resp
        if len(_CONFIRMED_DRAFT_RESPONSES) > _CONFIRMED_DRAFTS_MAX:
            _CONFIRMED_DRAFT_RESPONSES.popitem(last=False)

_INFLIGHT_CONFIRMS: set[str] = set()
"""Draft cache keys whose confirm handler is currently executing.

Closes the race documented in user feedback "nhập opt rồi nhấn huỷ
nhưng mà sao vẫn chuyển?": user clicks confirm, transfer starts,
user clicks cancel before the response arrives. Without this guard
the cancel endpoint would clear the session while ``confirm_draft``
is mid-execute — the transfer has already been written but the UI
shows "đã huỷ", which is worse than either pure outcome.

With this guard the cancel arm returns a polite "đang xử lý" notice
so the user sees the transfer either complete or fail on its own.
The frontend's matching ``inFlightDraftIds`` set already disables
the Huỷ button — this is a server-side belt for the same braces."""


# ---------------------------------------------------------------------------
# Audit A: per-(user, draft) lock + OTP-attempt counter on the confirm route.
# ---------------------------------------------------------------------------
#
# Bug A — double-debit race:
#   The previous implementation only did `set.add(cache_key)` and never
#   CHECKED membership before invoking ``confirm_draft``. Two concurrent
#   POSTs to /api/transactions/{draft_id}/confirm therefore both passed
#   the idempotency cache (cold) AND both entered the inflight set, both
#   called confirm_draft → execute_transfer → two debits on the same
#   draft. The SQLite schema has no ``CHECK(balance>=0)`` so the account
#   silently went negative.
#
#   Fix: a per-(user, draft) ``threading.Lock`` held around the entire
#   read-cache → run-confirm → write-cache sequence. The first caller
#   takes the lock and writes the idempotency entry on its way out;
#   subsequent concurrent callers block on the lock, then find the
#   cached response on re-entry and replay it.
#
# Bug B — OTP brute-force:
#   No per-draft attempt counter at the HTTP layer and no rate limit on
#   the confirm route meant a scripted client could spam 000000..999999
#   against an open draft. Add a bounded per-(user,draft) counter that
#   trips at 3 failed OTP attempts, cancels the draft, and returns a
#   polite Vietnamese lock notice. Also wire ``enforce_user_rate_limit``
#   so the bucket caps overall confirm-endpoint throughput per user.

_CONFIRM_LOCKS_MAX = 1024
_CONFIRM_LOCKS: "_OD[str, threading.Lock]" = _OD()
_CONFIRM_LOCKS_GUARD = threading.Lock()

_OTP_ATTEMPTS_MAX = 1024
_OTP_ATTEMPTS: "_OD[str, int]" = _OD()
_OTP_ATTEMPTS_GUARD = threading.Lock()
_OTP_MAX_ATTEMPTS = 3


def _confirm_lock(cache_key: str) -> threading.Lock:
    """Acquire (or create) a per-(user, draft) lock used to serialise
    concurrent confirm calls. Bounded LRU so a long-lived process can't
    accumulate one lock per unique cache_key forever."""
    with _CONFIRM_LOCKS_GUARD:
        lock = _CONFIRM_LOCKS.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _CONFIRM_LOCKS[cache_key] = lock
            if len(_CONFIRM_LOCKS) > _CONFIRM_LOCKS_MAX:
                _CONFIRM_LOCKS.popitem(last=False)
        else:
            _CONFIRM_LOCKS.move_to_end(cache_key)
        return lock


def _otp_attempts_get(cache_key: str) -> int:
    with _OTP_ATTEMPTS_GUARD:
        return _OTP_ATTEMPTS.get(cache_key, 0)


def _otp_attempts_inc(cache_key: str) -> int:
    with _OTP_ATTEMPTS_GUARD:
        n = _OTP_ATTEMPTS.get(cache_key, 0) + 1
        _OTP_ATTEMPTS[cache_key] = n
        _OTP_ATTEMPTS.move_to_end(cache_key)
        if len(_OTP_ATTEMPTS) > _OTP_ATTEMPTS_MAX:
            _OTP_ATTEMPTS.popitem(last=False)
        return n


def _otp_attempts_reset(cache_key: str) -> None:
    with _OTP_ATTEMPTS_GUARD:
        _OTP_ATTEMPTS.pop(cache_key, None)


@router.post("/transactions/{draft_id}/confirm", response_model=OmniResponse)
def confirm(
    draft_id: str,
    req: ConfirmTransactionRequest | None = None,
    user_id: str = Depends(current_user),
) -> OmniResponse:
    # Cheap front-line throttle: stops scripted clients from hammering
    # /confirm with one OTP guess per request. Pairs with the per-draft
    # attempt counter below — defence in depth.
    enforce_user_rate_limit(user_id)

    cache_key = f"{user_id}:{draft_id}"

    # Brute-force lock: a draft that has already burned its OTP budget
    # stays locked even on retries. Cancel the underlying draft so the
    # user must start a new flow.
    if _otp_attempts_get(cache_key) >= _OTP_MAX_ATTEMPTS:
        try:
            cancel_draft(user_id, draft_id)
        except Exception:  # pragma: no cover — best effort
            pass
        locked = OmniResponse(
            intent="transfer",
            text=(
                "Quá nhiều lần thử sai OTP, giao dịch bị khoá. "
                "Bạn vui lòng tạo lại lệnh chuyển tiền nhé."
            ),
        )
        _persist_action_turn(
            user_id=user_id,
            session_id=req.session_id if req else None,
            user_text="Xác minh OTP",
            resp=locked,
        )
        return locked

    # Per-(user, draft) mutex: serialise the entire read-cache →
    # confirm_draft → write-cache window so two concurrent POSTs cannot
    # both pass the cold cache check, both call ``execute_transfer``,
    # and double-debit the source account. The second caller blocks
    # here, then exits the critical section seeing the idempotency
    # cache populated and replays the first caller's response.
    with _confirm_lock(cache_key):
        # Idempotency: if this user already successfully confirmed this draft,
        # replay the cached response instead of re-executing the transfer.
        # Stops the demo-classic "user double-clicked → two debits" failure.
        cached = _confirmed_get(cache_key)
        if cached is not None:
            _persist_action_turn(
                user_id=user_id,
                session_id=req.session_id if req else None,
                user_text=_otp_log_label(req, cached),
                resp=cached,
            )
            return cached

        # Biometric face-scan auth: risky transfers require OTP + an 8D face
        # scan. The frontend sends both together in the confirm body; the
        # orchestrator verifies each method independently (see confirm_draft).
        # The _INFLIGHT_CONFIRMS guard makes the endpoint idempotent against
        # double-click races (commit 94f6443 — verifier audit A2).
        _INFLIGHT_CONFIRMS.add(cache_key)
        try:
            resp = confirm_draft(
                user_id,
                draft_id,
                otp=req.otp if req else None,
                source_account_id=req.source_account_id if req else None,
                biometric_scan=req.biometric_scan.dict() if req and req.biometric_scan else None,
            )
        finally:
            _INFLIGHT_CONFIRMS.discard(cache_key)
        if resp.intent == "unknown":
            raise HTTPException(status_code=404, detail=resp.text)

        # OTP attempt accounting. ``resp.draft.awaiting_otp == True`` after
        # a confirm with an ``otp`` arg means the orchestrator rejected the
        # supplied code (wrong / empty / missing); count it as a failed
        # attempt at the HTTP layer too so a brute-force loop trips the
        # lock at 3 even if the orchestrator's per-draft counter is reset
        # by a session swap.
        otp_supplied = bool(req and req.otp is not None)
        wrong_otp = (
            otp_supplied
            and resp.draft is not None
            and getattr(resp.draft, "awaiting_otp", False)
        )
        if wrong_otp:
            attempts = _otp_attempts_inc(cache_key)
            if attempts >= _OTP_MAX_ATTEMPTS:
                try:
                    cancel_draft(user_id, draft_id)
                except Exception:  # pragma: no cover
                    pass
                resp = OmniResponse(
                    intent="transfer",
                    text=(
                        "Quá nhiều lần thử sai OTP, giao dịch bị khoá. "
                        "Bạn vui lòng tạo lại lệnh chuyển tiền nhé."
                    ),
                )
        _persist_action_turn(
            user_id=user_id,
            session_id=req.session_id if req else None,
            user_text=_otp_log_label(req, resp),
            resp=resp,
        )
        # Only cache fully-confirmed transfers — OTP prompts / re-confirm
        # branches still need to be replayable for new input. Heuristic:
        # cache when the response carries no draft (transfer landed) or
        # the draft has no ``awaiting_otp`` flag.
        if resp.draft is None or not getattr(resp.draft, "awaiting_otp", False):
            _confirmed_set(cache_key, resp)
            # Successful terminal state — clear the attempt counter so a
            # future draft with a colliding key (extraordinarily rare,
            # but possible) doesn't inherit stale failures.
            _otp_attempts_reset(cache_key)
        return resp


@router.post("/transactions/{draft_id}/cancel", response_model=OmniResponse)
def cancel(
    draft_id: str,
    req: ActionSessionRequest | None = None,
    user_id: str = Depends(current_user),
) -> OmniResponse:
    cache_key = f"{user_id}:{draft_id}"
    # Confirm is mid-execute — refuse to clear the session under it.
    if cache_key in _INFLIGHT_CONFIRMS:
        resp = OmniResponse(
            intent="transfer",
            text="Giao dịch đang được xử lý, không thể huỷ ở bước này.",
        )
        _persist_action_turn(
            user_id=user_id,
            session_id=req.session_id if req else None,
            user_text="Huỷ",
            resp=resp,
        )
        return resp
    # Confirm already finished — replay the idempotent confirm response
    # instead of pretending the cancel succeeded.
    cached = _confirmed_get(cache_key)
    if cached is not None:
        _persist_action_turn(
            user_id=user_id,
            session_id=req.session_id if req else None,
            user_text="Huỷ",
            resp=cached,
        )
        return cached
    resp = cancel_draft(user_id, draft_id)
    _persist_action_turn(
        user_id=user_id,
        session_id=req.session_id if req else None,
        user_text="Huỷ",
        resp=resp,
    )
    return resp


class SplitBillRequest(BaseModel):
    total_amount: int = Field(gt=0)
    description: str = ""
    recipient_ids: list[str] = Field(min_length=1, max_length=10)

    @field_validator("description")
    @classmethod
    def _clean_description(cls, v: str) -> str:
        # Description is optional and may be empty — sanitize only when
        # non-empty so an empty string doesn't trip the "no content" branch.
        if not v:
            return ""
        try:
            return sanitize_text(v, max_len=_MAX_DESC_LEN)
        except ValueError:
            return ""

    @field_validator("recipient_ids")
    @classmethod
    def _clean_recipient_ids(cls, v: list[str]) -> list[str]:
        return [sanitize_text(x, max_len=_MAX_ID_LEN) for x in v]


@router.post("/transactions/split", response_model=OmniResponse)
def split_bill(
    req: SplitBillRequest,
    user_id: str = Depends(current_user),
) -> OmniResponse:
    """Create N split-share drafts from a confirmed receipt. First draft
    becomes active in the session; the rest queue. Each successful
    confirm pops the next from the queue and surfaces it to the user.
    """
    enforce_user_rate_limit(user_id)
    return start_split_bill(
        user_id,
        total_amount=req.total_amount,
        description=req.description,
        recipient_ids=req.recipient_ids,
    )


@router.post("/transactions/{draft_id}/select", response_model=OmniResponse)
def select(
    draft_id: str,
    req: SelectCandidateRequest,
    user_id: str = Depends(current_user),
) -> OmniResponse:
    resp = select_candidate(user_id, draft_id, req.contact_id)
    if resp.intent == "unknown":
        fallback = OmniResponse(
            intent="transfer",
            text=(
                "Phiên giao dịch vừa được làm mới. Bạn nhập lại câu chuyển tiền giúp mình nhé."
            ),
        )
        _persist_action_turn(
            user_id=user_id,
            session_id=req.session_id,
            user_text="Chọn người nhận",
            resp=fallback,
        )
        return fallback
    recipient_name = resp.draft.recipient.display_name if resp.draft and resp.draft.recipient else "người nhận"
    _persist_action_turn(
        user_id=user_id,
        session_id=req.session_id,
        user_text=f"Chọn {recipient_name}",
        resp=resp,
    )
    return resp


@router.post("/contacts/{draft_id}/confirm", response_model=OmniResponse)
def confirm_contact(draft_id: str, user_id: str = Depends(current_user)) -> OmniResponse:
    resp = confirm_contact_draft(user_id, draft_id)
    if resp.intent == "unknown":
        raise HTTPException(status_code=404, detail=resp.text)
    return resp


@router.post("/contacts/{draft_id}/cancel", response_model=OmniResponse)
def cancel_contact(draft_id: str, user_id: str = Depends(current_user)) -> OmniResponse:
    return cancel_contact_draft(user_id, draft_id)


@router.post("/schedules/{draft_id}/confirm", response_model=OmniResponse)
def confirm_schedule(
    draft_id: str,
    req: ConfirmTransactionRequest | None = None,
    user_id: str = Depends(current_user),
) -> OmniResponse:
    resp = confirm_schedule_draft(
        user_id,
        draft_id,
        otp=req.otp if req else None,
        source_account_id=req.source_account_id if req else None,
    )
    if resp.intent == "unknown":
        raise HTTPException(status_code=404, detail=resp.text)
    return resp


@router.post("/schedules/{draft_id}/cancel", response_model=OmniResponse)
def cancel_schedule(draft_id: str, user_id: str = Depends(current_user)) -> OmniResponse:
    return cancel_schedule_draft(user_id, draft_id)


@router.post("/session/reset")
def reset_session(user_id: str = Depends(current_user)) -> dict:
    """Clear all in-flight drafts and conversation history for the caller.

    Intended for testing and as the 'fresh chat' button — not exposed in
    the UI but harmless if hit accidentally.

    Safety contract (stress-agent A regression, "wrong-recipient silent
    confirm"): every per-user piece of state that a subsequent
    confirm/cancel/OTP token could land on MUST be wiped here. If even
    one slot survives, a future bare "ok" turn can dispatch the prior
    flow's draft — including an awaiting-OTP transfer to the wrong
    recipient at the wrong amount.

    Slots reset:
      * Session drafts (transfer / contact / schedule) — including drafts
        already in ``awaiting_otp=True`` state. ``clear_draft`` drops the
        whole serialized blob, so the OTP flag goes with it.
      * Orchestrator module dicts (budget draft / goal draft / pending
        split-bill queue) — these live outside the session backend and
        ``Session.clear_*`` doesn't touch them.
      * Idempotency / OTP-attempt caches in this module that key on
        ``f"{user_id}:{draft_id}"`` — after reset, any new draft uses a
        fresh UUID, but we also drop everything user-scoped so a stale
        "already-confirmed" replay can't echo back to a new session.
      * Conversation history at the backend layer. The previous code
        wrote ``s.history.clear()``, which mutated the *list copy*
        returned by the ``history`` property and never reached the
        backend; the new ``clear_history()`` helper writes an empty
        list through ``set_history``.
      * The ``_LAST_SESSION_BY_USER`` marker so the next /api/chat call
        re-enters fresh — preserving it would only matter for chat
        archival, but resetting it costs nothing and keeps the surface
        clean.
    """
    s = session_for(user_id)
    # current_* are read-only properties after the Redis-sessions refactor;
    # use the clear_* methods instead so this works for memory/redis/fake-redis.
    s.clear_draft()
    s.clear_contact_draft()
    s.clear_schedule_draft()
    # Backend-level history wipe (was: s.history.clear() — a no-op against
    # the copy returned by the property).
    s.clear_history()

    # Orchestrator module-level dicts: budget draft / goal draft / split queue.
    clear_user_module_state(user_id)

    # Drop any cached confirm response / inflight / OTP-attempt counters
    # for this user so a new draft cannot inherit them by accident.
    prefix = f"{user_id}:"
    with _CONFIRMED_DRAFT_LOCK:
        stale_confirmed = [k for k in _CONFIRMED_DRAFT_RESPONSES if k.startswith(prefix)]
        for k in stale_confirmed:
            _CONFIRMED_DRAFT_RESPONSES.pop(k, None)
    with _OTP_ATTEMPTS_GUARD:
        stale_otp = [k for k in _OTP_ATTEMPTS if k.startswith(prefix)]
        for k in stale_otp:
            _OTP_ATTEMPTS.pop(k, None)
    with _CONFIRM_LOCKS_GUARD:
        stale_locks = [k for k in _CONFIRM_LOCKS if k.startswith(prefix)]
        for k in stale_locks:
            _CONFIRM_LOCKS.pop(k, None)
    # Inflight set isn't lock-guarded but a discard is safe — we only
    # remove entries that belong to this user.
    stale_inflight = [k for k in list(_INFLIGHT_CONFIRMS) if k.startswith(prefix)]
    for k in stale_inflight:
        _INFLIGHT_CONFIRMS.discard(k)

    # Forget the last-seen conversation marker so the next /api/chat
    # opens a clean slot for this user.
    _LAST_SESSION_BY_USER.pop(user_id, None)

    return {"ok": True, "user_id": user_id}

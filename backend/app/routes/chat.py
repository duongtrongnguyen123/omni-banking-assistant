from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from ..context.session import session_for
from ..db import chat_log
from ..models.schemas import OmniResponse
from ..services.orchestrator import (
    begin_telemetry,
    cancel_contact_draft,
    cancel_draft,
    cancel_schedule_draft,
    confirm_contact_draft,
    confirm_draft,
    confirm_schedule_draft,
    end_telemetry,
    handle_message,
    select_candidate,
    start_split_bill,
)
from ._ratelimit import enforce_user_rate_limit
from .deps import current_user

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    # Which persisted conversation this turn belongs to. Optional: when
    # absent (or stale) the backend opens a fresh conversation so a
    # message is never lost. Returned to the client via the
    # `X-Chat-Session-Id` response header.
    session_id: str | None = None


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class SelectCandidateRequest(BaseModel):
    contact_id: str


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
    biometric_verified: bool = False
    biometric_scan: BiometricScanResult | None = None


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
    session_id = chat_log.resolve_session(user_id, req.session_id)
    response.headers["X-Chat-Session-Id"] = session_id
    if dev:
        begin_telemetry()
    try:
        resp = handle_message(user_id, req.message)
    finally:
        if dev:
            end_telemetry()
    # Persist both sides of the turn. Best-effort: a logging failure must
    # never break the user's transfer flow.
    try:
        chat_log.append_message(session_id, user_id, "user", req.message)
        chat_log.append_message(
            session_id, user_id, "omni", resp.text, intent=resp.intent
        )
    except Exception:  # noqa: BLE001 — archival is non-critical
        pass
    return resp


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
    return chat_log.create_session(user_id)


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


_CONFIRMED_DRAFT_RESPONSES: dict[str, OmniResponse] = {}
"""Idempotency cache: ``{user_id}:{draft_id}`` → first successful response.

Prevents double-fire from double-clicks / network retries on the
confirm button. Bounded by the natural draft lifetime — each draft_id
is consumed once then never reused (orchestrator clears the session
draft after confirm), so the cache stays small in practice. A wider
LRU bound is in TODO but not load-bearing for the demo."""

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


@router.post("/transactions/{draft_id}/confirm", response_model=OmniResponse)
def confirm(
    draft_id: str,
    req: ConfirmTransactionRequest | None = None,
    user_id: str = Depends(current_user),
) -> OmniResponse:
    # Idempotency: if this user already successfully confirmed this draft,
    # replay the cached response instead of re-executing the transfer.
    # Stops the demo-classic "user double-clicked → two debits" failure.
    cache_key = f"{user_id}:{draft_id}"
    cached = _CONFIRMED_DRAFT_RESPONSES.get(cache_key)
    if cached is not None:
        return cached

    # Biometric params are part of ConfirmTransactionRequest (kept from
    # origin/main wire schema) but the orchestrator's confirm_draft
    # doesn't consume them yet — they'll be wired when we land the
    # face-scan auth layer. For now they're accepted-and-ignored so the
    # frontend can ship the wire without backend failure.
    _INFLIGHT_CONFIRMS.add(cache_key)
    try:
        resp = confirm_draft(
            user_id,
            draft_id,
            otp=req.otp if req else None,
            source_account_id=req.source_account_id if req else None,
        )
    finally:
        _INFLIGHT_CONFIRMS.discard(cache_key)
    if resp.intent == "unknown":
        raise HTTPException(status_code=404, detail=resp.text)
    # Only cache fully-confirmed transfers — OTP prompts / re-confirm
    # branches still need to be replayable for new input. Heuristic:
    # cache when the response carries no draft (transfer landed) or
    # the draft has no ``awaiting_otp`` flag.
    if resp.draft is None or not getattr(resp.draft, "awaiting_otp", False):
        _CONFIRMED_DRAFT_RESPONSES[cache_key] = resp
    return resp


@router.post("/transactions/{draft_id}/cancel", response_model=OmniResponse)
def cancel(draft_id: str, user_id: str = Depends(current_user)) -> OmniResponse:
    cache_key = f"{user_id}:{draft_id}"
    # Confirm is mid-execute — refuse to clear the session under it.
    if cache_key in _INFLIGHT_CONFIRMS:
        return OmniResponse(
            intent="transfer",
            text="Giao dịch đang được xử lý, không thể huỷ ở bước này.",
        )
    # Confirm already finished — replay the idempotent confirm response
    # instead of pretending the cancel succeeded.
    cached = _CONFIRMED_DRAFT_RESPONSES.get(cache_key)
    if cached is not None:
        return cached
    return cancel_draft(user_id, draft_id)


class SplitBillRequest(BaseModel):
    total_amount: int = Field(gt=0)
    description: str = ""
    recipient_ids: list[str] = Field(min_length=1, max_length=10)


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
        return OmniResponse(
            intent="transfer",
            text=(
                "Phiên giao dịch vừa được làm mới. Bạn nhập lại câu chuyển tiền giúp mình nhé."
            ),
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
    the UI but harmless if hit accidentally."""
    s = session_for(user_id)
    # current_* are read-only properties after the Redis-sessions refactor;
    # use the clear_* methods instead so this works for memory/redis/fake-redis.
    s.clear_draft()
    s.clear_contact_draft()
    s.clear_schedule_draft()
    s.history.clear()
    return {"ok": True, "user_id": user_id}

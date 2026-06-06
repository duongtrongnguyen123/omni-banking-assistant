from __future__ import annotations

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


class ConfirmTransactionRequest(BaseModel):
    otp: str | None = None
    source_account_id: str | None = None


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


@router.post("/transactions/{draft_id}/confirm", response_model=OmniResponse)
def confirm(
    draft_id: str,
    req: ConfirmTransactionRequest | None = None,
    user_id: str = Depends(current_user),
) -> OmniResponse:
    resp = confirm_draft(
        user_id,
        draft_id,
        otp=req.otp if req else None,
        source_account_id=req.source_account_id if req else None,
    )
    if resp.intent == "unknown":
        raise HTTPException(status_code=404, detail=resp.text)
    return resp


@router.post("/transactions/{draft_id}/cancel", response_model=OmniResponse)
def cancel(draft_id: str, user_id: str = Depends(current_user)) -> OmniResponse:
    return cancel_draft(user_id, draft_id)


@router.post("/transactions/{draft_id}/select", response_model=OmniResponse)
def select(
    draft_id: str,
    req: SelectCandidateRequest,
    user_id: str = Depends(current_user),
) -> OmniResponse:
    resp = select_candidate(user_id, draft_id, req.contact_id)
    if resp.intent == "unknown":
        raise HTTPException(status_code=404, detail=resp.text)
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

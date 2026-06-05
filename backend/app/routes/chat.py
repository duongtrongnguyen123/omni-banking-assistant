from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from ..models.schemas import OmniResponse
from ..context.session import session_for
from ..services.orchestrator import (
    cancel_contact_draft,
    cancel_draft,
    cancel_schedule_draft,
    confirm_contact_draft,
    confirm_draft,
    confirm_schedule_draft,
    handle_message,
    select_candidate,
)
from ..services.responses import detect_lang, t as response_t
from .deps import current_user


# Known-template substitutions. Whenever the orchestrator's deterministic
# templates produce one of these Vietnamese strings, we swap the EN
# equivalent in before returning. This keeps the LLM-phrased path
# untouched — only the safety-critical, hand-built lines get translated.
#
# Kept tiny (top-10 visible lines) — the language pill in the UI calls
# the shots, the actual transfer execution remains data-driven.
_KNOWN_VI_TO_KEY = {
    "Đã huỷ giao dịch.": "transfer_cancelled",
    "Đã huỷ đặt lịch.": "schedule_cancelled",
    "Vui lòng nhập OTP để xác minh giao dịch. Mã demo: 123456.": "otp_prompt",
    "OTP chưa đúng. Bạn kiểm tra và nhập lại mã xác minh nhé.": "otp_failed",
    (
        "Mình chưa rõ ý bạn. Bạn thử nói cụ thể hơn nhé — ví dụ "
        "\"chuyển cho mẹ 2 triệu\" hoặc \"tháng này tiêu bao nhiêu?\""
    ): "unknown_fallback",
}


def _translate(resp: OmniResponse, lang: str) -> OmniResponse:
    if lang != "en":
        return resp
    key = _KNOWN_VI_TO_KEY.get(resp.text)
    if key:
        resp.text = response_t(key, lang)
    return resp

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)


class SelectCandidateRequest(BaseModel):
    contact_id: str


class ConfirmTransactionRequest(BaseModel):
    otp: str | None = None
    source_account_id: str | None = None
    biometric_verified: bool = False


@router.post("/chat", response_model=OmniResponse)
def chat(
    req: ChatRequest,
    user_id: str = Depends(current_user),
    accept_language: Optional[str] = Header(default=None, alias="accept-language"),
    lang: Optional[str] = Query(default=None),
) -> OmniResponse:
    resolved = detect_lang(accept_language=accept_language, query_lang=lang)
    return _translate(handle_message(user_id, req.message), resolved)


@router.post("/transactions/{draft_id}/confirm", response_model=OmniResponse)
def confirm(
    draft_id: str,
    req: ConfirmTransactionRequest | None = None,
    user_id: str = Depends(current_user),
    accept_language: Optional[str] = Header(default=None, alias="accept-language"),
    lang: Optional[str] = Query(default=None),
) -> OmniResponse:
    resp = confirm_draft(
        user_id,
        draft_id,
        otp=req.otp if req else None,
        source_account_id=req.source_account_id if req else None,
        biometric_verified=req.biometric_verified if req else False,
    )
    if resp.intent == "unknown":
        raise HTTPException(status_code=404, detail=resp.text)
    return _translate(resp, detect_lang(accept_language=accept_language, query_lang=lang))


@router.post("/transactions/{draft_id}/cancel", response_model=OmniResponse)
def cancel(
    draft_id: str,
    user_id: str = Depends(current_user),
    accept_language: Optional[str] = Header(default=None, alias="accept-language"),
    lang: Optional[str] = Query(default=None),
) -> OmniResponse:
    return _translate(
        cancel_draft(user_id, draft_id),
        detect_lang(accept_language=accept_language, query_lang=lang),
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
    s.current_draft = None
    s.current_contact_draft = None
    s.current_schedule_draft = None
    s.history.clear()
    return {"ok": True, "user_id": user_id}

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..models.schemas import OmniResponse
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
from .deps import current_user

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
def chat(req: ChatRequest, user_id: str = Depends(current_user)) -> OmniResponse:
    return handle_message(user_id, req.message)


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
        biometric_verified=req.biometric_verified if req else False,
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

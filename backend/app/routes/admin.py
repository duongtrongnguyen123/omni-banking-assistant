"""Admin / observability endpoints.

These are non-user-facing routes used by the demo UI and by judges
inspecting the runtime trust contract. They expose the privacy mode
toggle and the outbound LLM payload audit ring buffer.

Privacy mode is a process-wide knob (see ``app.nlp.privacy``); changes
take effect on the next outbound LLM call. The audit buffer is FIFO,
capped at 100 entries.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..nlp import privacy

router = APIRouter(prefix="/api/admin", tags=["admin"])


class PrivacyModeBody(BaseModel):
    mode: Literal["off", "redact", "local-only"]


class PrivacyModeResponse(BaseModel):
    mode: str
    description: str


_MODE_DESCRIPTIONS = {
    "off": "Không lọc PII trước khi gửi câu hỏi tới LLM.",
    "redact": "Mọi câu hỏi gửi tới LLM đều được lọc PII trên máy trước khi rời máy chủ.",
    "local-only": "Tắt hoàn toàn LLM bên thứ ba — chỉ dùng rule-based extractor.",
}


@router.get("/privacy-mode", response_model=PrivacyModeResponse)
def get_privacy_mode() -> PrivacyModeResponse:
    mode = privacy.get_mode()
    return PrivacyModeResponse(mode=mode, description=_MODE_DESCRIPTIONS[mode])


@router.post("/privacy-mode", response_model=PrivacyModeResponse)
def set_privacy_mode(body: PrivacyModeBody) -> PrivacyModeResponse:
    try:
        mode = privacy.set_mode(body.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return PrivacyModeResponse(mode=mode, description=_MODE_DESCRIPTIONS[mode])


class LLMAuditEntry(BaseModel):
    seq: int
    ts: float
    provider: str
    mode: str
    original_size: int
    redacted_size: int
    redaction_count: int
    redaction_breakdown: dict
    suppressed: bool
    note: Optional[str] = None


class LLMAuditResponse(BaseModel):
    mode: str
    capacity: int
    count: int
    entries: list[LLMAuditEntry]


@router.get("/llm-audit", response_model=LLMAuditResponse)
def get_llm_audit(limit: int = 100) -> LLMAuditResponse:
    entries = privacy.recent_audit(limit=limit)
    return LLMAuditResponse(
        mode=privacy.get_mode(),
        capacity=100,
        count=len(entries),
        entries=[LLMAuditEntry(**e) for e in entries],
    )


__all__ = ["router"]

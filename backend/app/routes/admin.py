"""Admin / observability endpoints.

These are non-user-facing routes used by the demo UI and by judges
inspecting the runtime trust contract. They expose the privacy mode
toggle and the outbound LLM payload audit ring buffer.

Privacy mode is a process-wide knob (see ``app.nlp.privacy``); changes
take effect on the next outbound LLM call. The audit buffer is FIFO,
capped at 100 entries.
"""

from __future__ import annotations

import os
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from ..nlp import privacy


def require_admin(authorization: Optional[str] = Header(default=None)) -> None:
    """Gate every ``/api/admin/*`` route behind a shared-secret bearer token.

    Behavior:

    * ``OMNI_ADMIN_TOKEN`` unset → open (demo mode). Documented in
      ``docs/admin-auth.md`` so judges and operators know the trust
      contract before deploying.
    * ``OMNI_ADMIN_TOKEN`` set → request must send
      ``Authorization: Bearer <token>`` with an exact-match value.
      Mismatch → 401. Missing header → 401.

    The token is compared in constant time to avoid timing oracles even
    though the demo doesn't realistically need it — habit hardening.
    """
    expected = os.environ.get("OMNI_ADMIN_TOKEN", "").strip()
    if not expected:
        return  # demo mode
    header = (authorization or "").strip()
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Thiếu Authorization Bearer token")
    supplied = header.split(None, 1)[1].strip()
    # constant-time comparison
    if len(supplied) != len(expected):
        raise HTTPException(status_code=401, detail="Token không hợp lệ")
    ok = 0
    for a, b in zip(supplied.encode(), expected.encode()):
        ok |= a ^ b
    if ok != 0:
        raise HTTPException(status_code=401, detail="Token không hợp lệ")


router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


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


# ---------------------------------------------------------------------------
# A/B framework for the next-recipient suggester. See ``app/ml/abtest.py``
# for the routing model and ``app/ml/bandit.py`` for the Thompson-sampling
# upgrade path.
# ---------------------------------------------------------------------------


class AbTestReport(BaseModel):
    enabled: bool
    min_trials_per_arm: int
    bandit_active: bool
    arms: dict


@router.get("/abtest/report", response_model=AbTestReport)
def get_abtest_report() -> AbTestReport:
    """Per-arm trials / hits / hit_rate / 95 % CI / weights.

    ``bandit_active`` is True once every arm has ≥ ``MIN_TRIALS_PER_ARM``
    trials and the router has switched from deterministic-hash routing to
    Thompson sampling.
    """
    from ..ml import abtest, bandit

    rep = abtest.report()
    bandit_on = bool(rep) and all(
        a["trials"] >= bandit.MIN_TRIALS_PER_ARM for a in rep.values()
    )
    return AbTestReport(
        enabled=abtest.is_enabled(),
        min_trials_per_arm=bandit.MIN_TRIALS_PER_ARM,
        bandit_active=bandit_on,
        arms=rep,
    )


@router.post("/abtest/reset")
def reset_abtest() -> dict:
    """Clear all trial / hit counters and the persisted Beta posteriors.

    Intended for the demo dashboard reset button and for the eval
    script. Returns the cleared arm list for confirmation.
    """
    from ..ml import abtest

    abtest.reset()
    return {"ok": True, "arms": abtest.arm_names()}


__all__ = ["router"]

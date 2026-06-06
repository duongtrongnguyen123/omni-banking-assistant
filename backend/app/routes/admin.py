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

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from ..config import get_settings
from ..db import chat_log
from ..db.connection import get_connection
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


class AdminChatSession(BaseModel):
    id: str
    user_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    preview: Optional[str] = ""
    intents: list[str] = []


class AdminChatSessionsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    sessions: list[AdminChatSession]


class AdminChatMessage(BaseModel):
    id: str
    user_id: str
    role: str
    content: str
    intent: Optional[str] = None
    response: Optional[dict] = None
    created_at: str


class AdminChatSessionDetail(BaseModel):
    id: str
    user_id: str
    title: str
    created_at: str
    updated_at: str
    messages: list[AdminChatMessage]


class AdminTransactionRow(BaseModel):
    id: str
    user_id: str
    contact_id: Optional[str] = None
    recipient_name: Optional[str] = None
    recipient_bank: Optional[str] = None
    recipient_account_masked: Optional[str] = None
    amount: int
    description: str
    category: str
    status: str
    created_at: str
    auth_methods: list[str] = Field(default_factory=list)
    kyc_level: Optional[str] = None
    daily_limit_vnd: Optional[int] = None
    daily_total_before_vnd: Optional[int] = None
    retention_until: Optional[str] = None


class AdminTransactionsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    retention_years: int
    transactions: list[AdminTransactionRow]


class RetentionPolicyResponse(BaseModel):
    transaction_retention_years: int
    scope: list[str]
    note: str


@router.get("/llm-audit", response_model=LLMAuditResponse)
def get_llm_audit(limit: int = 100) -> LLMAuditResponse:
    entries = privacy.recent_audit(limit=limit)
    return LLMAuditResponse(
        mode=privacy.get_mode(),
        capacity=100,
        count=len(entries),
        entries=[LLMAuditEntry(**e) for e in entries],
    )


@router.get("/chat/sessions", response_model=AdminChatSessionsResponse)
def admin_chat_sessions(
    user_id: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None, max_length=120),
    intent: Optional[str] = Query(default=None, max_length=40),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> AdminChatSessionsResponse:
    """Read-only conversation archive for support/admin review."""
    sessions = chat_log.admin_list_sessions(
        user_id=user_id,
        q=q,
        intent=intent,
        limit=limit,
        offset=offset,
    )
    total = chat_log.admin_count_sessions(user_id=user_id, q=q, intent=intent)
    return AdminChatSessionsResponse(
        total=total,
        limit=limit,
        offset=offset,
        sessions=[AdminChatSession(**s) for s in sessions],
    )


@router.get("/chat/sessions/{session_id}", response_model=AdminChatSessionDetail)
def admin_chat_session_detail(session_id: str) -> AdminChatSessionDetail:
    session = chat_log.admin_get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc trò chuyện")
    messages = chat_log.admin_get_messages(session_id) or []
    return AdminChatSessionDetail(
        **session,
        messages=[AdminChatMessage(**m) for m in messages],
    )


def _admin_transaction_where(
    *,
    user_id: Optional[str],
    q: Optional[str],
    status: Optional[str],
    from_date: Optional[str],
    to_date: Optional[str],
    min_amount: Optional[int],
    max_amount: Optional[int],
) -> tuple[str, list[object]]:
    where: list[str] = []
    params: list[object] = []
    if user_id:
        where.append("t.owner_id = ?")
        params.append(user_id)
    if status:
        where.append("t.status = ?")
        params.append(status)
    if from_date:
        where.append("t.created_at >= ?")
        params.append(from_date)
    if to_date:
        where.append("t.created_at < ?")
        params.append(to_date)
    if min_amount is not None:
        where.append("t.amount >= ?")
        params.append(min_amount)
    if max_amount is not None:
        where.append("t.amount <= ?")
        params.append(max_amount)
    if q:
        like = f"%{q.strip()}%"
        where.append(
            """
            (
                t.description LIKE ?
                OR c.display_name LIKE ?
                OR c.bank LIKE ?
                OR c.account_masked LIKE ?
            )
            """
        )
        params.extend([like, like, like, like])
    return (f"WHERE {' AND '.join(where)}" if where else ""), params


@router.get("/transactions", response_model=AdminTransactionsResponse)
def admin_transactions(
    user_id: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None, max_length=120),
    status: Optional[str] = Query(default="completed", max_length=40),
    from_date: Optional[str] = Query(default=None, description="ISO datetime lower bound"),
    to_date: Optional[str] = Query(default=None, description="ISO datetime upper bound"),
    min_amount: Optional[int] = Query(default=None, ge=0),
    max_amount: Optional[int] = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> AdminTransactionsResponse:
    """Read-only ledger of transfers executed through Omni."""
    conn = get_connection()
    where_sql, params = _admin_transaction_where(
        user_id=user_id,
        q=q,
        status=status,
        from_date=from_date,
        to_date=to_date,
        min_amount=min_amount,
        max_amount=max_amount,
    )
    total_row = conn.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM transactions t
        LEFT JOIN contacts c ON c.id = t.contact_id
        {where_sql}
        """,
        params,
    ).fetchone()
    rows = conn.execute(
        f"""
        SELECT t.id, t.owner_id AS user_id, t.contact_id, t.amount,
               t.description, t.category, t.status, t.created_at,
               t.auth_methods, t.kyc_level, t.daily_limit_vnd,
               t.daily_total_before_vnd, t.retention_until,
               c.display_name AS recipient_name,
               c.bank AS recipient_bank,
               c.account_masked AS recipient_account_masked
        FROM transactions t
        LEFT JOIN contacts c ON c.id = t.contact_id
        {where_sql}
        ORDER BY t.created_at DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    return AdminTransactionsResponse(
        total=int(total_row["n"] if total_row else 0),
        limit=limit,
        offset=offset,
        retention_years=get_settings().transaction_retention_years,
        transactions=[
            AdminTransactionRow(
                **{
                    **dict(r),
                    "auth_methods": [
                        m for m in (r["auth_methods"] or "").split(",") if m
                    ],
                }
            )
            for r in rows
        ],
    )


@router.get("/compliance/retention", response_model=RetentionPolicyResponse)
def retention_policy() -> RetentionPolicyResponse:
    """Document the demo retention contract for transaction/audit records."""
    years = get_settings().transaction_retention_years
    return RetentionPolicyResponse(
        transaction_retention_years=years,
        scope=[
            "completed transaction ledger",
            "transaction auth metadata",
            "file-based safety/OTP/transfer audit trail",
        ],
        note=(
            f"Omni demo marks completed transaction rows for at least {years} "
            "years of retention for reporting, dispute lookup, audit, and "
            "competent-authority requests. No purge job deletes rows before "
            "retention_until."
        ),
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

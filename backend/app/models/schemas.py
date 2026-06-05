from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

Intent = Literal[
    "transfer",
    "balance",
    "history",
    "schedule",
    "recurring",
    "insights",
    "reminder",
    "add_contact",
    "smalltalk",
    "unknown",
]

InsightFacet = Literal["spending", "anomalies", "subscriptions"]


class Account(BaseModel):
    id: str
    bank: str
    number: str
    balance: int
    currency: str = "VND"
    primary: bool = False


class User(BaseModel):
    id: str
    display_name: str
    phone: str
    accounts: list[Account]


class Contact(BaseModel):
    id: str
    owner_id: str
    display_name: str
    bank: str
    account_number: str
    account_masked: str
    aliases: list[str] = Field(default_factory=list)
    label: Optional[str] = None
    verified: bool = True
    frequent: bool = False


class Transaction(BaseModel):
    id: str
    owner_id: str
    contact_id: str
    amount: int
    description: str = ""
    category: str = "other"
    status: Literal["pending", "completed", "cancelled", "needs_confirm"] = "completed"
    created_at: datetime


class Schedule(BaseModel):
    id: str
    owner_id: str
    contact_id: str
    source_account_id: Optional[str] = None
    amount: int
    description: str = ""
    cron: str
    next_run: datetime
    active: bool = True


class ExtractedEntities(BaseModel):
    recipient_text: Optional[str] = None
    amount: Optional[int] = None
    amount_text: Optional[str] = None
    description: Optional[str] = None
    temporal_reference: Optional[str] = None
    account_hint: Optional[str] = None
    schedule_cron: Optional[str] = None
    # Used by add_contact intent
    bank_name: Optional[str] = None
    alias: Optional[str] = None
    # Used by history intent
    specific_month: Optional[int] = None   # 1..12
    specific_year: Optional[int] = None
    all_time: bool = False                 # "tất cả từ trước đến giờ"
    limit: Optional[int] = None            # "5 giao dịch gần nhất"
    semantic_filter: Optional[str] = None  # fuzzy text match on description, e.g. "ăn uống", "Tết"
    top_recipient: bool = False            # "ai nhận nhiều nhất"
    top_category: bool = False             # "chủ đề nào nhiều nhất"
    # Used by insights intent — which facet of the insights summary the user
    # asked about. None → return a high-level rollup of all three.
    insight_facet: Optional[InsightFacet] = None


class NLUResult(BaseModel):
    intent: Intent
    confidence: float = 1.0
    entities: ExtractedEntities = Field(default_factory=ExtractedEntities)
    raw_text: str
    source: Literal["llm", "rule"] = "rule"  # which layer produced this result


class ResolvedRecipient(BaseModel):
    contact: Contact
    via_alias: Optional[str] = None
    matched_from: Literal["alias", "name", "history", "exact"] = "name"


class SafetyFlag(BaseModel):
    code: Literal[
        "missing_amount",
        "missing_recipient",
        "ambiguous_recipient",
        "new_recipient_large_amount",
        "amount_above_average",
        "insufficient_balance",
        "ok",
    ]
    severity: Literal["info", "warn", "block"]
    message: str


class TransactionDraft(BaseModel):
    id: str
    recipient: Optional[Contact] = None
    candidates: list[Contact] = Field(default_factory=list)
    source_account_id: Optional[str] = None
    source_accounts: list[Account] = Field(default_factory=list)
    amount: Optional[int] = None
    description: str = ""
    source_text: str = ""
    reference_transaction_id: Optional[str] = None
    flags: list[SafetyFlag] = Field(default_factory=list)
    requires_step_up: bool = False
    awaiting_otp: bool = False
    # True when `amount` was filled in by the history predictor rather than
    # extracted from the user's utterance. The UI surfaces this as a chip so
    # the user knows it's a suggestion they can override.
    predicted_amount: bool = False


class ContactDraft(BaseModel):
    id: str
    display_name: str
    bank: str
    account_number: str
    account_masked: str
    aliases: list[str] = Field(default_factory=list)
    label: Optional[str] = None
    flags: list[SafetyFlag] = Field(default_factory=list)


class ScheduleDraft(BaseModel):
    id: str
    recipient: Contact
    source_account_id: Optional[str] = None
    source_accounts: list[Account] = Field(default_factory=list)
    amount: int
    description: str = ""
    cron: str
    cron_label: str = ""  # human-readable, e.g. "mùng 1 hàng tháng"
    next_run: datetime
    flags: list[SafetyFlag] = Field(default_factory=list)


class OmniResponse(BaseModel):
    intent: Intent
    text: str
    draft: Optional[TransactionDraft] = None
    contact_draft: Optional[ContactDraft] = None
    schedule_draft: Optional[ScheduleDraft] = None
    history: Optional[dict] = None
    balance: Optional[dict] = None
    schedule: Optional[Schedule] = None
    # Raw dicts to avoid a schemas ↔ banking.recurring ↔ store cycle.
    # The orchestrator dumps RecurringPattern via model_dump() before attach.
    recurring_patterns: Optional[list[dict]] = None
    needs_disambiguation: bool = False

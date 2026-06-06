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
    "reminder",
    "add_contact",
    # Proactive analytics — "có giao dịch nào bất thường?", "so với tháng
    # trước?", "phân tích chi tiêu". The rule classifier emits this string
    # from its Tier-1 keyword list; this Literal entry is what stops
    # NLUResult from raising ValidationError under the rule-only fallback
    # (LLM rate-limited / CI / Playwright).
    "insights",
    "set_budget",
    "set_goal",
    "budget_status",
    # Goal progress query — "tiến độ mục tiêu Tết", "đã tiết kiệm được
    # bao nhiêu", "mục tiêu của tôi". Parallel to budget_status: reads
    # the savings_goals table and reports % progress per goal.
    "goal_status",
    # "ATM gần nhất", "tìm cây ATM Vietcombank" — location-aware finder.
    # Handler returns the OmniResponse.atms field populated from the
    # mock seed in ``banking/atm.py``.
    "atm_finder",
    "smalltalk",
    "unknown",
]


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
    # Used by set_budget / budget_status — internal category code
    # ("food", "transport", …) resolved from Vietnamese surface forms.
    budget_category: Optional[str] = None
    # Used by set_goal — the user-supplied name of the savings pot,
    # e.g. "Tết 2027" or "Mua xe".
    goal_name: Optional[str] = None
    # Used by atm_finder — surface form of the bank the user mentioned
    # ("Vietcombank", "VCB", "Techcom"). The handler normalises this and
    # passes it to ``banking.atm.find_nearby``.
    atm_bank: Optional[str] = None


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
        # Isolation Forest score over the fraud threshold — model is
        # per-user, trained on the user's own history. Raises a warn flag
        # that triggers OTP step-up; never a hard block (false positives
        # on a real bank dataset are too costly to auto-cancel a transfer).
        "fraud_risk_high",
        "ok",
    ]
    severity: Literal["info", "warn", "block"]
    message: str
    # Structured payload — currently only populated for amount_above_average
    # so the frontend can render a "why" box (median, p90, n_samples,
    # ratio) under the warning. Optional so older flag emitters stay
    # source-compatible.
    details: Optional[dict] = None


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
    # Short Vietnamese phrase explaining where the predicted amount came
    # from ("Median của 4 lần chuyển trong cùng dải ngày" / "Median của 12
    # lần chuyển cho người này"). Only set when ``predicted_amount`` is
    # True; surfaced as the tooltip on the "đề xuất từ lịch sử" chip so
    # judges can see the ML rationale on hover.
    amount_prediction_reason: Optional[str] = None
    # Auto-inferred category for the draft, derived from the description
    # text by ``app.ml.categorizer``. ``None`` when no description was
    # provided or the classifier abstained. The UI renders this as a small
    # chip below the amount; the value is also stamped onto the Transaction
    # row when the draft is executed (see banking/service.execute_transfer).
    category: Optional[str] = None


class ContactDraft(BaseModel):
    id: str
    display_name: str
    bank: str
    account_number: str
    account_masked: str
    aliases: list[str] = Field(default_factory=list)
    label: Optional[str] = None
    flags: list[SafetyFlag] = Field(default_factory=list)


class Budget(BaseModel):
    """Monthly spending envelope for a single category.

    ``category`` stores the *internal* code ("food", "transport", …) so
    aggregations against ``transactions.category`` join cleanly. The UI
    layer renders the Vietnamese label via the same mapping the
    history breakdown uses.
    """

    id: str
    user_id: str
    category: str
    monthly_limit_vnd: int
    created_at: datetime


class SavingsGoal(BaseModel):
    """A named savings pot. ``current_vnd`` is the running total of
    contributions; ``deadline`` is optional and stored as a date-only
    ISO string (the contest scope is months/years, not minutes)."""

    id: str
    user_id: str
    name: str
    target_vnd: int
    current_vnd: int = 0
    deadline: Optional[str] = None
    created_at: datetime


class BudgetDraft(BaseModel):
    """Staged budget waiting for chat confirmation. Mirrors the
    Schedule/Contact draft pattern so the confirm/cancel paths in
    the orchestrator stay symmetric."""

    id: str
    category: str
    category_label: str  # Vietnamese display name
    monthly_limit_vnd: int
    replaces_existing: bool = False
    flags: list[SafetyFlag] = Field(default_factory=list)


class GoalDraft(BaseModel):
    id: str
    name: str
    target_vnd: int
    deadline: Optional[str] = None
    flags: list[SafetyFlag] = Field(default_factory=list)


class BudgetStatus(BaseModel):
    """Snapshot of one budget vs this month's spend.

    ``ratio`` is ``spent / limit`` — the UI uses it to pick the bar
    colour (green <0.8 / orange 0.8-1.0 / red >1.0)."""

    category: str
    category_label: str
    monthly_limit_vnd: int
    spent_vnd: int
    remaining_vnd: int
    ratio: float


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
    budget_draft: Optional[BudgetDraft] = None
    goal_draft: Optional[GoalDraft] = None
    history: Optional[dict] = None
    balance: Optional[dict] = None
    schedule: Optional[Schedule] = None
    # Raw dicts to avoid a schemas ↔ banking.recurring ↔ store cycle.
    # The orchestrator dumps RecurringPattern via model_dump() before attach.
    recurring_patterns: Optional[list[dict]] = None
    budget_statuses: Optional[list[BudgetStatus]] = None
    # Populated by the ``atm_finder`` intent. Each entry has the ATM seed
    # fields plus ``distance_km`` when the user shared their location.
    atms: Optional[list[dict]] = None
    # Populated by the ``/help`` (and Vietnamese "trợ giúp") synthetic
    # intent. Each entry is a section dict ``{"title": str, "items":
    # list[{"label": str, "example": str}], "shortcut": Optional[str]}``.
    # Frontend renders this via ``<HelpCard />``; the plain ``text``
    # field carries an equivalent prose fallback for AT users / replays.
    help_sections: Optional[list[dict]] = None
    needs_disambiguation: bool = False
    # Populated only when the ``?dev=1`` query param flags the request as
    # a telemetry-overlay client. ``None`` in the default UI path so
    # judges never see internal latency numbers leak into a serialized
    # response.
    telemetry: Optional[dict] = None

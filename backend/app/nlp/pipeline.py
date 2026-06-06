"""NLU entry point. Tries Gemini if configured, falls back to rules."""

from __future__ import annotations

from typing import Optional

from ..models.schemas import NLUResult

# Add-contact fills (bank_name / alias / save-verb name) live in a sibling
# module so the canonical "Lưu <Name> STK <num> <Bank>" demo phrasing
# produces a complete contact_draft under the rule-only fallback too.
from .add_contact_entities import augment as _augment_add_contact
from .budget_entities import (
    detect_budget_intent,
    detect_goal_intent,
    extract_budget_category,
    extract_goal_name,
)
from .entities import extract
from .intent import classify
from .llm import llm_understand


def _apply_budget_overrides(text: str, result: NLUResult) -> NLUResult:
    """Augment any NLU result with budget / goal signals.

    Both the LLM and rule paths funnel through here so the feature is
    deterministic from the moment the keyword lands in the message —
    matters when Groq + Gemini are both rate-limited and we can only
    rely on rules. MUTATES + returns the same NLUResult so the caller
    doesn't need to re-thread it.
    """
    budget_kind = detect_budget_intent(text)
    goal_signal = detect_goal_intent(text)

    # Goal intent wins over budget when the text mentions both, because
    # "tiết kiệm" is a more specific anchor.
    if goal_signal and result.entities.amount is not None:
        if result.intent not in {"transfer", "history", "schedule"}:
            result.intent = "set_goal"
        elif result.intent != "set_goal":
            name = extract_goal_name(text)
            if name:
                result.intent = "set_goal"
        name = extract_goal_name(text)
        if name and not result.entities.goal_name:
            result.entities.goal_name = name

    if budget_kind is not None:
        cat = extract_budget_category(text)
        if budget_kind == "set_budget":
            # Require a category to flip; amount can be missing — the
            # handler will then ask for it in Vietnamese rather than
            # bailing out with the generic "unknown intent" reply.
            if cat is not None and result.intent != "set_goal":
                result.intent = "set_budget"
                result.entities.budget_category = cat[0]
        elif budget_kind == "budget_status":
            result.intent = "budget_status"
            if cat is not None:
                result.entities.budget_category = cat[0]

    return result


def understand(
    text: str,
    history: Optional[list[dict]] = None,
    current_draft: Optional[dict] = None,
) -> NLUResult:
    """NLU entry — LLM first, rule fallback.

    ``current_draft`` is an optional snapshot of the in-flight transfer
    draft (``{"recipient_text": str, "amount": int, "description":
    str}``) — passed to the LLM as conversational context so pronouns,
    corrections, and bare references inherit unmentioned slots from the
    antecedent draft instead of being silently dropped or re-resolved to
    the wrong contact.
    """
    llm_result = llm_understand(text, history=history, current_draft=current_draft)
    if llm_result is not None:
        llm_result.source = "llm"
        # Merge: rule-based extractor often catches amount/description better
        # than the LLM for short Vietnamese inputs; only fill blanks.
        rule_entities = extract(text)
        _augment_add_contact(rule_entities, text)
        merged = llm_result.entities.model_copy()
        for field in merged.model_fields:
            if getattr(merged, field) in (None, ""):
                setattr(merged, field, getattr(rule_entities, field))
        llm_result.entities = merged
        return _apply_budget_overrides(text, llm_result)

    intent, confidence = classify(text)
    entities = extract(text)
    _augment_add_contact(entities, text)
    # Schedule cron only makes sense under schedule intent.
    if intent != "schedule":
        entities.schedule_cron = None
    result = NLUResult(
        intent=intent,
        confidence=confidence,
        entities=entities,
        raw_text=text,
        source="rule",
    )
    return _apply_budget_overrides(text, result)

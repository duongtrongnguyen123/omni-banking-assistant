"""NLU entry point. Tries Gemini if configured, falls back to rules."""

from __future__ import annotations

from typing import Optional

from ..models.schemas import NLUResult
from .entities import extract
from .intent import classify
from .llm import llm_understand


def understand(
    text: str, history: Optional[list[dict]] = None
) -> NLUResult:
    llm_result = llm_understand(text, history=history)
    if llm_result is not None:
        llm_result.source = "llm"
        # Merge: rule-based extractor often catches amount/description better
        # than the LLM for short Vietnamese inputs; only fill blanks.
        rule_entities = extract(text)
        merged = llm_result.entities.model_copy()
        for field in merged.model_fields:
            if getattr(merged, field) in (None, ""):
                setattr(merged, field, getattr(rule_entities, field))
        llm_result.entities = merged
        return llm_result

    intent, confidence = classify(text)
    entities = extract(text)
    # Schedule cron only makes sense under schedule intent.
    if intent != "schedule":
        entities.schedule_cron = None
    return NLUResult(
        intent=intent,
        confidence=confidence,
        entities=entities,
        raw_text=text,
        source="rule",
    )

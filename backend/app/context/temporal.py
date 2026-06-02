"""Temporal reference resolver.

Maps phrases like "như tháng trước" or "người hôm qua" to a concrete past
transaction, given the user's history. Used to fill in amount/description
when the user hasn't said them explicitly.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from typing import Optional

from ..models.schemas import Transaction


def _fold(s: str) -> str:
    n = unicodedata.normalize("NFKD", s)
    return (
        "".join(c for c in n if not unicodedata.combining(c))
        .lower()
        .replace("đ", "d")
    )


def resolve_temporal_reference(
    phrase: str,
    contact_id: Optional[str],
    transactions: list[Transaction],
    now: Optional[datetime] = None,
) -> Optional[Transaction]:
    """Pick the most relevant past transaction for the phrase.

    Strategy:
      - "tháng trước" / "như tháng trước" → most recent tx in the previous
        calendar month for that contact (fallback: most recent overall).
      - "lần trước" / "vừa rồi" → most recent tx for that contact.
      - "hôm qua" / "người hôm qua" → tx within the last 24–48h for that contact.
    """
    if not phrase or not transactions:
        return None

    p = _fold(phrase)
    now = now or datetime.now(tz=transactions[0].created_at.tzinfo)

    candidates = [t for t in transactions if (contact_id is None or t.contact_id == contact_id)]
    if not candidates:
        return None
    candidates.sort(key=lambda t: t.created_at, reverse=True)

    if re.search(r"thang\s+truoc", p):
        prev = (now.replace(day=1) - timedelta(days=1))
        same_month = [
            t for t in candidates
            if t.created_at.year == prev.year and t.created_at.month == prev.month
        ]
        if same_month:
            return same_month[0]
        return candidates[0]

    if re.search(r"tuan\s+truoc", p):
        cutoff = now - timedelta(days=14)
        recent = [t for t in candidates if t.created_at >= cutoff]
        return recent[0] if recent else candidates[0]

    if "hom qua" in p:
        cutoff = now - timedelta(days=2)
        recent = [t for t in candidates if t.created_at >= cutoff]
        return recent[0] if recent else candidates[0]

    # Generic "lần trước" / "vừa rồi" / "như trước"
    return candidates[0]

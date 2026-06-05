"""Vietnamese amount parser.

Handles forms users actually type in chat:
  "5 triệu", "5tr", "5tr500", "5 triệu rưỡi", "5,5 triệu",
  "500k", "500 nghìn", "500 ngàn", "500.000", "500.000đ",
  "1 tỷ", "1tỷ", "2.000.000", "2 trieu" (no-diacritic).
"""

from __future__ import annotations

import re
from typing import Optional

_UNITS = {
    "tỷ": 1_000_000_000,
    "ty": 1_000_000_000,
    "tỉ": 1_000_000_000,
    "ti": 1_000_000_000,
    # EN — "billion" needs to come before "tr"/"m" in greedy ordering, but
    # the table is sorted by len in `_UNIT_KEYS` so this just works.
    "billion": 1_000_000_000,
    "bn": 1_000_000_000,
    "triệu": 1_000_000,
    "trieu": 1_000_000,
    "million": 1_000_000,
    "mil": 1_000_000,
    "tr": 1_000_000,
    "m": 1_000_000,
    "nghìn": 1_000,
    "nghin": 1_000,
    "ngàn": 1_000,
    "ngan": 1_000,
    "thousand": 1_000,
    "k": 1_000,
}

# Ordered for greedy match: longer keys first.
_UNIT_KEYS = sorted(_UNITS.keys(), key=len, reverse=True)
_UNIT_ALT = "|".join(re.escape(k) for k in _UNIT_KEYS)

# Pattern matches "5", "5.5", "5,5"
_NUM = r"(\d+(?:[.,]\d+)?)"

# Compound like "5tr500" or "5 triệu 500" or "5tr 500k"
_PRIMARY_RE = re.compile(
    rf"{_NUM}\s*(?P<unit>{_UNIT_ALT})\s*(?:(?P<rest>\d+)\s*(?P<rest_unit>k|nghìn|nghin|ngàn|ngan)?)?",
    re.IGNORECASE,
)

# "5 triệu rưỡi" -> 5.5
_HALF_RE = re.compile(
    rf"{_NUM}\s*(?P<unit>{_UNIT_ALT})\s*rưỡi",
    re.IGNORECASE,
)

# Plain digits with dot/comma thousand separators: 2.000.000 / 2,000,000
_PLAIN_RE = re.compile(r"(\d{1,3}(?:[.,]\d{3}){1,4})(?!\d)")


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


def parse_amount(text: str) -> tuple[Optional[int], Optional[str]]:
    """Return (vnd_amount, raw_span) or (None, None) if no amount detected."""
    if not text:
        return None, None
    t = text.lower()

    # 1. "X triệu rưỡi" — must run before primary to win the unit.
    m = _HALF_RE.search(t)
    if m:
        n = _to_float(m.group(1))
        unit = _UNITS[m.group("unit").lower()]
        return int((n + 0.5) * unit), m.group(0)

    # 2. Compound primary: "5 triệu 500", "5tr500k", "5tr500"
    m = _PRIMARY_RE.search(t)
    if m:
        n = _to_float(m.group(1))
        unit_key = m.group("unit").lower()
        unit = _UNITS[unit_key]
        total = n * unit
        rest = m.group("rest")
        if rest is not None:
            rest_n = int(rest)
            rest_unit_kw = m.group("rest_unit")
            if rest_unit_kw:
                total += rest_n * _UNITS[rest_unit_kw.lower()]
            else:
                # "5tr500" -> 5,500,000 ; "5 triệu 500" -> 5,500,000 if unit==triệu
                if unit >= 1_000_000:
                    total += rest_n * 1_000
                else:
                    total += rest_n
        return int(total), m.group(0)

    # 3. Plain dotted digits: "2.000.000"
    m = _PLAIN_RE.search(t)
    if m:
        digits = re.sub(r"[.,]", "", m.group(1))
        return int(digits), m.group(0)

    # 4. Bare large number followed by đ/vnd
    m = re.search(r"(\d{4,})\s*(?:đ|vnd|d)\b", t)
    if m:
        return int(m.group(1)), m.group(0)

    return None, None


def format_vnd(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + "đ"

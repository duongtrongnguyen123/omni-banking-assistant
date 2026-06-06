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
    "triệu": 1_000_000,
    "trieu": 1_000_000,
    "tr": 1_000_000,
    "trăm nghìn": 100_000,  # "5 trăm nghìn" = 500K
    "tram nghin": 100_000,
    "trăm ngàn": 100_000,
    "tram ngan": 100_000,
    "trăm k": 100_000,
    "tram k": 100_000,
    "trăm": 100,  # bare "trăm" = 100. Mostly only useful with thousand-tail
    "tram": 100,  # ("5 trăm nghìn"), kept here so the unit alternation catches it.
    "nghìn": 1_000,
    "nghin": 1_000,
    "ngàn": 1_000,
    "ngan": 1_000,
    "k": 1_000,
    # NOTE: deliberately no bare "m" — it causes false positives like
    # "4 mình" → "4 m" → 4,000,000đ. Users who want millions use "tr"/"triệu".
}

# Ordered for greedy match: longer keys first.
_UNIT_KEYS = sorted(_UNITS.keys(), key=len, reverse=True)
_UNIT_ALT = "|".join(re.escape(k) for k in _UNIT_KEYS)

# Pattern matches "5", "5.5", "5,5"
_NUM = r"(\d+(?:[.,]\d+)?)"

# Compound like "5tr500" or "5 triệu 500" or "5tr 500k". CRITICAL: a
# trailing letter-only negative lookahead after the unit so "tr" can't
# match "tr" inside "trăm" — that was sending "5 trăm" through as
# 5 000 000 (10× overpay; visible-money bug). Digits / whitespace /
# end-of-string still match, so "1tr5" and "5tr500" keep working.
_PRIMARY_RE = re.compile(
    rf"{_NUM}\s*(?P<unit>{_UNIT_ALT})(?![A-Za-zÀ-ỹĂăÂâĐđÊêÔôƠơƯư])\s*"
    # CRITICAL guard (round 6 S5): "100k 2 lần cho mẹ" used to swallow
    # the trailing "2" as a sub-unit concatenation and produce 100.002đ
    # — money-loss-class wrong-amount. The rest group now refuses to
    # match when the digits are followed by a non-amount Vietnamese
    # word (lần / tháng / ngày / giờ / phút / tuần / năm / người), so
    # "5tr500" and "100k500" still work but "100k 2 lần" stops at the
    # 100k and the "2 lần" stays out of the amount.
    rf"(?:(?P<rest>\d+)"
    r"(?!\s+(?:lần|lan|tháng|thang|ngày|ngay|giờ|gio|phút|phut|giây|giay|tuần|tuan|năm|nam|người|nguoi|lượt|luot))"
    rf"\s*(?P<rest_unit>k|nghìn|nghin|ngàn|ngan)?)?",
    re.IGNORECASE,
)

# "5 triệu rưỡi" -> 5.5
_HALF_RE = re.compile(
    rf"{_NUM}\s*(?P<unit>{_UNIT_ALT})\s*rưỡi",
    re.IGNORECASE,
)

# Plain digits with dot/comma thousand separators: 2.000.000 / 2,000,000
_PLAIN_RE = re.compile(r"(\d{1,3}(?:[.,]\d{3}){1,4})(?!\d)")

# Vietnamese spelled-out numerals 1–10. Spelled forms ("hai triệu",
# "năm trăm nghìn", "ba chục triệu") never reach the digit-based regexes
# above, so we substitute the digit before running parse_amount's main
# branches. Conservative: only substitute when the word is immediately
# followed by a Vietnamese amount unit so "ba tuổi" / "hai con mèo"
# don't get a spurious "3" / "2" injected.
_WORD_TO_DIGIT = {
    "một": "1", "mot": "1",
    "hai": "2",
    "ba": "3",
    "bốn": "4", "bon": "4", "tư": "4", "tu": "4",
    "năm": "5", "nam": "5",
    "sáu": "6", "sau": "6",
    "bảy": "7", "bay": "7",
    "tám": "8", "tam": "8",
    "chín": "9", "chin": "9",
    "mười": "10", "muoi": "10",
}

# Unit anchor: same vocab as the main parser plus "chục" / "chuc"
# (= ×10). Used only to verify the spelled word is followed by an
# amount unit, so the substitution stays scoped.
_SPELLED_TAIL_UNITS = (
    "triệu", "trieu", "tr",
    "tỷ", "ty", "tỉ", "ti",
    "nghìn", "nghin", "ngàn", "ngan",
    "trăm nghìn", "tram nghin", "trăm ngàn", "tram ngan",
    "trăm k", "tram k",
    "trăm", "tram",
    "chục", "chuc",
    "k",
)
# Sort by length descending so "trăm nghìn" wins over "trăm" alone.
_SPELLED_TAIL_ALT = "|".join(
    re.escape(u) for u in sorted(_SPELLED_TAIL_UNITS, key=len, reverse=True)
)
_SPELLED_WORD_ALT = "|".join(
    re.escape(w) for w in sorted(_WORD_TO_DIGIT.keys(), key=len, reverse=True)
)
_SPELLED_RE = re.compile(
    rf"\b(?P<word>{_SPELLED_WORD_ALT})\s+(?P<unit>{_SPELLED_TAIL_ALT})\b",
    re.IGNORECASE,
)


def _substitute_spelled(text: str) -> str:
    """Replace "hai triệu" → "2 triệu" / "năm trăm nghìn" → "5 trăm nghìn"
    so the digit-based regexes downstream pick the amount up. Multi-pass
    so "ba chục triệu" becomes "3 chục triệu" → "30 triệu" via the
    chuc-collapse step below."""
    def _replace(m: "re.Match[str]") -> str:
        digit = _WORD_TO_DIGIT[m.group("word").lower()]
        return f"{digit} {m.group('unit')}"

    out = _SPELLED_RE.sub(_replace, text)
    # Collapse "<N> chục" into the multiplied digit so "3 chục triệu"
    # → "30 triệu" and the existing parser handles it.
    out = re.sub(
        r"\b(\d+)\s*(?:chục|chuc)\b",
        lambda m: str(int(m.group(1)) * 10),
        out,
        flags=re.IGNORECASE,
    )
    return out


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


def parse_amount(text: str) -> tuple[Optional[int], Optional[str]]:
    """Return (vnd_amount, raw_span) or (None, None) if no amount detected."""
    if not text:
        return None, None
    t = text.lower()
    # Substitute spelled-out numerals BEFORE the digit-based regexes run.
    # Conservative: only when the spelled word is immediately followed by
    # an amount unit, so "hai con mèo" / "ba tuổi" stay untouched.
    t = _substitute_spelled(t)

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
                # "5tr500" -> 5,500,000 ; "5 triệu 500" -> 5,500,000.
                # CASE: single trailing digit with no unit and a ≥1M base
                # is the colloquial decimal-fraction form. "1tr5" means
                # 1.5 million, not 1.005 million. Likewise "2tr5" = 2.5M.
                # Only fires when rest is a SINGLE digit (1-9) so
                # "5tr500" (three digits) still resolves to 5.5M via
                # the *1000 branch.
                if unit >= 1_000_000 and len(rest) == 1:
                    total += rest_n * (unit // 10)
                elif unit >= 1_000_000:
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

    # 5. Single digit + đ — covers explicit "0đ" / "5đ". Kept narrow
    # so it doesn't swallow account hints (which use ≥3 digit prefixes
    # and the ``stk`` cue) or year mentions.
    m = re.search(r"\b(\d)\s*đ\b", t)
    if m:
        return int(m.group(1)), m.group(0)

    # 6. Plain bare integer ≥1000 inside an explicit transfer clause.
    # Catches "chuyển 100000000 cho mẹ" — the user typed the full VND
    # amount without a unit suffix. Without this branch the parser
    # returned None and the amount predictor silently overwrote the
    # user's explicit 100M with the recipient's median (~750k).
    # Restricted to a transfer-verb context so phone numbers, account
    # numbers and dates can't be mis-parsed as amounts. The
    # ``stk``/``so tai khoan`` guard prevents account hints from being
    # read as amounts (entities.py has its own _ACCOUNT_HINT_RE).
    if not re.search(r"\b(?:stk|số\s+tài\s+khoản|so\s+tai\s+khoan|account)\b", t):
        m = re.search(
            r"(?:chuyển|chuyen|gửi|gui|trả|tra|nạp|nap|send|transfer)\s+"
            r"(?:cho|tới|toi|đến|den|sang|qua\s+)?"
            r"[^\d]*?(\d{4,12})\b(?!\s*(?:người|nguoi|lần|lan|giờ|gio|phút|phut|giây|giay))",
            t,
        )
        if m:
            return int(m.group(1)), m.group(0)

    return None, None


def format_vnd(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + "đ"

"""Entity extraction for banking commands.

Extracts: recipient surface form, amount, description, temporal reference,
schedule recurrence — all without an LLM.

Patterns operate on the precomposed (NFC) form of Vietnamese characters —
e.g. "ử" is U+1EED, not U+0075 + U+0309. Each pattern explicitly lists the
precomposed alternative alongside the diacritic-free fallback so users can
type either way.
"""

from __future__ import annotations

import re
import unicodedata

from ..models.schemas import ExtractedEntities
from .amount import parse_amount


def _strip_diacritics(s: str) -> str:
    n = unicodedata.normalize("NFKD", s)
    return "".join(c for c in n if not unicodedata.combining(c)).lower().replace("đ", "d")


def normalize_alias(s: str) -> str:
    return _strip_diacritics(s).strip()


_TEMPORAL_PATTERNS = [
    r"như\s+tháng\s+trước",
    r"nhu\s+thang\s+truoc",
    r"như\s+lần\s+trước",
    r"nhu\s+lan\s+truoc",
    r"lần\s+trước",
    r"lan\s+truoc",
    r"tháng\s+trước",
    r"thang\s+truoc",
    r"người\s+hôm\s+qua",
    r"nguoi\s+hom\s+qua",
    r"hôm\s+qua",
    r"hom\s+qua",
    r"tuần\s+trước",
    r"tuan\s+truoc",
    r"vừa\s+rồi",
    r"vua\s+roi",
    # English temporal aliases — keep them in the same alternation so
    # downstream `_period_from_temporal` matches against the ascii-folded
    # form ("last month", "last week", etc.).
    r"like\s+last\s+month",
    r"same\s+as\s+last\s+month",
    r"last\s+month",
    r"previous\s+month",
    r"last\s+week",
    r"previous\s+week",
    r"yesterday",
    r"previous",
]
_TEMPORAL_RE = re.compile("|".join(_TEMPORAL_PATTERNS), re.IGNORECASE)

_DESC_RE = re.compile(
    r"(?:nội\s+dung|noi\s+dung|ghi\s+chú|ghi\s+chu|tiền|tien|note|memo)\s+"
    r"([^,.\n?!]+?)"
    r"(?:$|[,.\n?!]| như | nhu |\s+cho\s+|\s+với\s+|\s+voi\s+|\s+like\s+)",
    re.IGNORECASE,
)

_CRON_DAY_OF_MONTH = re.compile(
    r"(?:mùng|mung|ngày|ngay)\s*(\d{1,2})\s*(?:hàng|hang|mỗi|moi)\s*tháng",
    re.IGNORECASE,
)
_CRON_MONTHLY = re.compile(r"(?:hàng|hang|mỗi|moi)\s*tháng", re.IGNORECASE)
_CRON_WEEKLY = re.compile(r"(?:hàng|hang|mỗi|moi)\s*tuần", re.IGNORECASE)


# Lookahead stop tokens — used to decide where a recipient name ends.
_STOP_LOOKAHEAD = (
    r"\d"
    r"|số\s+tiền|so\s+tien"
    r"|số\s+tài|so\s+tai"
    r"|stk"
    r"|tiền\b|tien\b"
    r"|như\s+|nhu\s+"
    r"|nội\s+dung|noi\s+dung"
    r"|bao\s+nhi"
    r"|rồi\b|roi\b"
    r"|mỗi\s+tháng|moi\s+thang"
    r"|hàng\s+tháng|hang\s+thang"
    r"|cho\s"
    r"|vào\s|vao\s"
    r"|đã\b|da\b"
    r"|$"
    r"|[,.?!\n]"
)

# Preposition-led: "cho|tới|đến X" — high precision. English forms
# "to X" and "for X" piggy-back on the same handler.
_RECIPIENT_PREP_RE = re.compile(
    r"(?:cho|tới|toi|đến|den|to|for)\s+(?P<who>[^\d,.\n?!]+?)"
    rf"(?=\s*(?:{_STOP_LOOKAHEAD}))",
    re.IGNORECASE,
)

# Verb-led fallback: "chuyển|gửi|trả|nạp X <amount>" — used only when the
# preposition pattern finds nothing (otherwise "chuyển cho X" double-matches).
_RECIPIENT_VERB_RE = re.compile(
    r"(?:chuyển|chuyen|gửi|gui|trả|tra|nạp|nap|send|transfer|pay|wire)\s+"
    r"(?P<who>[^\d,.\n?!]+?)"
    rf"(?=\s*(?:{_STOP_LOOKAHEAD}))",
    re.IGNORECASE,
)

_ACCOUNT_HINT_RE = re.compile(
    r"(?:stk|số\s+tài\s+khoản|so\s+tai\s+khoan|account|số\s+cuối|so\s+cuoi)"
    r"\s*(?:là|la)?\s*(\d{3,})",
    re.IGNORECASE,
)


def _clean_recipient(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(
        r"^(?:cho|gửi|gui|đến|den|tới|toi|chuyển|chuyen|to|for|send|transfer|pay|wire)\s+",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return s.strip(" ,.;-?!")


def extract(text: str) -> ExtractedEntities:
    out = ExtractedEntities()

    amount, span = parse_amount(text)
    if amount is not None:
        out.amount = amount
        out.amount_text = span

    m = _TEMPORAL_RE.search(text)
    if m:
        out.temporal_reference = m.group(0)

    m = _DESC_RE.search(text)
    if m:
        desc = m.group(1).strip(" ,.;-?!")
        if not re.search(r"\d", desc):
            out.description = desc

    m = _RECIPIENT_PREP_RE.search(text)
    if m:
        out.recipient_text = _clean_recipient(m.group("who"))

    if not out.recipient_text:
        m = _RECIPIENT_VERB_RE.search(text)
        if m:
            out.recipient_text = _clean_recipient(m.group("who"))

    m = _ACCOUNT_HINT_RE.search(text)
    if m:
        out.account_hint = m.group(1)

    m = _CRON_DAY_OF_MONTH.search(text)
    if m:
        day = int(m.group(1))
        out.schedule_cron = f"0 9 {day} * *"
    elif _CRON_MONTHLY.search(text):
        out.schedule_cron = "0 9 1 * *"
    elif _CRON_WEEKLY.search(text):
        out.schedule_cron = "0 9 * * 1"

    return out

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
]
_TEMPORAL_RE = re.compile("|".join(_TEMPORAL_PATTERNS), re.IGNORECASE)

_DESC_RE = re.compile(
    r"(?:nội\s+dung|noi\s+dung|ghi\s+chú|ghi\s+chu|tiền|tien)\s+"
    r"([^,.\n?!]+?)"
    r"(?:$|[,.\n?!]| như | nhu |\s+cho\s+|\s+với\s+|\s+voi\s+)",
    re.IGNORECASE,
)

_CRON_DAY_OF_MONTH = re.compile(
    r"(?:mùng|mung|ngày|ngay)\s*(\d{1,2})\s*(?:hàng|hang|mỗi|moi)\s*tháng",
    re.IGNORECASE,
)
_CRON_MONTHLY = re.compile(r"(?:hàng|hang|mỗi|moi)\s*tháng", re.IGNORECASE)
_CRON_WEEKLY = re.compile(r"(?:hàng|hang|mỗi|moi)\s*tuần", re.IGNORECASE)

# ---------------------------------------------------------------------------
# History-intent specific extractors — needed when the LLM is rate-limited
# and the rule pipeline has to produce these fields on its own.
# ---------------------------------------------------------------------------

# "Tháng 4", "tháng 11", "tháng 4 năm 2025"
_SPECIFIC_MONTH_RE = re.compile(
    r"th[áa]ng\s+(\d{1,2})(?:\s+n[ăa]m\s+(\d{4}))?",
    re.IGNORECASE,
)

# "tất cả", "từ trước đến giờ", "từ xưa đến nay"
_ALL_TIME_RE = re.compile(
    r"tất\s+cả|tat\s+ca|từ\s+trước\s+đến\s+giờ|tu\s+truoc\s+den\s+gio|từ\s+xưa|tu\s+xua",
    re.IGNORECASE,
)

# "5 giao dịch", "3 lần", "10 giao dịch gần nhất"
_LIMIT_RE = re.compile(
    r"(\d{1,3})\s*(?:giao\s+dịch|giao\s+dich|lần|lan|khoản|khoan|cái|cai)",
    re.IGNORECASE,
)

# "lần cuối", "lần gần nhất" → 1
_LIMIT_ONE_RE = re.compile(
    r"lần\s+cuối|lần\s+gần\s+nhất|lan\s+cuoi|lan\s+gan\s+nhat",
    re.IGNORECASE,
)

# "ai nhận nhiều nhất", "ai gửi NHIỀU TIỀN nhất", "ai chuyển khoản nhiều nhất".
# We just need "ai" + verb somewhere, then "nhiều" + (anything) + "nhất".
_TOP_RECIPIENT_RE = re.compile(
    r"ai\s+(?:nhận|nhan|gửi|gui|chuyển|chuyen)[^,.\n?!]*nhiều[^,.\n?!]*nhất"
    r"|ai\s+(?:nhận|nhan|gửi|gui|chuyển|chuyen)[^,.\n?!]*nhieu[^,.\n?!]*nhat",
    re.IGNORECASE,
)

# "chủ đề nào", "danh mục nào", "khoản chi nào nhiều nhất"
_TOP_CATEGORY_RE = re.compile(
    r"chủ\s+đề\s+nào|danh\s+mục\s+nào|khoản\s+(?:chi|nào)\s+nhiều\s+nhất"
    r"|chu\s+de\s+nao|danh\s+muc\s+nao",
    re.IGNORECASE,
)

# Semantic filter trigger words: tiêu/chi (+ optional gì) + cho, plus
# "liên quan đến", "về chủ đề". Captures the phrase after them.
_SEMANTIC_RE = re.compile(
    r"(?:"
    r"  (?:tiêu|chi|tieu)\s+(?:gì\s+|gi\s+)?cho"
    r"| liên\s+quan\s+đến|lien\s+quan\s+den"
    r"| về\s+chủ\s+đề|ve\s+chu\s+de"
    r")"
    r"\s+([^,.\n?!]+?)(?=\s+bao\s+nhi|\s+tháng|\s+tuần|\s+hôm|\s+năm|$|[,.\n?!])",
    re.IGNORECASE | re.VERBOSE,
)


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
    r"|là\s|la\s"        # "mẹ là bao nhiêu" — stop at "là"
    r"|thì\s|thi\s"      # "anh thì khoẻ không" — stop at "thì"
    r"|$"
    r"|[,.?!\n]"
)

# Preposition-led: "cho|tới X" — high precision.
# NOTE: deliberately drop "đến/den" — it's overloaded in Vietnamese
# ("đến giờ" = "until now") and causes false-positive recipient captures.
_RECIPIENT_PREP_RE = re.compile(
    r"(?:cho|tới|toi)\s+(?P<who>[^\d,.\n?!]+?)"
    rf"(?=\s*(?:{_STOP_LOOKAHEAD}))",
    re.IGNORECASE,
)

# Verb-led fallback: "chuyển|gửi|trả|nạp X <amount>" — used only when the
# preposition pattern finds nothing (otherwise "chuyển cho X" double-matches).
_RECIPIENT_VERB_RE = re.compile(
    r"(?:chuyển|chuyen|gửi|gui|trả|tra|nạp|nap|send|transfer)\s+"
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
        r"^(?:cho|gửi|gui|đến|den|tới|toi|chuyển|chuyen)\s+",
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

    # History-intent specific entities — extracted regardless of intent so
    # the orchestrator gets full information when the rule pipeline runs.
    m = _SPECIFIC_MONTH_RE.search(text)
    if m and 1 <= int(m.group(1)) <= 12:
        out.specific_month = int(m.group(1))
        if m.group(2):
            out.specific_year = int(m.group(2))

    if _ALL_TIME_RE.search(text):
        out.all_time = True

    if _LIMIT_ONE_RE.search(text):
        out.limit = 1
    else:
        m = _LIMIT_RE.search(text)
        if m:
            out.limit = int(m.group(1))

    if _TOP_RECIPIENT_RE.search(text):
        out.top_recipient = True
    if _TOP_CATEGORY_RE.search(text):
        out.top_category = True

    m = _SEMANTIC_RE.search(text)
    if m:
        sf = m.group(1).strip(" ,.;-?!")
        if sf and not re.search(r"\d", sf):
            out.semantic_filter = sf

    return out

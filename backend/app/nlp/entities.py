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

# Preposition-led: "cho|tới|đến X" — high precision.
_RECIPIENT_PREP_RE = re.compile(
    r"(?:cho|tới|toi|đến|den)\s+(?P<who>[^\d,.\n?!]+?)"
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

# Source-account hints in the user's utterance.
# Bank-name forms: "từ tài khoản Vietcombank", "từ VCB", "trừ vào VPBank"
# Account-kind forms: "từ tài khoản tiết kiệm", "từ savings", "lấy từ lương"
_SOURCE_BANK_RE = re.compile(
    r"(?:từ|tu|trừ\s+vào|tru\s+vao|lấy\s+từ|lay\s+tu|dùng|dung)\s+"
    r"(?:tài\s+khoản|tai\s+khoan|tk|account)?\s*"
    r"(vietcombank|vcb|techcombank|tcb|vpbank|vpb|mb\s*bank|mbbank|mb|bidv|"
    r"agribank|acb|tpbank|tp\s*bank|sacombank|vietinbank|ctg)",
    re.IGNORECASE,
)
_SOURCE_KIND_RE = re.compile(
    r"(?:từ|tu|trừ\s+vào|tru\s+vao|lấy\s+từ|lay\s+tu|dùng|dung)\s+"
    r"(?:tài\s+khoản|tai\s+khoan|tk|account|tiền|tien)?\s*"
    r"(tiết\s*kiệm|tiet\s*kiem|savings?|saving|"
    r"lương|luong|salary|"
    r"thanh\s*toán|thanh\s*toan|chính|chinh|checking|main)",
    re.IGNORECASE,
)
_INTERNAL_TRANSFER_RE = re.compile(
    r"chuyển\s+nội\s+bộ|chuyen\s+noi\s+bo|"
    r"chuyển\s+(?:qua|sang)\s+(?:tài\s+khoản|tai\s+khoan|tk)\s+(?:của\s+mình|cua\s+minh)|"
    r"chuyển\s+(?:giữa|qua\s+lại)\s+tài\s+khoản|"
    r"internal\s+transfer",
    re.IGNORECASE,
)


def _normalize_source_hint(raw: str) -> str:
    """Map a matched surface form to a canonical key.

    Bank names → lowercase canonical slug (vietcombank, techcombank, vpbank, …).
    Account kinds → "savings" | "salary" | "checking".
    """
    s = raw.lower().strip()
    s = re.sub(r"\s+", "", s)
    # diacritic fold for matching
    folded = _strip_diacritics(s).replace(" ", "")
    if folded in ("vcb", "vietcombank"):
        return "vietcombank"
    if folded in ("tcb", "techcombank"):
        return "techcombank"
    if folded in ("vpb", "vpbank"):
        return "vpbank"
    if folded in ("mb", "mbbank"):
        return "mb bank"
    if folded == "bidv":
        return "bidv"
    if folded == "agribank":
        return "agribank"
    if folded == "acb":
        return "acb"
    if folded in ("tp", "tpbank"):
        return "tpbank"
    if folded == "sacombank":
        return "sacombank"
    if folded in ("vietinbank", "ctg"):
        return "vietinbank"
    if folded in ("tietkiem", "saving", "savings"):
        return "savings"
    if folded in ("luong", "salary"):
        return "salary"
    if folded in ("thanhtoan", "chinh", "checking", "main"):
        return "checking"
    return folded


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

    # Source-account hints. Bank match wins over kind match because banks are
    # more specific (3 banks vs ~3 kinds).
    m = _SOURCE_BANK_RE.search(text)
    if m:
        out.source_account_hint = _normalize_source_hint(m.group(1))
    else:
        m = _SOURCE_KIND_RE.search(text)
        if m:
            out.source_account_hint = _normalize_source_hint(m.group(1))

    if _INTERNAL_TRANSFER_RE.search(text):
        out.internal_transfer = True

    m = _CRON_DAY_OF_MONTH.search(text)
    if m:
        day = int(m.group(1))
        out.schedule_cron = f"0 9 {day} * *"
    elif _CRON_MONTHLY.search(text):
        out.schedule_cron = "0 9 1 * *"
    elif _CRON_WEEKLY.search(text):
        out.schedule_cron = "0 9 * * 1"

    return out

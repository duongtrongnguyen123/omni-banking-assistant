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
from typing import Optional

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
    r"hôm\s+nay",
    r"hom\s+nay",
    r"hôm\s+qua",
    r"hom\s+qua",
    r"tuần\s+này",
    r"tuan\s+nay",
    r"tuần\s+trước",
    r"tuan\s+truoc",
    r"năm\s+nay",
    r"nam\s+nay",
    r"năm\s+ngoái",
    r"nam\s+ngoai",
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

# Drop these as a description — they're question/agreement particles, not
# transaction content ("được ko" / "được không" / "nhé" / "nha" / "nhỉ").
_DESC_PARTICLE_RE = re.compile(
    r"^(?:được\s+(?:không|ko|hong|hk)|được|không|ko|nhé|nha|nhỉ|nhe|nha)\s*\??$",
    re.IGNORECASE,
)

_CRON_DAY_OF_MONTH = re.compile(
    r"(?:mùng|mung|ngày|ngay)\s*(\d{1,2})\s*(?:hàng|hang|mỗi|moi)\s*tháng",
    re.IGNORECASE,
)
_CRON_MONTHLY = re.compile(r"(?:hàng|hang|mỗi|moi)\s*tháng", re.IGNORECASE)
_CRON_WEEKLY = re.compile(r"(?:hàng|hang|mỗi|moi)\s*tuần", re.IGNORECASE)
_CRON_DAILY = re.compile(r"(?:hàng|hang|mỗi|moi)\s*ng[àa]y", re.IGNORECASE)

# Day-of-week extraction for weekly schedules. Maps Vietnamese forms
# ("thứ 2" / "thứ hai" / "Chủ nhật") to cron DOW (0=Sun, 1=Mon, …, 6=Sat).
# CRITICAL: ``_CRON_WEEKLY`` alone always emitted cron DOW=1 (Monday) no
# matter what the user said, so "đặt lịch thứ 5 hàng tuần" got scheduled
# for Monday — wrong-day bug. This table + the new pattern below close
# that gap.
_DOW_PATTERNS: list[tuple[str, int]] = [
    # Spelled-out + numeric Vietnamese.
    (r"chủ\s+nhật|chu\s+nhat|\bcn\b", 0),
    (r"thứ\s+(?:hai|2)|thu\s+(?:hai|2)", 1),
    (r"thứ\s+(?:ba|3)|thu\s+(?:ba|3)", 2),
    (r"thứ\s+(?:tư|4)|thu\s+(?:tu|4)", 3),
    (r"thứ\s+(?:năm|5)|thu\s+(?:nam|5)", 4),
    (r"thứ\s+(?:sáu|6)|thu\s+(?:sau|6)", 5),
    (r"thứ\s+(?:bảy|7)|thu\s+(?:bay|7)", 6),
]
_DOW_COMPILED = [(re.compile(p, re.IGNORECASE), d) for p, d in _DOW_PATTERNS]


def _extract_dow(text: str) -> Optional[int]:
    """Return cron DOW (0-6) for a Vietnamese day-of-week mention, or
    ``None`` if no day was named. Uses an ordered table so "thứ 2" wins
    over the bare "2" inside other contexts."""
    for rx, dow in _DOW_COMPILED:
        if rx.search(text):
            return dow
    return None

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

# Default-limit cue — "(các) giao dịch gần nhất" / "giao dịch gần đây"
# without a number. Falls back to N=5 so the user gets a list instead
# of the aggregate they didn't ask for.
_LIMIT_DEFAULT_RE = re.compile(
    r"(?:cac\s+)?(?:các\s+)?giao\s+d[ịi]ch\s+(?:gần\s+nhất|gan\s+nhat|gần\s+đây|gan\s+day)"
    r"|(?:cac\s+)?(?:các\s+)?giao\s+d[ịi]ch\s+(?:vừa\s+rồi|vua\s+roi|mới\s+nhất|moi\s+nhat)",
    re.IGNORECASE,
)
_LIMIT_DEFAULT_N = 5

# "ai nhận nhiều nhất", "ai gửi NHIỀU TIỀN nhất", "ai chuyển khoản nhiều nhất",
# plus the inverted phrasings "tôi tiêu nhiều nhất cho ai" / "cho ai nhiều nhất"
# where the verb is on the user side ("tiêu / chi") and "ai" is the object.
_TOP_RECIPIENT_RE = re.compile(
    r"ai\s+(?:nhận|nhan|gửi|gui|chuyển|chuyen)[^,.\n?!]*nhiều[^,.\n?!]*nhất"
    r"|ai\s+(?:nhận|nhan|gửi|gui|chuyển|chuyen)[^,.\n?!]*nhieu[^,.\n?!]*nhat"
    r"|(?:nhiều|nhieu)\s+nhất\s+cho\s+ai|(?:nhieu)\s+nhat\s+cho\s+ai"
    r"|cho\s+ai\s+(?:nhiều|nhieu)\s+nhất|cho\s+ai\s+(?:nhieu)\s+nhat"
    # Verb-first form ("tôi gửi ai nhiều nhất").
    r"|(?:gửi|gui|chuyển|chuyen|nhận|nhan)\s+ai[^,.\n?!]*(?:nhiều|nhieu)[^,.\n?!]*(?:nhất|nhat)"
    # "Top N người ..." — the ranking phrasing judges actually type.
    # Conservative: requires explicit "Top" anchor + người to avoid
    # eating numeric-amount transfer commands.
    r"|\btop\s+\d+\s+(?:nguoi|người)"
    r"|\btop\s+(?:nguoi|người)\s+(?:nhận|nhan|gửi|gui|chuyển|chuyen)",
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
    r"|từ\s+trước|tu\s+truoc"  # "gửi bố từ trước đến giờ"
    r"|từ\s+xưa|tu\s+xua"
    r"|ít\s+tiền|it\s+tien"    # "chuyển ny ít tiền" — qualifier, not part of name
    r"|một\s+ít|mot\s+it|vài\s+|vai\s+"
    r"|chút\s|chut\s"
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

# Bare leading-token + amount fallback: "mẹ 2tr" / "anh Hùng 500k". Vietnamese
# shorthand judges actually use. Only fires when the message *starts* with a
# token, optionally a relational prefix, then an amount unit — keeps the
# pattern conservative enough to ignore "tiền nhà 3tr" / "lương 5tr" /
# "ngân sách 1tr" via a denylist.
_BARE_RECIPIENT_AMOUNT_RE = re.compile(
    r"^(?P<who>[^\d,.\n?!]+?)\s+\d+(?:[.,]\d+)?\s*"
    r"(?:tr|triệu|trieu|k|nghìn|nghin|ngàn|ngan|tỷ|ty|tỉ|ti)\b",
    re.IGNORECASE,
)

# Tokens that are amount-context nouns, not recipient names. If the
# captured "who" reduces to any of these (after diacritic-fold), bail —
# "lương 5tr" / "tiền nhà 3tr" / "số dư 2tr" must NOT route to transfer
# with those words as the recipient.
_BARE_RECIPIENT_DENYLIST = {
    "luong", "tien", "so du", "tien nha", "tien dien", "tien nuoc",
    "tien an", "ngan sach", "han muc", "muc tieu", "tiet kiem",
    "thue", "phi", "no", "cuoc",
}

# First-person pronouns that judges naturally use ("gửi mình 200k",
# "ai chuyển tiền cho mình", "trả tôi"). Without this guard, "mình"
# diacritic-folds to "minh" → matches the contact "Minh" → confirm card
# offers to send Minh money. Same trap for "tôi" → "toi".
#
# CRITICAL: compare against the *original* text (with diacritics) so
# the contact name "Minh" (no `ì`) still resolves. The diacritic is the
# only thing distinguishing pronoun from name.
_SELF_PRONOUNS_DIACRITIC = {
    "mình",   # most common
    "tôi",
    "tớ",
    "tao",    # informal first person
}

# ATM finder — surface-form → canonical bank name. Both the short
# (VCB, TCB, …) and full ("Vietcombank") forms are common in chat;
# we normalise to the canonical bank label used in ``data/atms.json`` so
# the route can filter directly.
_ATM_BANK_ALIASES: list[tuple[str, str]] = [
    ("vietcombank", "Vietcombank"),
    ("vcb", "Vietcombank"),
    ("techcombank", "Techcombank"),
    ("techcom", "Techcombank"),
    ("tcb", "Techcombank"),
    ("bidv", "BIDV"),
    ("agribank", "Agribank"),
    ("mb bank", "MB Bank"),
    ("mbbank", "MB Bank"),
    ("mb ", "MB Bank"),
    (" mb", "MB Bank"),
    ("vpbank", "VPBank"),
    ("vpb", "VPBank"),
    ("acb", "ACB"),
    ("sacombank", "Sacombank"),
    ("stb", "Sacombank"),
]


def extract_atm_bank(text: str) -> Optional[str]:
    """Return the canonical bank name mentioned in ``text``, or ``None``.

    Matched on the diacritic-folded form so "Vietcombank" / "vietcombank"
    / "VCB" all resolve to ``"Vietcombank"``.
    """
    folded = _strip_diacritics(text)
    for needle, canonical in _ATM_BANK_ALIASES:
        if needle in folded:
            return canonical
    return None


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
        # Reject question / agreement particles ("được ko", "nhé", "nha"…)
        # and digit-only spans.
        if (
            desc
            and not re.search(r"\d", desc)
            and not _DESC_PARTICLE_RE.match(desc)
        ):
            out.description = desc

    m = _RECIPIENT_PREP_RE.search(text)
    if m:
        out.recipient_text = _clean_recipient(m.group("who"))

    if not out.recipient_text:
        m = _RECIPIENT_VERB_RE.search(text)
        if m:
            out.recipient_text = _clean_recipient(m.group("who"))

    # Bare leading-token + amount fallback ("mẹ 2tr" / "anh Hùng 500k").
    # Only fires when no other recipient was found. Denylist filters out
    # amount-context nouns like "lương 5tr" / "tiền nhà 3tr".
    if not out.recipient_text:
        m = _BARE_RECIPIENT_AMOUNT_RE.search(text)
        if m:
            candidate = _clean_recipient(m.group("who"))
            folded = _strip_diacritics(candidate).strip()
            if folded and folded not in _BARE_RECIPIENT_DENYLIST:
                out.recipient_text = candidate

    # Self-pronoun guard. Compare against the still-diacritic-bearing
    # recipient_text — "mình" vs "Minh" only differ by the dấu huyền.
    # Folding before the compare would let the contact "Minh" be
    # mistaken for the pronoun and vice-versa. Check the FIRST token
    # so trailing question particles ("mình không", "tôi nhé") still
    # trigger the drop.
    if out.recipient_text:
        first = out.recipient_text.strip().split()[0].lower()
        if first in _SELF_PRONOUNS_DIACRITIC:
            out.recipient_text = None

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
        # Honour the user's day-of-week. Falls back to Monday (DOW=1) only
        # when the message says "hàng tuần" without naming a day.
        dow = _extract_dow(text)
        out.schedule_cron = f"0 9 * * {dow if dow is not None else 1}"
    elif _CRON_DAILY.search(text):
        # "mỗi ngày 100k cho mẹ" — fire every day at 9 a.m.
        out.schedule_cron = "0 9 * * *"

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
        elif _LIMIT_DEFAULT_RE.search(text):
            # "các giao dịch gần nhất" without a number — default to 5 so
            # the handler emits a list, not an aggregate.
            out.limit = _LIMIT_DEFAULT_N

    if _TOP_RECIPIENT_RE.search(text):
        out.top_recipient = True
    if _TOP_CATEGORY_RE.search(text):
        out.top_category = True

    m = _SEMANTIC_RE.search(text)
    if m:
        sf = m.group(1).strip(" ,.;-?!")
        if sf and not re.search(r"\d", sf):
            out.semantic_filter = sf

    # ATM finder — bank hint is optional ("ATM gần nhất" sends None,
    # "ATM Vietcombank gần đây" sends the canonical issuer name).
    bank = extract_atm_bank(text)
    if bank:
        out.atm_bank = bank

    return out

"""Rule-based extractors specific to the ``add_contact`` intent.

Lives in its own module so the augmentation hook in ``pipeline.py`` is
a one-line import + one-line call, and the long regex bodies stay
isolated from the many in-flight merges hitting ``nlp/entities.py``.

When the LLM is rate-limited (CI / Playwright / 429 fallback) the
canonical demo phrasing

    Lưu Lê Mai STK 0123987654 Vietcombank tên gọi tắt chị Mai

would otherwise produce empty ``bank_name`` / ``alias`` / ``recipient_text``
on the rule path — the orchestrator then asks "Bạn cho mình biết tên
người cần lưu nhé. Ngân hàng của tài khoản này là gì?" and the KB7
demo fails. The regexes below close that gap, while staying conservative
enough to leave the other intents untouched.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models.schemas import ExtractedEntities


_BANK_NAME_RE = re.compile(
    r"\b("
    r"vietcombank|vcb"
    r"|techcombank|tcb"
    r"|bidv"
    r"|mbbank|mb\s+bank"
    r"|tpbank|tp\s+bank"
    r"|vpbank|vp\s+bank"
    r"|acb"
    r"|sacombank|stb"
    r"|agribank"
    r"|vietinbank|ctg"
    r"|shb"
    r"|hdbank|hd\s+bank"
    r"|ocb"
    r"|cake|timo|tnex"
    r")\b",
    re.IGNORECASE,
)

# Surface-form → canonical display name used on the contact card.
_BANK_NORMALIZE: dict[str, str] = {
    "vcb": "Vietcombank",
    "tcb": "Techcombank",
    "stb": "Sacombank",
    "ctg": "Vietinbank",
    "mb bank": "MB Bank",
    "mbbank": "MB Bank",
    "tp bank": "TPBank",
    "vp bank": "VPBank",
    "hd bank": "HDBank",
}

# "tên gọi tắt chị Mai" / "biệt danh boss" / "gọi là Mai" / "alias mom"
_ALIAS_RE = re.compile(
    r"(?:tên\s+gọi\s+tắt|ten\s+goi\s+tat|biệt\s+danh|biet\s+danh"
    r"|gọi\s+(?:là|tắt)|goi\s+(?:la|tat)|alias|nickname)"
    r"\s+([^,.\n?!]+?)(?=$|[,.?!\n]|\s+stk\b|\s+ngân\s+hàng\b|\s+ngan\s+hang\b)",
    re.IGNORECASE,
)

# "lưu X STK …" / "thêm liên hệ X STK …" — capture the name between the
# save verb and the account marker. Conservative: only fires when an
# account marker is present in the message (caller guards on this too).
_SAVE_NAME_RE = re.compile(
    r"(?:lưu|luu|thêm(?:\s+liên\s+hệ)?|them(?:\s+lien\s+he)?|ghi\s+nhớ|ghi\s+nho)"
    r"\s+([^\d,.\n?!]+?)"
    r"(?=\s*(?:\bstk\b|\bsố\s+tài\s+khoản\b|\bso\s+tai\s+khoan\b|\baccount\b|\d))",
    re.IGNORECASE,
)

# Account-number marker — must co-occur for the save-name capture and
# the bank-name fill, otherwise we'd false-positive on "lưu ý" / "thêm
# tiền vào lương".
_ACCOUNT_MARKER_RE = re.compile(
    r"\bstk\b|\bsố\s+tài\s+khoản\b|\bso\s+tai\s+khoan\b|\baccount\b",
    re.IGNORECASE,
)


def augment(entities: ExtractedEntities, text: str) -> None:
    """Mutate ``entities`` in place with add_contact-specific fields.

    Only fills blanks — never overwrites a value the upstream extractor
    or LLM already produced. Idempotent.
    """
    has_account_marker = bool(_ACCOUNT_MARKER_RE.search(text))

    # bank_name — safe to set even without an account marker (matching the
    # bank vocabulary is high precision on its own).
    if not entities.bank_name:
        m = _BANK_NAME_RE.search(text)
        if m:
            raw = re.sub(r"\s+", " ", m.group(1).strip().lower())
            entities.bank_name = _BANK_NORMALIZE.get(raw, m.group(1).strip())

    # alias — same, the trigger phrase ("tên gọi tắt", "biệt danh") is
    # specific enough that a match doesn't false-positive on transfer text.
    if not entities.alias:
        m = _ALIAS_RE.search(text)
        if m:
            a = m.group(1).strip(" ,.;-?!\"'")
            if a:
                entities.alias = a

    # Save-verb name — only when an account marker is also present.
    # The preposition-led recipient extractor in entities.py starts at
    # "cho" / "tới" and misses bare "lưu <Name> STK …" so this fills
    # the blank.
    if has_account_marker and not entities.recipient_text:
        m = _SAVE_NAME_RE.search(text)
        if m:
            name = m.group(1).strip(" ,.;-?!")
            # Strip "liên hệ" if it leaked in (e.g. "thêm liên hệ X").
            name = re.sub(
                r"^(?:liên\s+hệ|lien\s+he)\s+", "", name, flags=re.IGNORECASE
            )
            if name and not re.search(r"\d", name):
                entities.recipient_text = name

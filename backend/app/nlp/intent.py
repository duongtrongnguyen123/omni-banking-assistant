"""Intent classifier — keyword-priority with Vietnamese diacritic tolerance.

Uses three tiers so specific signals win deterministically over generic
ones — important because this is the fallback when the LLM rate-limits
out and we still need correct routing.

Tier 1 (HIGH, 0.85): unambiguous keywords (`so du`, `lich su`, `dat lich`).
Tier 2 (MED, 0.65):  precise but possibly overlapping (`bao nhieu`, `tieu`,
                     `lan cuoi`, transfer verbs).
Tier 3 (LOW, 0.4):   fallback heuristic (bare digit → transfer).
"""

from __future__ import annotations

import re
import unicodedata

from ..models.schemas import Intent


def _ascii_fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.replace("đ", "d").replace("Đ", "D").lower()


# ---------------------------------------------------------------------------
# Tier 1 — high precision, very unambiguous
# ---------------------------------------------------------------------------

_HIGH: list[tuple[Intent, list[str]]] = [
    ("schedule", [
        "dat lich", "len lich", "lap lich", "dinh ky",
        "tu dong chuyen",
    ]),
    ("reminder", ["nhac no", "nhac tra", "nhac thanh toan", "tao nhac"]),
    ("balance", [
        "so du", "kiem tra so du", "xem so du", "balance",
        "tai khoan con", "con bao nhieu trong tai khoan",
    ]),
    ("history", [
        "lich su", "thong ke",
        "ai nhan nhieu", "ai gui nhieu", "ai chuyen nhieu",
        "nhieu nhat", "lan cuoi", "lan gan nhat", "gan nhat",
        "5 giao dich", "3 giao dich", "10 giao dich",
        "giao dich gan day", "xem giao dich",
        "tu truoc den gio", "tat ca cac lan",
    ]),
    ("add_contact", [
        "luu danh ba", "them danh ba", "luu lien lac", "luu so",
    ]),
    ("smalltalk", ["xin chao", "chao omni", "hello", "cam on"]),
]

# ---------------------------------------------------------------------------
# Tier 2 — medium precision; check after high-tier short-circuit
# ---------------------------------------------------------------------------

_MED: list[tuple[Intent, list[str]]] = [
    ("schedule", [
        "hang thang", "moi thang", "hang tuan", "moi tuan",
        "moi ngay", "hang ngay",
    ]),
    # history before transfer: "bao nhieu" + verb is retrospective, not a
    # transfer command. Transfer queries have a concrete amount.
    ("history", [
        "bao nhieu", "da tieu", "da gui", "da chuyen",
        "tieu bao", "minh tieu", "toi tieu", "tieu cho",
        "chi cho", "chi gi", "chi nao", "khoan chi", "khoan nao",
        "tong cong", "tong chi", "tong gui", "tong chuyen",
        "tong tien", "tat ca", "den gio", "lien quan",
        "thang nay gui", "thang nay chuyen", "thang nay tieu",
        "thang truoc gui", "thang truoc chuyen", "thang truoc tieu",
    ]),
    ("transfer", [
        "chuyen", "gui", "tra", "thanh toan", "nap",
        "transfer", "send",
    ]),
    ("smalltalk", ["hi", "hey"]),
]


def classify(text: str) -> tuple[Intent, float]:
    folded = _ascii_fold(text)
    folded = re.sub(r"\s+", " ", folded)

    # Tier 1 — first match wins, no scoring needed.
    for intent, kws in _HIGH:
        for kw in kws:
            if kw in folded:
                return intent, 0.85

    # Tier 2 — first match wins again, but scoring kept for telemetry.
    for intent, kws in _MED:
        for kw in kws:
            if kw in folded:
                return intent, 0.65

    # Tier 3 — bare digit means an unclassified transfer command.
    if re.search(r"\d", folded):
        return "transfer", 0.4

    return "unknown", 0.0

"""Intent classifier — keyword-priority with Vietnamese diacritic tolerance."""

from __future__ import annotations

import re
import unicodedata

from ..models.schemas import Intent

_INTENT_KEYWORDS: list[tuple[Intent, list[str]]] = [
    # Order matters: schedule before transfer (overlapping verbs).
    (
        "schedule",
        [
            "dat lich", "len lich", "lap lich", "dinh ky",
            "hang thang", "moi thang", "tu dong chuyen",
            "moi tuan", "hang tuan",
        ],
    ),
    (
        "reminder",
        ["nhac no", "nhac tra", "nhac thanh toan", "tao nhac"],
    ),
    (
        "history",
        [
            "lich su", "da gui", "da chuyen", "bao nhieu roi",
            "thang nay gui", "thang nay chuyen", "xem giao dich",
            "tong chi", "tong gui", "tong chuyen", "so voi thang",
            "thong ke",
        ],
    ),
    (
        "balance",
        [
            "so du", "con bao nhieu", "tai khoan con", "balance",
            "kiem tra so du", "xem so du",
        ],
    ),
    (
        "transfer",
        [
            "chuyen", "gui", "tra", "thanh toan", "nap", "transfer",
            "send",
        ],
    ),
    (
        "smalltalk",
        ["xin chao", "chao omni", "hello", "hi", "cam on"],
    ),
]


def _ascii_fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    # Vietnamese đ/Đ aren't decomposed by NFKD.
    return s.replace("đ", "d").replace("Đ", "D").lower()


def classify(text: str) -> tuple[Intent, float]:
    folded = _ascii_fold(text)
    folded = re.sub(r"\s+", " ", folded)

    best: tuple[Intent, float] = ("unknown", 0.0)
    for intent, kws in _INTENT_KEYWORDS:
        for kw in kws:
            if kw in folded:
                # Crude confidence: longer keyword → higher confidence.
                conf = min(0.5 + 0.05 * len(kw.split()), 0.95)
                if conf > best[1]:
                    best = (intent, conf)
                # Earlier categories win on ties (loop order).
    if best[0] == "unknown" and re.search(r"\d", folded):
        # A bare amount with no verb most likely means a transfer.
        return "transfer", 0.4
    return best

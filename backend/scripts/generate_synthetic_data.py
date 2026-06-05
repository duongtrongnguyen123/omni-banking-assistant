"""Generate richer transaction history for the demo user.

Writes ~100 transactions across 6 months (Jan→Jun 2026) following clear
behavioural patterns so the tree + rule scorer have something to learn:

    mẹ        → 1st of month ±2
    bố        → 5th of month ±2
    tạp hoá   → every Sunday
    cơm văn phòng → Mon/Wed/Fri lunchtime
    PT gym    → every Monday morning
    yoga      → every Saturday evening
    shipper   → 4–6× per month, random weekday
    bestie    → 2× per month, Friday evenings
    sếp       → end-of-month bonus / quà
    tạp hoá   → occasional weekday top-ups

Idempotent: re-running wipes the existing transactions and reseeds.
Embeddings are NOT regenerated here — they'll fill in lazily.

Usage:
    .venv/bin/python scripts/generate_synthetic_data.py
"""

from __future__ import annotations

import os
import random
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.connection import get_connection  # noqa: E402

USER = "u_an"
TZ_SUFFIX = "+07:00"
random.seed(42)

# (contact_id, label, category, day-of-month pattern OR dow pattern,
#  amount range, description template)
PATTERNS = [
    # Monthly fixed
    ("c_lan",      "family", "dom", (1, 2),  (4_500_000, 5_500_000), "Tiền sinh hoạt tháng"),
    ("c_bo_hung",  "family", "dom", (5, 2),  (1_800_000, 2_200_000), "Tiền hiếu bố tháng"),
    ("c_co_mai",   "family", "dom", (10, 3), (400_000, 800_000),     "Gửi cô Mai"),
    ("c_chi_hoa",  "family", "dom", (15, 3), (300_000, 700_000),     "Tiền chị Hoa"),

    # Weekly / weekday patterns
    ("c_tap_hoa",  "daily",  "dow", (6,),    (200_000, 400_000),     "Mua đồ tạp hoá"),       # Sundays
    ("c_com_lan",  "daily",  "dow_set", (0, 2, 4), (35_000, 70_000), "Cơm văn phòng"),         # Mon/Wed/Fri
    ("c_pt_nga",   "health", "dow", (0,),    (1_900_000, 2_100_000), "PT gym tuần"),           # Monday (monthly)
    ("c_yoga_duc", "health", "dow", (5,),    (900_000, 1_100_000),   "Buổi yoga"),             # Saturday

    # Less-regular
    ("c_shipper",  "daily",  "random", (4, 6), (40_000, 130_000),    "GrabFood"),              # 4–6× / month
    ("c_linh",     "friends", "dow_count", (4, 2), (150_000, 300_000), "Cafe với bestie"),     # Friday × 2
    ("c_minh_mb",  "friends", "random", (2, 4), (200_000, 800_000),   "Cafe + ăn trưa"),
    ("c_cuong",    "work",    "dom_once", (28,), (400_000, 800_000),  "Quà cuối tháng cho sếp"),
    ("c_huy",      "friends", "random", (1, 2), (150_000, 400_000),    "Chia tiền nhậu"),
    ("c_nam_dev",  "work",    "random", (1, 2), (100_000, 200_000),    "Cafe / chia tiền"),
]

CONTACTS = {p[0] for p in PATTERNS}


def _ts(year: int, month: int, day: int, hour: int = 9) -> str:
    return datetime(year, month, day, hour, random.randint(0, 59), 0).isoformat() + TZ_SUFFIX


def _days_in(y: int, m: int) -> int:
    import calendar
    return calendar.monthrange(y, m)[1]


def _emit(rows: list, contact_id: str, when: str, amount: int, desc: str, category: str) -> None:
    rows.append({
        "id": f"tg_{uuid.uuid4().hex[:8]}",
        "owner_id": USER,
        "contact_id": contact_id,
        "amount": amount,
        "description": desc,
        "category": category,
        "status": "completed",
        "created_at": when,
        "embedding": None,
    })


def generate() -> list[dict]:
    rows: list[dict] = []
    months = [(2026, m) for m in range(1, 7)]  # Jan → Jun

    for cid, cat, kind, params, (amt_lo, amt_hi), desc_tpl in PATTERNS:
        for year, month in months:
            n_days = _days_in(year, month)
            if kind == "dom":
                target_day, jitter = params
                day = max(1, min(n_days, target_day + random.randint(-jitter, jitter)))
                _emit(rows, cid, _ts(year, month, day), random.randint(amt_lo, amt_hi),
                      f"{desc_tpl} {month}", cat)
            elif kind == "dom_once":
                target_day = params[0]
                if random.random() < 0.7:  # not every month
                    day = max(1, min(n_days, target_day + random.randint(-2, 2)))
                    _emit(rows, cid, _ts(year, month, day), random.randint(amt_lo, amt_hi),
                          desc_tpl, cat)
            elif kind == "dow":
                target_dow = params[0]
                for day in range(1, n_days + 1):
                    if datetime(year, month, day).weekday() == target_dow:
                        if random.random() < 0.9:  # 90% adherence
                            _emit(rows, cid, _ts(year, month, day, hour=19 if target_dow == 5 else 7),
                                  random.randint(amt_lo, amt_hi), f"{desc_tpl} {month}/{day}", cat)
            elif kind == "dow_set":
                target_dows = set(params)
                for day in range(1, n_days + 1):
                    if datetime(year, month, day).weekday() in target_dows:
                        if random.random() < 0.6:  # not every match
                            _emit(rows, cid, _ts(year, month, day, hour=11),
                                  random.randint(amt_lo, amt_hi), desc_tpl, cat)
            elif kind == "dow_count":
                target_dow, n = params
                fridays = [d for d in range(1, n_days + 1)
                           if datetime(year, month, d).weekday() == target_dow]
                random.shuffle(fridays)
                for day in fridays[:n]:
                    _emit(rows, cid, _ts(year, month, day, hour=18),
                          random.randint(amt_lo, amt_hi), desc_tpl, cat)
            elif kind == "random":
                lo, hi = params
                n = random.randint(lo, hi)
                days = random.sample(range(1, n_days + 1), min(n, n_days))
                for day in days:
                    _emit(rows, cid, _ts(year, month, day, hour=random.randint(8, 21)),
                          random.randint(amt_lo, amt_hi), desc_tpl, cat)

    rows.sort(key=lambda r: r["created_at"])
    return rows


def _insert(rows: list[dict]) -> None:
    conn = get_connection()
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM transactions WHERE owner_id = ?", (USER,))
        for r in rows:
            conn.execute(
                """INSERT INTO transactions
                   (id, owner_id, contact_id, amount, description, category,
                    status, created_at, embedding)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    r["id"], r["owner_id"], r["contact_id"], r["amount"],
                    r["description"], r["category"], r["status"],
                    r["created_at"], r["embedding"],
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


if __name__ == "__main__":
    rows = generate()
    print(f"Generated {len(rows)} transactions across {len(CONTACTS)} contacts.")
    print(f"  range: {rows[0]['created_at'][:10]} → {rows[-1]['created_at'][:10]}")
    by_month: dict[str, int] = {}
    for r in rows:
        m = r["created_at"][:7]
        by_month[m] = by_month.get(m, 0) + 1
    for m, n in sorted(by_month.items()):
        print(f"    {m}: {n} tx")
    _insert(rows)
    print("Inserted into SQLite. Now run eval_suggester.py.")

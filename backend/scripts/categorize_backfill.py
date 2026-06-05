"""Backfill `transactions.category` from the description text.

Scans the configured SQLite store for transactions whose category is
``other`` / ``omni`` / NULL / empty and asks ``app.ml.categorizer`` to
predict a better label from the description. Only rows where the
classifier returns a non-"other" prediction are updated, so meaningful
existing categories (food, family, work, …) are never overwritten and
ambiguous descriptions stay as "other".

Usage
-----
    .venv/bin/python -m scripts.categorize_backfill              # default DB
    OMNI_DB_PATH=app/data/omni_contest.db .venv/bin/python -m scripts.categorize_backfill

Pass ``--dry-run`` to see the breakdown without writing.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.connection import get_connection  # noqa: E402
from app.ml.categorizer import categorize  # noqa: E402
from app.store import get_store  # noqa: E402

# Ensure the JSON seed has been bootstrapped into omni.db before we scan
# for transactions to categorise. Idempotent — re-runs are no-ops once
# the DB has rows.
get_store()

# Existing category values we treat as "unset" — these are either the
# default ("other") or the legacy placeholder ("omni") that older transfers
# wrote before the categorizer existed. Real categories from the seed
# JSON ("food", "family", "health", …) are left untouched.
_REPLACEABLE: set[str] = {"other", "omni", "", "daily"}


def backfill(dry_run: bool = False) -> dict:
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, description, category
           FROM transactions
           WHERE category IS NULL OR category IN ('other', 'omni', '', 'daily')"""
    ).fetchall()

    breakdown: Counter[str] = Counter()
    skipped = 0
    samples: dict[str, list[tuple[str, str]]] = {}
    updates: list[tuple[str, str]] = []

    for r in rows:
        desc = r["description"] or ""
        before = r["category"] or "other"
        cat, conf = categorize(desc)
        # Only commit a change when we're moving away from "other" with
        # meaningful confidence. Confidence floor of 0.5 keeps weak
        # TF-IDF guesses from polluting the dataset.
        if cat == "other" or cat == before:
            skipped += 1
            continue
        if conf < 0.5:
            skipped += 1
            continue
        updates.append((cat, r["id"]))
        breakdown[cat] += 1
        # Keep up to 3 example descriptions per category for the report.
        samples.setdefault(cat, [])
        if len(samples[cat]) < 3:
            samples[cat].append((desc, f"{conf:.2f}"))

    if not dry_run and updates:
        with conn:
            conn.executemany(
                "UPDATE transactions SET category = ? WHERE id = ?", updates
            )

    return {
        "scanned": len(rows),
        "updated": len(updates),
        "skipped": skipped,
        "breakdown": dict(breakdown),
        "samples": samples,
    }


def _format_report(stats: dict, dry_run: bool) -> str:
    lines = [
        "Categorize backfill" + (" (DRY RUN)" if dry_run else ""),
        "-" * 50,
        f"  scanned          : {stats['scanned']}",
        f"  updated          : {stats['updated']}",
        f"  skipped (other)  : {stats['skipped']}",
        "",
        "Breakdown by new category:",
    ]
    for cat, n in sorted(
        stats["breakdown"].items(), key=lambda kv: kv[1], reverse=True
    ):
        lines.append(f"  {cat:<14} {n}")
        for desc, conf in stats["samples"].get(cat, []):
            lines.append(f"      ex: {desc!r}  (conf {conf})")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing to the DB.",
    )
    args = parser.parse_args()
    stats = backfill(dry_run=args.dry_run)
    print(_format_report(stats, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Honest evaluation of `app/banking/recurring.py` against Czech PKDD'99
ground truth.

The Czech bank's own `permanent_orders` (fin_order.tsv) IS the ground
truth — every row is a real recurring payment the customer set up at
the bank. We load it into a separate `permanent_orders` table via
`load_czech.py` and never let the recurring detector see it.

For each of the 5 demo users:
  1. Pull all outgoing transactions from the DB (the detector's input).
  2. Run `detect_recurring()` with default args.
  3. Compare detected patterns against the user's `permanent_orders`
     rows, matching on:
       - contact_id (already aligned by the loader)
       - typical_amount within ±10% of order's amount
  4. Report precision / recall / F1.

We DO NOT match on category — the orders' `HH/IN/LO` mapping is mostly
implicit in the contact (an other-bank account that always receives
HH payments), and the detector doesn't predict category anyway.

Caveat: the Czech bank's "permanent_orders" are *bank-side* recurring
setups, but the dataset also contains plenty of de-facto monthly
patterns the customer never formally registered as a permanent order
(salary credits, regular cash withdrawals). Those will look like false
positives here even though they are real recurrences — so the recall
number is the more meaningful signal; precision is a lower bound.
"""

from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Make sure we read the Czech DB.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")
os.environ.setdefault(
    "OMNI_DB_PATH",
    str(ROOT / "app" / "data" / "omni_czech.db"),
)

from app.banking.recurring import detect_recurring  # noqa: E402
from app.db.connection import get_connection  # noqa: E402
from app.models.schemas import Transaction  # noqa: E402


AMOUNT_TOLERANCE = 0.10  # ±10%


def _load_users(conn) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT id FROM users ORDER BY id")]


def _load_user_txs(conn, owner: str) -> list[Transaction]:
    rows = conn.execute(
        """SELECT id, owner_id, contact_id, amount, description, category,
                  status, created_at
           FROM transactions
           WHERE owner_id = ?
           ORDER BY created_at""",
        (owner,),
    ).fetchall()
    return [
        Transaction(
            id=r["id"],
            owner_id=r["owner_id"],
            contact_id=r["contact_id"],
            amount=r["amount"],
            description=r["description"] or "",
            category=r["category"] or "other",
            status=r["status"],
            created_at=datetime.fromisoformat(r["created_at"]),
        )
        for r in rows
    ]


def _load_user_orders(conn, owner: str) -> list[dict]:
    rows = conn.execute(
        """SELECT order_id, contact_id, amount, category
           FROM permanent_orders WHERE owner_id = ?""",
        (owner,),
    ).fetchall()
    return [dict(r) for r in rows]


def _match_orders_against_patterns(
    orders: list[dict], patterns: list
) -> tuple[set[int], set[int]]:
    """Return (matched_order_ids, matched_pattern_indexes)."""
    matched_orders: set[int] = set()
    matched_patterns: set[int] = set()

    # Index patterns by contact_id for fast lookup.
    pat_by_contact: dict[str, list[tuple[int, object]]] = defaultdict(list)
    for i, p in enumerate(patterns):
        pat_by_contact[p.contact_id].append((i, p))

    for o in orders:
        order_amt = o["amount"]
        for i, p in pat_by_contact.get(o["contact_id"], []):
            if i in matched_patterns:
                continue
            ratio = p.typical_amount / max(order_amt, 1)
            if abs(ratio - 1.0) <= AMOUNT_TOLERANCE:
                matched_orders.add(o["order_id"])
                matched_patterns.add(i)
                break
    return matched_orders, matched_patterns


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


def main() -> None:
    conn = get_connection()
    users = _load_users(conn)
    print(f"Czech demo users: {users}")
    print(f"DB: {os.environ['OMNI_DB_PATH']}")
    print()

    tot_tp = tot_fp = tot_fn = 0
    rows = []
    for owner in users:
        txs = _load_user_txs(conn, owner)
        orders = _load_user_orders(conn, owner)
        if not txs:
            print(f"  {owner}: no tx — skipped")
            continue
        if not orders:
            # User has no permanent orders → every detected pattern is a
            # false positive against ground truth (but, see caveat in docstring).
            patterns = detect_recurring(txs)
            print(
                f"  {owner}: 0 ground-truth orders, "
                f"{len(patterns)} detected (skipping P/R — no positives)"
            )
            continue

        t0 = time.perf_counter()
        patterns = detect_recurring(txs)
        dt = time.perf_counter() - t0

        matched_orders, matched_patterns = _match_orders_against_patterns(
            orders, patterns
        )
        tp = len(matched_orders)
        fn = len(orders) - tp
        fp = len(patterns) - len(matched_patterns)
        p, r, f = _prf(tp, fp, fn)
        rows.append((owner, len(txs), len(orders), len(patterns), tp, fp, fn, p, r, f, dt))
        tot_tp += tp
        tot_fp += fp
        tot_fn += fn

    print()
    print(
        f"{'user':14s} {'tx':>6s} {'orders':>7s} {'detected':>9s} "
        f"{'TP':>3s} {'FP':>3s} {'FN':>3s} "
        f"{'prec':>6s} {'rec':>6s} {'F1':>6s} {'secs':>6s}"
    )
    for row in rows:
        owner, n_tx, n_ord, n_det, tp, fp, fn, p, r, f, dt = row
        print(
            f"{owner:14s} {n_tx:>6d} {n_ord:>7d} {n_det:>9d} "
            f"{tp:>3d} {fp:>3d} {fn:>3d} "
            f"{p:>6.3f} {r:>6.3f} {f:>6.3f} {dt:>6.2f}"
        )
    p, r, f = _prf(tot_tp, tot_fp, tot_fn)
    print(
        f"\n{'AGGREGATE':14s} {'':>6s} {'':>7s} {'':>9s} "
        f"{tot_tp:>3d} {tot_fp:>3d} {tot_fn:>3d} "
        f"{p:>6.3f} {r:>6.3f} {f:>6.3f}"
    )


if __name__ == "__main__":
    main()

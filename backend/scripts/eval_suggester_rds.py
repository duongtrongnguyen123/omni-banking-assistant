"""Hit@K on the prod RDS dataset using a time-ordered holdout.

Strategy
--------
* Train window: first ``--train-days`` of the dataset (default Jan 2025).
* Test window: every outgoing tx after the train window.
* Predictor: frequency-prior — for each test tx, the top-K guess is the K
  most-frequent recipients in the train window, ordered by count desc.
  This is the simplest non-trivial baseline; the local SQLite tree/freq
  hybrid lives in ``app/ml/suggester.py`` but it depends on the local
  store interface, so we'd need a Postgres adapter to reuse it. The
  frequency prior is what the auto-weighted model collapses to for
  rich-data users anyway, so it's a fair lower-bound number.

Output: Hit@1 / Hit@3 / Hit@5 + total wall time + counts so the team can
decide whether to invest in a real PG adapter or stick with BankSim
(the public dataset already in `docs/eval-real-data.md`) for the pitch.

Run:
    .venv/bin/python scripts/eval_suggester_rds.py
"""

from __future__ import annotations

import argparse
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import psycopg2

DB_HOST = os.environ.get(
    "OMNI_RDS_HOST", "omni-banking-db.c23kswskyheq.us-east-1.rds.amazonaws.com"
)
DB_USER = os.environ.get("OMNI_RDS_USER", "postgres")
DB_PASS = os.environ.get("OMNI_RDS_PASSWORD", "Omni123456")
DB_NAME = os.environ.get("OMNI_RDS_DB", "postgres")
USER_ID = os.environ.get("OMNI_RDS_USER_ID", "u_an")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-days", type=int, default=31,
                        help="Train window length in days from the first tx.")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--test-limit", type=int, default=None,
                        help="Optional cap on test rows for faster iteration.")
    args = parser.parse_args()

    print(f"Connecting to {DB_HOST} …")
    conn = psycopg2.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASS, dbname=DB_NAME,
    )
    cur = conn.cursor()

    t0 = time.perf_counter()
    cur.execute(
        "SELECT min(created_at), max(created_at), count(*) "
        "FROM transactions WHERE owner_id = %s AND contact_id IS NOT NULL",
        (USER_ID,),
    )
    min_ts, max_ts, n_total = cur.fetchone()
    print(f"  range {min_ts} → {max_ts}, n={n_total}")

    train_end = min_ts + timedelta(days=args.train_days)
    print(f"  train ≤ {train_end} ({args.train_days} days)")

    cur.execute(
        "SELECT contact_id FROM transactions "
        "WHERE owner_id = %s AND contact_id IS NOT NULL "
        "  AND created_at < %s",
        (USER_ID, train_end),
    )
    train_rows = cur.fetchall()
    freqs = Counter(r[0] for r in train_rows)
    n_train = len(train_rows)
    n_recipients = len(freqs)
    print(f"  train: {n_train} tx · {n_recipients} unique recipients")

    if not freqs:
        print("  ! train window empty")
        return 1

    top_overall = [cid for cid, _ in freqs.most_common(args.top_k)]
    print(f"  top-{args.top_k} train recipients: {top_overall}")

    sql = (
        "SELECT contact_id FROM transactions "
        "WHERE owner_id = %s AND contact_id IS NOT NULL "
        "  AND created_at >= %s "
        "ORDER BY created_at"
    )
    params: list = [USER_ID, train_end]
    if args.test_limit is not None:
        sql += " LIMIT %s"
        params.append(args.test_limit)
    cur.execute(sql, params)
    test_rows = cur.fetchall()
    n_test = len(test_rows)
    print(f"  test: {n_test} tx")

    # Hit@K: did the true next-recipient appear in our top-K prediction?
    # Prediction is the global frequency-prior topK — same for every test row
    # (no time decay in this baseline). The tree-based suggester re-ranks
    # per-time-of-day but is out of scope here.
    hit1 = hit3 = hit5 = 0
    for (cid,) in test_rows:
        if cid == top_overall[0]:
            hit1 += 1
        if cid in top_overall[:3]:
            hit3 += 1
        if cid in top_overall[:5]:
            hit5 += 1

    elapsed = time.perf_counter() - t0
    print()
    print("Results")
    print(f"  Hit@1 = {hit1 / max(n_test, 1):.4f}  ({hit1}/{n_test})")
    print(f"  Hit@3 = {hit3 / max(n_test, 1):.4f}  ({hit3}/{n_test})")
    print(f"  Hit@5 = {hit5 / max(n_test, 1):.4f}  ({hit5}/{n_test})")
    print(f"  wall  = {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

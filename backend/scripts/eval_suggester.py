"""Time-ordered holdout evaluation for the next-recipient suggester.

Splits the user's transaction history at the 80/20 mark by ``created_at``.
Trains on the early 80%, then for every transaction in the held-out tail
asks ``suggest()`` *as of that moment* and records whether the true
recipient was in the top-K.

Reports Hit@1 / Hit@3 / Hit@5 plus per-component ablation (tree-only,
freq-only, rules-only, full hybrid) so you can see what's actually pulling
the rank.

Implementation note: ``train_for(txs=...)`` lets us feed the model a
training slice without touching the DB — crucial on 500k-row contest data
where the old delete-reinsert dance took 10+ minutes.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.connection import get_connection  # noqa: E402
from app.ml import suggester  # noqa: E402
from app.models.schemas import Transaction  # noqa: E402
from app.store import get_store  # noqa: E402


USER = "u_an"


def _row_to_tx(row: dict) -> Transaction:
    return Transaction(
        id=row["id"],
        owner_id=row["owner_id"],
        contact_id=row["contact_id"] or "",
        amount=row["amount"],
        description=row["description"] or "",
        category=row["category"] or "other",
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def evaluate(weights: tuple[float, float, float], label: str) -> dict:
    tw, fw, rw = weights
    contact_ids = {c.id for c in get_store().contacts_of(USER)}

    hit_at = {1: 0, 3: 0, 5: 0}
    n = 0
    skipped = 0
    for tx in TEST:
        if tx["contact_id"] not in contact_ids:
            skipped += 1
            continue
        when = datetime.fromisoformat(tx["created_at"])
        result = suggester.suggest(
            USER, when=when, k=20, include_all=False,
            tree_weight=tw, freq_weight=fw, rule_weight=rw,
        )
        ranked = [r["contact"]["id"] for r in result]
        for k in hit_at:
            if tx["contact_id"] in ranked[:k]:
                hit_at[k] += 1
        n += 1

    return {
        "label": label,
        "weights": weights,
        "n_test": n,
        "skipped": skipped,
        **{f"hit@{k}": hit_at[k] / max(n, 1) for k in hit_at},
    }


def _print(row: dict) -> None:
    print(
        f"  {row['label']:24s}  "
        f"tw={row['weights'][0]:.2f} fw={row['weights'][1]:.2f} rw={row['weights'][2]:.2f}  "
        f"hit@1={row['hit@1']:.2f}  hit@3={row['hit@3']:.2f}  hit@5={row['hit@5']:.2f}  "
        f"(n={row['n_test']})"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    conn = get_connection()
    ALL = [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE owner_id = ? ORDER BY created_at",
        (USER,),
    ).fetchall()]

    if len(ALL) < 10:
        print(f"Only {len(ALL)} transactions — too few to evaluate.")
        sys.exit(0)

    cut = int(len(ALL) * 0.8)
    TRAIN, TEST = ALL[:cut], ALL[cut:]
    # Cap the test set so eval stays under a minute even on 500k-row datasets.
    # The eval is still time-ordered and from the held-out tail.
    TEST_LIMIT = int(os.environ.get("EVAL_TEST_LIMIT", "1500"))
    if len(TEST) > TEST_LIMIT:
        TEST = TEST[-TEST_LIMIT:]

    # Filter test rows to contacts with ≥ MIN_TRAIN_TX in the train window —
    # one-shot contacts can't be predicted and just deflate Hit@K.
    MIN_TRAIN = int(os.environ.get("EVAL_MIN_TRAIN", "5"))
    from collections import Counter
    train_count = Counter(r["contact_id"] for r in TRAIN)
    TEST = [r for r in TEST if train_count[r["contact_id"]] >= MIN_TRAIN]

    print(f"Train: {len(TRAIN):,} tx ({TRAIN[0]['created_at'][:10]} → {TRAIN[-1]['created_at'][:10]})")
    print(f"Test : {len(TEST):,} tx (≥{MIN_TRAIN} train hits each, capped {TEST_LIMIT})")
    print()

    try:
        # Feed the training slice to the model directly — no DB writes.
        train_txs = [_row_to_tx(r) for r in TRAIN]
        suggester.train_for(USER, txs=train_txs)

        rows = [
            evaluate((1.0, 0.0, 0.0), "tree only"),
            evaluate((0.0, 1.0, 0.0), "freq only"),
            evaluate((0.0, 0.0, 1.0), "rule only"),
            evaluate((0.0, 0.5, 0.5), "rule + freq (no tree)"),
            evaluate((0.60, 0.40, 0.00), "tree + freq (no rule)"),
            evaluate((0.35, 0.25, 0.40), "balanced hybrid"),
            evaluate((0.55, 0.30, 0.15), "tree-heavy"),
            evaluate((0.20, 0.20, 0.60), "rule-heavy"),
        ]
        for r in rows:
            _print(r)
    finally:
        suggester.reset_all()

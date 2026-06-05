"""Time-ordered holdout evaluation for the next-recipient suggester.

Splits the user's transaction history at the 80/20 mark by ``created_at``.
Trains on the early 80%, then for every transaction in the held-out tail
asks ``suggest()`` *as of that moment* and records whether the true
recipient was in the top-K.

Reports Hit@1 / Hit@3 / Hit@5 plus per-component ablation (tree-only,
freq-only, rules-only, full hybrid) so you can see what's actually pulling
the rank.
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
from app.store import get_store  # noqa: E402


USER = "u_an"


def _restore_full(snapshot: list[dict]) -> None:
    """Repopulate the transactions table from a snapshot of dict rows."""
    conn = get_connection()
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM transactions")
        for r in snapshot:
            conn.execute(
                """INSERT INTO transactions
                    (id, owner_id, contact_id, amount, description,
                     category, status, created_at, embedding)
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


def _set_active(rows: list[dict]) -> None:
    """Replace the transactions table with `rows`. Used to swap train/test
    sets so suggester sees only the training portion."""
    _restore_full(rows)
    suggester.reset_all()


def evaluate(weights: tuple[float, float, float], label: str) -> dict:
    """Run the eval against the current train set with the given weights."""
    tw, fw, rw = weights
    store = get_store()
    contacts = store.contacts_of(USER)

    # Build a list of contacts the suggester can return; if test contact
    # isn't in there, it's a definite miss.
    contact_ids = {c.id for c in contacts}

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
    print(f"Train: {len(TRAIN)} tx ({TRAIN[0]['created_at'][:10]} → {TRAIN[-1]['created_at'][:10]})")
    print(f"Test : {len(TEST)} tx ({TEST[0]['created_at'][:10]} → {TEST[-1]['created_at'][:10]})")
    print()

    try:
        _set_active(TRAIN)
        suggester.train_for(USER)

        rows = [
            evaluate((1.0, 0.0, 0.0), "tree only"),
            evaluate((0.0, 1.0, 0.0), "freq only"),
            evaluate((0.0, 0.0, 1.0), "rule only"),
            evaluate((0.60, 0.40, 0.00), "tree + freq (no rule)"),
            evaluate((0.35, 0.25, 0.40), "hybrid (default)"),
            evaluate((0.20, 0.20, 0.60), "rule-heavy"),
        ]
        for r in rows:
            _print(r)
    finally:
        # Restore the original DB state so the running uvicorn (if any)
        # doesn't see a mutilated transactions table.
        _set_active(ALL)
        suggester.reset_all()

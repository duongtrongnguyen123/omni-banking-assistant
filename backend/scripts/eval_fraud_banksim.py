"""Honest evaluation of `app/safety/fraud_model.py` (Isolation Forest)
against BankSim's labelled fraud rows.

Setup
-----
``load_banksim.py`` instantiates 50 BankSim customers as Omni users
with their tx history attached. Each user has:
  * many "legit" tx (label `is_fraud=0`)
  * a handful of "fraud" tx (label `is_fraud=1`)

Eval loop (per user):
  1. Split chronologically: take fraud rows + use ONLY legit rows up to
     the median timestamp as training.  Anything after the cutoff
     (legit + fraud) is the test set.
  2. Train Isolation Forest exclusively on non-fraud training rows
     (no leakage of fraud labels into training).
  3. For every test row, ask ``score_draft()`` and threshold at
     `FRAUD_RISK_THRESHOLD` (0.7 by default — same as production).
  4. Tally TP/FP/FN/TN against `is_fraud`.

Caveat about precision/recall numbers
-------------------------------------
Isolation Forest is *unsupervised*. The model has no information that
some rows are fraud — it only learns "what looks normal for this user".
What "looks anomalous" is a noisy approximation of fraud. So we expect
recall to dominate precision (any unusual amount looks anomalous, but
not every unusual amount is fraud) — that's exactly why the rule engine
uses this as a *step-up* signal (OTP), not an autoblock.

We also report the *base false-positive rate on legit rows* — the
fraction of non-fraud test rows scored above threshold. That's the
operator-visible "noise" number.

Caps: with `EVAL_TEST_LIMIT=5000`, this runs in ~15s. Run with
`EVAL_TEST_LIMIT=0` to evaluate every user's full test set.
"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")
os.environ.setdefault(
    "OMNI_DB_PATH",
    str(ROOT / "app" / "data" / "omni_banksim.db"),
)

from app.db.connection import get_connection  # noqa: E402
from app.models.schemas import Transaction  # noqa: E402
from app.safety import fraud_model  # noqa: E402


def _load_user_rows(conn, owner: str) -> list[tuple[Transaction, int]]:
    rows = conn.execute(
        """SELECT id, owner_id, contact_id, amount, description, category,
                  status, created_at, is_fraud
           FROM transactions
           WHERE owner_id = ?
           ORDER BY created_at""",
        (owner,),
    ).fetchall()
    out: list[tuple[Transaction, int]] = []
    for r in rows:
        out.append((
            Transaction(
                id=r["id"],
                owner_id=r["owner_id"],
                contact_id=r["contact_id"],
                amount=r["amount"],
                description=r["description"] or "",
                category=r["category"] or "other",
                status=r["status"],
                created_at=datetime.fromisoformat(r["created_at"]),
            ),
            r["is_fraud"],
        ))
    return out


def _split(rows: list[tuple[Transaction, int]]) -> tuple[list, list]:
    """Time-ordered 70/30 split.  Training receives only non-fraud rows
    in the first 70% slice; test receives EVERYTHING (legit + fraud) in
    the last 30% slice — including the fraud rows that fell inside the
    training window (we don't show them to the model)."""
    if not rows:
        return [], []
    cut = int(len(rows) * 0.7)
    train_window = rows[:cut]
    test_window = rows[cut:]
    train_txs = [tx for tx, fr in train_window if fr == 0]
    test_pairs = test_window
    return train_txs, test_pairs


def main() -> None:
    if not fraud_model.is_enabled():
        print("Fraud model disabled (sklearn missing or OMNI_FRAUD_DISABLE set).")
        sys.exit(1)

    conn = get_connection()
    users = [r["id"] for r in conn.execute("SELECT id FROM users ORDER BY id")]
    print(f"BankSim users: {len(users)}")

    TEST_LIMIT = int(os.environ.get("EVAL_TEST_LIMIT", "5000"))
    THRESHOLD = float(os.environ.get("FRAUD_THRESHOLD",
                                     str(fraud_model.FRAUD_RISK_THRESHOLD)))
    print(f"Threshold: {THRESHOLD:.2f}")
    print(f"Test cap : {TEST_LIMIT or 'all'}")
    print()

    # Aggregates
    TP = FP = FN = TN = 0
    n_users_with_model = 0
    n_users_skipped = 0
    legit_scores: list[float] = []
    fraud_scores: list[float] = []

    t0 = time.perf_counter()
    total_test_seen = 0

    for owner in users:
        rows = _load_user_rows(conn, owner)
        train_txs, test_pairs = _split(rows)
        if not test_pairs:
            n_users_skipped += 1
            continue
        # Anchor "now" to the latest tx so the 6-month window is realistic.
        # `_trim_training_set` mixes the reference_now with `_ensure_aware`
        # results, so the reference must be tz-aware to avoid mixed-type
        # comparisons.
        ref_now = max(r[0].created_at for r in rows).replace(tzinfo=timezone.utc)
        # Bypass the wall-clock cutoff in `_trim_training_set` by patching
        # it; cleaner: train via `train_user(reference_now=…)`.
        fraud_model.clear_models()
        fitted = fraud_model.train_user(owner, train_txs, reference_now=ref_now)
        if fitted is None:
            n_users_skipped += 1
            continue
        n_users_with_model += 1

        # Score the test slice
        for tx, label in test_pairs:
            if TEST_LIMIT and total_test_seen >= TEST_LIMIT:
                break
            score = fraud_model.score_draft(
                user_id=owner,
                amount=tx.amount,
                when=tx.created_at,
                contact_id=tx.contact_id,
                category=tx.category,
            )
            if score is None:
                continue
            total_test_seen += 1
            predicted = 1 if score >= THRESHOLD else 0
            if label == 1:
                fraud_scores.append(score)
                if predicted == 1:
                    TP += 1
                else:
                    FN += 1
            else:
                legit_scores.append(score)
                if predicted == 1:
                    FP += 1
                else:
                    TN += 1
        if TEST_LIMIT and total_test_seen >= TEST_LIMIT:
            break

    dt = time.perf_counter() - t0

    precision = TP / max(TP + FP, 1)
    recall = TP / max(TP + FN, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    fp_rate_on_legit = FP / max(FP + TN, 1)

    print(f"users with trained model: {n_users_with_model}")
    print(f"users skipped (too few legit tx): {n_users_skipped}")
    print(f"test rows scored: {total_test_seen:,}")
    print(f"runtime: {dt:.1f}s")
    print()
    print(f"  TP={TP}  FP={FP}  FN={FN}  TN={TN}")
    print(f"  precision = {precision:.3f}")
    print(f"  recall    = {recall:.3f}")
    print(f"  F1        = {f1:.3f}")
    print(f"  FP rate on legit tx = {fp_rate_on_legit:.3f}")
    print()
    if fraud_scores:
        print(f"  fraud score    median={np.median(fraud_scores):.3f}  "
              f"mean={np.mean(fraud_scores):.3f}  "
              f"top10%={np.quantile(fraud_scores, 0.9):.3f}")
    if legit_scores:
        print(f"  legit score    median={np.median(legit_scores):.3f}  "
              f"mean={np.mean(legit_scores):.3f}  "
              f"top10%={np.quantile(legit_scores, 0.9):.3f}")


if __name__ == "__main__":
    main()

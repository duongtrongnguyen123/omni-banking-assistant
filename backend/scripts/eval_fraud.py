"""Evaluate the per-user fraud Isolation Forest.

What it does
------------
1. Loads transactions for the demo user from whichever store the
   environment points at (``BANKING_DATA_DIR``). For the contest
   evaluation, point this at ``../data/demo`` or load the 521k-row
   contest CSV directly via ``--contest-csv``.
2. Holds out the *last 10%* of transactions as the legit test set —
   anything before that is the training window.
3. Synthesises ``--n-fraud`` fraudulent transactions: very large
   amounts to never-before-seen recipients at unusual hours.
4. Trains the model on training-window data, then scores both the
   legit holdout and the synthetic fraud, reporting:
       * precision / recall of ``fraud_risk_high`` flag
       * base-rate false-positive % on legit transactions

Run
---
    # demo seed (1 user, ~1.9k tx)
    BANKING_DATA_DIR=../data/demo \\
      python scripts/eval_fraud.py

    # 521k contest CSV — bypass the JSON store, build Transaction
    # objects directly from the enriched CSV.
    python scripts/eval_fraud.py --contest-csv ../generated/transactions_enriched_6m.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Make the `app.*` imports resolve when this file is run directly.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.models.schemas import Transaction  # noqa: E402
from app.safety import fraud_model  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
logger = logging.getLogger("eval_fraud")


def _log(msg: str, *args) -> None:
    """Logging that actually flushes — Python logging buffers on macOS
    when stdout is redirected to a file."""
    text = msg % args if args else msg
    print(text, flush=True)


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _load_demo_user() -> tuple[str, list[Transaction]]:
    """Loads via the bootstrapped Store — picks the user with the most tx."""
    from app.store import get_store

    store = get_store()
    best_user: Optional[str] = None
    best_count = -1
    for uid in store.users:
        n = len(store.transactions_of(uid))
        if n > best_count:
            best_user, best_count = uid, n
    if not best_user:
        raise SystemExit("No users in store.")
    return best_user, store.transactions_of(best_user)


def _load_contest_csv(path: Path, max_rows: int) -> tuple[str, list[Transaction]]:
    """Builds Transaction objects from the contest enriched CSV.

    The CSV is one user (``sender_id == u_an``) — we just stream
    outgoing rows up to ``max_rows`` newest.
    """
    rows: list[Transaction] = []
    with path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r.get("direction", "").lower() != "outgoing":
                continue
            if r.get("status", "").lower() not in ("completed", ""):
                continue
            try:
                amount = int(float(r["amount_vnd"]))
                created = datetime.fromisoformat(r["transaction_at"])
            except (KeyError, ValueError):
                continue
            tx = Transaction(
                id=r.get("transaction_id") or f"row{len(rows)}",
                owner_id=r.get("sender_id") or "u_an",
                contact_id=r.get("receiver_id") or "",
                amount=amount,
                description=r.get("note_normalized") or r.get("note_raw") or "",
                category=r.get("category") or "other",
                status="completed",
                created_at=created,
            )
            rows.append(tx)
    rows.sort(key=lambda t: t.created_at)
    if max_rows and len(rows) > max_rows:
        rows = rows[-max_rows:]
    user = rows[-1].owner_id if rows else "u_an"
    return user, rows


def _synthesize_fraud(
    user_id: str, txs: list[Transaction], n: int, seed: int = 42
) -> list[tuple[int, datetime, str]]:
    """Generate `n` synthetic fraud signatures: very large amount to a
    never-seen recipient at an unusual hour."""
    rng = random.Random(seed)
    seen_recipients = {t.contact_id for t in txs}
    legit_amounts = [t.amount for t in txs if t.amount > 0]
    if not legit_amounts:
        return []
    p99 = sorted(legit_amounts)[max(int(len(legit_amounts) * 0.99) - 1, 0)]
    base_after = max(t.created_at for t in txs)
    out: list[tuple[int, datetime, str]] = []
    for i in range(n):
        # 5x..50x the 99th percentile so this is unambiguously "huge"
        amount = int(p99 * rng.uniform(5, 50))
        # 1..120 minutes after the last legit tx, between 1-4am local
        when = base_after + timedelta(minutes=rng.randint(60, 24 * 60 * 30))
        when = when.replace(hour=rng.choice([1, 2, 3, 4]), minute=rng.randint(0, 59))
        # Brand-new "fraud recipient" never present in history
        fake_recipient = f"fraud_{i}"
        while fake_recipient in seen_recipients:
            fake_recipient += "x"
        out.append((amount, when, fake_recipient))
    return out


def _split_train_test(
    txs: list[Transaction], holdout_frac: float
) -> tuple[list[Transaction], list[Transaction]]:
    txs = sorted(txs, key=lambda t: t.created_at)
    cutoff = int(len(txs) * (1 - holdout_frac))
    return txs[:cutoff], txs[cutoff:]


def run(
    *,
    contest_csv: Optional[Path],
    n_fraud: int,
    holdout_frac: float,
    max_rows: int,
) -> int:
    t_load = time.perf_counter()
    if contest_csv is not None:
        user_id, all_txs = _load_contest_csv(contest_csv, max_rows=max_rows)
        source_label = f"contest CSV ({contest_csv.name})"
    else:
        user_id, all_txs = _load_demo_user()
        source_label = "demo seed"
    load_ms = (time.perf_counter() - t_load) * 1000
    _log(
        "Loaded %d completed tx for user '%s' from %s in %.1fms",
        len(all_txs),
        user_id,
        source_label,
        load_ms,
    )

    if len(all_txs) < fraud_model.MIN_TX_FOR_TRAINING:
        _log(
            "Not enough transactions (%d < %d) to train.",
            len(all_txs),
            fraud_model.MIN_TX_FOR_TRAINING,
        )
        return 1

    train_txs, test_txs = _split_train_test(all_txs, holdout_frac)
    _log(
        "Train window: %d tx (%s -> %s)",
        len(train_txs),
        train_txs[0].created_at.isoformat(timespec="minutes"),
        train_txs[-1].created_at.isoformat(timespec="minutes"),
    )
    _log(
        "Legit holdout: %d tx (%s -> %s)",
        len(test_txs),
        test_txs[0].created_at.isoformat(timespec="minutes"),
        test_txs[-1].created_at.isoformat(timespec="minutes"),
    )

    # Train ----------------------------------------------------------------
    fraud_model.clear_models()
    t_train = time.perf_counter()
    # Reference "now" = end of the train window so the 6-month rolling
    # cutoff lines up with the data even when replaying older history.
    ref_now = _ensure_aware(train_txs[-1].created_at)
    fitted = fraud_model.train_user(user_id, train_txs, reference_now=ref_now)
    train_ms = (time.perf_counter() - t_train) * 1000
    if fitted is None:
        _log("Training returned None — sklearn missing or too few rows.")
        return 1
    _log(
        "Trained Isolation Forest on %d rows in %.1fms (p50=%.3f p95=%.3f p99=%.3f).",
        fitted.n_train,
        train_ms,
        fitted.score_p50,
        fitted.score_p95,
        fitted.score_p99,
    )

    # Score legit holdout --------------------------------------------------
    threshold = fraud_model.FRAUD_RISK_THRESHOLD
    t_score = time.perf_counter()
    legit_scores: list[float] = []
    for tx in test_txs:
        s = fraud_model.score_draft(
            user_id=user_id,
            amount=tx.amount,
            when=_ensure_aware(tx.created_at),
            contact_id=tx.contact_id,
            category=tx.category,
        )
        if s is not None:
            legit_scores.append(s)
    legit_score_ms = (time.perf_counter() - t_score) * 1000

    # Score synthetic fraud ------------------------------------------------
    fraud_signatures = _synthesize_fraud(user_id, train_txs, n=n_fraud)
    t_score = time.perf_counter()
    fraud_scores: list[float] = []
    for amount, when, cid in fraud_signatures:
        s = fraud_model.score_draft(
            user_id=user_id,
            amount=amount,
            when=when,
            contact_id=cid,
            category="other",
        )
        if s is not None:
            fraud_scores.append(s)
    fraud_score_ms = (time.perf_counter() - t_score) * 1000

    # Metrics --------------------------------------------------------------
    tp = sum(1 for s in fraud_scores if s >= threshold)
    fn = len(fraud_scores) - tp
    fp = sum(1 for s in legit_scores if s >= threshold)
    tn = len(legit_scores) - fp

    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    fpr = fp / max(len(legit_scores), 1)

    avg_legit_ms = legit_score_ms / max(len(test_txs), 1)
    avg_fraud_ms = fraud_score_ms / max(len(fraud_signatures), 1)

    _log("")
    _log("=== Evaluation results ===")
    _log("threshold              : %.2f", threshold)
    _log("synthetic fraud (n)    : %d", len(fraud_scores))
    _log("legit holdout (n)      : %d", len(legit_scores))
    _log("true positives         : %d", tp)
    _log("false negatives        : %d", fn)
    _log("false positives        : %d", fp)
    _log("true negatives         : %d", tn)
    _log("precision              : %.3f", precision)
    _log("recall                 : %.3f", recall)
    _log("base-rate FP           : %.2f%%", fpr * 100)
    _log("inference latency      : %.3f ms (legit) %.3f ms (fraud)",
                avg_legit_ms, avg_fraud_ms)

    # Distribution snapshot for the report
    if legit_scores:
        ls = sorted(legit_scores)
        _log(
            "legit score quantiles  : p50=%.3f p90=%.3f p99=%.3f max=%.3f",
            ls[len(ls) // 2],
            ls[int(len(ls) * 0.9)],
            ls[int(len(ls) * 0.99)],
            ls[-1],
        )
    if fraud_scores:
        fs = sorted(fraud_scores)
        _log(
            "fraud score quantiles  : p10=%.3f p50=%.3f p90=%.3f max=%.3f",
            fs[max(int(len(fs) * 0.1) - 1, 0)],
            fs[len(fs) // 2],
            fs[int(len(fs) * 0.9)],
            fs[-1],
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--contest-csv",
        type=Path,
        default=None,
        help="Path to transactions_enriched_6m.csv (full-fat contest data)",
    )
    ap.add_argument("--n-fraud", type=int, default=100)
    ap.add_argument("--holdout", type=float, default=0.10)
    ap.add_argument(
        "--max-rows",
        type=int,
        default=600_000,
        help="Cap CSV rows for the contest evaluation",
    )
    args = ap.parse_args()

    return run(
        contest_csv=args.contest_csv,
        n_fraud=args.n_fraud,
        holdout_frac=args.holdout,
        max_rows=args.max_rows,
    )


if __name__ == "__main__":
    # Respect env override of the JSON store location for the non-CSV path.
    os.environ.setdefault("BANKING_DATA_DIR", os.environ.get("BANKING_DATA_DIR", ""))
    sys.exit(main())

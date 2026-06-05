"""Time-ordered holdout evaluation for the next-recipient suggester.

Splits the user's transaction history at the 80/20 mark by ``created_at``.
Trains on the early 80%, then for every transaction in the held-out tail
asks ``suggest()`` *as of that moment* and records whether the true
recipient was in the top-K.

Reports Hit@1 / Hit@3 / Hit@5 plus per-component ablation (tree-only,
freq-only, rules-only, full hybrid) so you can see what's actually pulling
the rank.

Performance
-----------
The full contest dataset is 520k tx × 1000 contacts. The previous
implementation copied the training slice into the DB (DELETE / INSERT) and
called ``suggester.suggest()`` for each test row (which re-queries the DB
for contacts + transaction reasons). On contest data that took ~10
minutes per ablation weight × 8 weights = effectively unrunnable.

This rewrite is **entirely in-memory after the initial DB read**:
  * Load all transactions once via ``SELECT … ORDER BY created_at``.
  * Train the RF on the train slice using the suggester's feature
    function directly — no DB roundtrip per call.
  * Score test rows with a vectorised loop: a single
    ``predict_proba`` per unique test ``when``, dict lookups for freq +
    rule scores. No store / contacts / reasons fetch in the hot loop.

A run with ``EVAL_TEST_LIMIT=2000`` on the full 520k-row DB completes in
~60-90s total (one RF train + 8 ablation passes).
"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.connection import get_connection  # noqa: E402
from app.ml import suggester  # noqa: E402


USER = "u_an"


# ---------------------------------------------------------------------------
# Lightweight Transaction surrogate so we can reuse suggester._per_contact_stats
# without paying Pydantic's per-row validation cost on 500k rows.
# ---------------------------------------------------------------------------


class _Tx:
    __slots__ = ("contact_id", "created_at", "category")

    def __init__(self, contact_id: str, created_at: datetime, category: str) -> None:
        self.contact_id = contact_id
        self.created_at = created_at
        self.category = category


def _load_all() -> list[_Tx]:
    """One DB read; everything downstream is pure Python."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT contact_id, created_at, category
           FROM transactions
           WHERE owner_id = ?
           ORDER BY created_at""",
        (USER,),
    ).fetchall()
    return [
        _Tx(r["contact_id"], datetime.fromisoformat(r["created_at"]),
            r["category"] or "other")
        for r in rows
    ]


def _train_in_memory(train_txs: list[_Tx]) -> dict:
    """Train the RF + per-contact stats directly off an in-memory tx list.

    Mirrors ``suggester.train_for`` but skips the store roundtrip and the
    ``threading`` cache write — we hold the resulting state object locally.
    """
    try:
        from sklearn.ensemble import RandomForestClassifier  # type: ignore
    except ImportError:
        raise RuntimeError("scikit-learn missing")

    X = [suggester._feature_vec(t.created_at) for t in train_txs]
    y = [t.contact_id for t in train_txs]
    labels = sorted(set(y))
    freq = Counter(y)
    total = len(y)
    prior = {c: freq[c] / total for c in labels}
    contact_stats = suggester._per_contact_stats(train_txs)

    rf_kwargs: dict
    if total >= 10_000:
        rf_kwargs = dict(n_estimators=20, max_depth=8, min_samples_leaf=5, n_jobs=-1)
    else:
        rf_kwargs = dict(n_estimators=50, max_depth=5, min_samples_leaf=1,
                         class_weight="balanced")
    model = RandomForestClassifier(random_state=42, **rf_kwargs)
    model.fit(X, y)

    return {
        "model": model,
        "labels": list(model.classes_),
        "prior": prior,
        "n": total,
        "contact_stats": contact_stats,
    }


def _score_all(state: dict, test_txs: list[_Tx], weights: tuple[float, float, float]) -> dict:
    """Score every test tx and return Hit@1/3/5.

    Key optimisation: many test rows share the same hour-of-day; cache the
    expensive ``predict_proba`` per (year, month, day, weekday) since the
    feature vector only depends on those date parts.
    """
    tw, fw, rw = weights
    labels = state["labels"]
    prior = state["prior"]
    stats = state["contact_stats"]
    model = state["model"]

    proba_cache: dict[tuple, dict[str, float]] = {}

    def proba_for(when: datetime) -> dict[str, float]:
        # _feature_vec only uses day, weekday, month — cache on that.
        key = (when.month, when.day, when.weekday())
        hit = proba_cache.get(key)
        if hit is not None:
            return hit
        vec = suggester._feature_vec(when)
        p = model.predict_proba([vec])[0]
        d = dict(zip(labels, p))
        proba_cache[key] = d
        return d

    hit_at = {1: 0, 3: 0, 5: 0}
    n = 0

    # Precompute rule scores per (contact, day, weekday). The rule scorer
    # is moderately expensive (dict lookups + arithmetic) and there are
    # only ~31 * 7 ~ 217 unique (day, weekday, month) keys per contact.
    rule_cache: dict[tuple, float] = {}

    def rule_for(cid: str, when: datetime) -> float:
        key = (cid, when.month, when.day, when.weekday())
        v = rule_cache.get(key)
        if v is not None:
            return v
        s = suggester._rule_score(stats.get(cid), when)
        rule_cache[key] = s
        return s

    for tx in test_txs:
        when = tx.created_at
        true_cid = tx.contact_id

        if model is not None and tw > 0:
            tree_proba = proba_for(when)
        else:
            tree_proba = {}

        # Score every candidate the model has seen.
        scored: list[tuple[float, str]] = []
        for cid in prior:
            p_tree = float(tree_proba.get(cid, 0.0)) if tw > 0 else 0.0
            p_freq = prior[cid] if fw > 0 else 0.0
            p_rule = rule_for(cid, when) if rw > 0 else 0.0
            mixed = tw * p_tree + fw * p_freq + rw * p_rule
            scored.append((mixed, cid))

        # Top-5 only — we don't need a full sort.
        scored.sort(reverse=True)
        top5 = [cid for _, cid in scored[:5]]
        if top5 and top5[0] == true_cid:
            hit_at[1] += 1
        if true_cid in top5[:3]:
            hit_at[3] += 1
        if true_cid in top5:
            hit_at[5] += 1
        n += 1

    return {
        "n_test": n,
        **{f"hit@{k}": hit_at[k] / max(n, 1) for k in hit_at},
    }


def evaluate(state: dict, test_txs: list[_Tx],
             weights: tuple[float, float, float], label: str) -> dict:
    t0 = time.perf_counter()
    res = _score_all(state, test_txs, weights)
    dt = time.perf_counter() - t0
    return {"label": label, "weights": weights, "secs": dt, **res}


def _print(row: dict) -> None:
    print(
        f"  {row['label']:24s}  "
        f"tw={row['weights'][0]:.2f} fw={row['weights'][1]:.2f} rw={row['weights'][2]:.2f}  "
        f"hit@1={row['hit@1']:.3f}  hit@3={row['hit@3']:.3f}  hit@5={row['hit@5']:.3f}  "
        f"(n={row['n_test']}, {row['secs']:.1f}s)"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    t_start = time.perf_counter()
    print("Loading transactions from DB…")
    ALL = _load_all()
    print(f"  loaded {len(ALL):,} tx in {time.perf_counter() - t_start:.1f}s")

    if len(ALL) < 10:
        print(f"Only {len(ALL)} transactions — too few to evaluate.")
        sys.exit(0)

    cut = int(len(ALL) * 0.8)
    TRAIN, TEST = ALL[:cut], ALL[cut:]

    TEST_LIMIT = int(os.environ.get("EVAL_TEST_LIMIT", "1500"))
    if len(TEST) > TEST_LIMIT:
        TEST = TEST[-TEST_LIMIT:]

    MIN_TRAIN = int(os.environ.get("EVAL_MIN_TRAIN", "5"))
    train_count = Counter(t.contact_id for t in TRAIN)
    train_contacts = {c for c, n in train_count.items() if n >= MIN_TRAIN}
    TEST = [t for t in TEST if t.contact_id in train_contacts]

    print(f"Train: {len(TRAIN):,} tx "
          f"({TRAIN[0].created_at.date()} → {TRAIN[-1].created_at.date()})")
    print(f"Test : {len(TEST):,} tx (filtered to contacts with ≥{MIN_TRAIN} train hits, "
          f"capped {TEST_LIMIT})")
    print(f"       {len(train_contacts):,} unique candidate contacts in train")
    print()

    t0 = time.perf_counter()
    print("Training RF + per-contact stats…")
    state = _train_in_memory(TRAIN)
    print(f"  done in {time.perf_counter() - t0:.1f}s "
          f"({len(state['labels'])} classes)")
    print()

    rows = [
        evaluate(state, TEST, (1.0, 0.0, 0.0), "tree only"),
        evaluate(state, TEST, (0.0, 1.0, 0.0), "freq only"),
        evaluate(state, TEST, (0.0, 0.0, 1.0), "rule only"),
        evaluate(state, TEST, (0.0, 0.5, 0.5), "rule + freq (no tree)"),
        evaluate(state, TEST, (0.60, 0.40, 0.00), "tree + freq (no rule)"),
        evaluate(state, TEST, (0.35, 0.25, 0.40), "balanced hybrid"),
        evaluate(state, TEST, (0.55, 0.30, 0.15), "tree-heavy"),
        evaluate(state, TEST, (0.20, 0.20, 0.60), "rule-heavy"),
    ]
    for r in rows:
        _print(r)

    print()
    print(f"Total wall time: {time.perf_counter() - t_start:.1f}s")

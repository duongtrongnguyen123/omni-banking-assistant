"""Next-merchant Hit@K evaluation against BankSim — the only HONEST
suggester evaluation we have.

The synthetic seed in `generate_synthetic_data.py` lets the model find
patterns we ourselves encoded — circular. The contest dataset is
uniformly distributed across 1000 contacts so even a perfect model can't
beat ~1/1000. BankSim has 50 merchants and customer behaviour with real
weekly + monthly seasonality, so this is the suggester's first honest
test.

For each BankSim user (loaded via `load_banksim.py`):

  * Sort the user's transactions by step (synthetic date).
  * Train-on-80 / test-on-20 by time, mirroring `eval_suggester.py`.
  * For every test transaction, rank candidate merchants using a
    fully-cached in-memory training loop borrowed from
    `eval_suggester.py` so the run stays under ~60s on the full
    9.5k-row dataset.
  * Report Hit@1 / Hit@3 / Hit@5 across the 8 standard ablation weights.

The merchants we're predicting are categorical destinations (es_food,
es_travel, …) — the realistic shape of recipient suggestion in a
banking UX. Hit@K on this corpus is the headline number we can quote
to judges.
"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")
os.environ.setdefault(
    "OMNI_DB_PATH",
    str(ROOT / "app" / "data" / "omni_banksim.db"),
)

from app.db.connection import get_connection  # noqa: E402
from app.ml import suggester  # noqa: E402


class _Tx:
    __slots__ = ("contact_id", "created_at", "category")

    def __init__(self, contact_id, created_at, category):
        self.contact_id = contact_id
        self.created_at = created_at
        self.category = category


def _load_user_txs(conn, user_id) -> list[_Tx]:
    rows = conn.execute(
        """SELECT contact_id, created_at, category
           FROM transactions WHERE owner_id = ?
           ORDER BY created_at""",
        (user_id,),
    ).fetchall()
    return [_Tx(r["contact_id"], datetime.fromisoformat(r["created_at"]),
                r["category"] or "other")
            for r in rows]


def _train(train_txs: list[_Tx]) -> dict:
    from sklearn.ensemble import RandomForestClassifier  # type: ignore

    X = [suggester._feature_vec(t.created_at) for t in train_txs]
    y = [t.contact_id for t in train_txs]
    labels = sorted(set(y))
    freq = Counter(y)
    total = len(y)
    prior = {c: freq[c] / total for c in labels}
    contact_stats = suggester._per_contact_stats(train_txs)

    if len(labels) < 2:
        return {
            "model": None, "labels": labels, "prior": prior,
            "n": total, "contact_stats": contact_stats,
        }

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


def _score(state: dict, test_txs: list[_Tx], weights) -> dict:
    tw, fw, rw = weights
    labels = state["labels"]
    prior = state["prior"]
    stats = state["contact_stats"]
    model = state["model"]

    proba_cache: dict = {}
    rule_cache: dict = {}

    def proba_for(when: datetime):
        key = (when.month, when.day, when.weekday())
        h = proba_cache.get(key)
        if h is not None:
            return h
        if model is None:
            proba_cache[key] = {}
            return {}
        vec = suggester._feature_vec(when)
        p = model.predict_proba([vec])[0]
        d = dict(zip(labels, p))
        proba_cache[key] = d
        return d

    def rule_for(cid: str, when: datetime) -> float:
        key = (cid, when.month, when.day, when.weekday())
        v = rule_cache.get(key)
        if v is not None:
            return v
        s = suggester._rule_score(stats.get(cid), when)
        rule_cache[key] = s
        return s

    hit = {1: 0, 3: 0, 5: 0}
    n = 0
    for tx in test_txs:
        when = tx.created_at
        tree_proba = proba_for(when) if tw > 0 else {}
        scored = []
        for cid in prior:
            p_tree = float(tree_proba.get(cid, 0.0)) if tw > 0 else 0.0
            p_freq = prior[cid] if fw > 0 else 0.0
            p_rule = rule_for(cid, when) if rw > 0 else 0.0
            scored.append((tw * p_tree + fw * p_freq + rw * p_rule, cid))
        scored.sort(reverse=True)
        top5 = [c for _, c in scored[:5]]
        if top5 and top5[0] == tx.contact_id:
            hit[1] += 1
        if tx.contact_id in top5[:3]:
            hit[3] += 1
        if tx.contact_id in top5:
            hit[5] += 1
        n += 1
    return {"n_test": n, **{f"hit@{k}": hit[k] / max(n, 1) for k in hit}}


WEIGHTS = [
    ((1.0, 0.0, 0.0), "tree only"),
    ((0.0, 1.0, 0.0), "freq only"),
    ((0.0, 0.0, 1.0), "rule only"),
    ((0.0, 0.5, 0.5), "rule + freq (no tree)"),
    ((0.60, 0.40, 0.00), "tree + freq (no rule)"),
    ((0.35, 0.25, 0.40), "balanced hybrid"),
    ((0.55, 0.30, 0.15), "tree-heavy"),
    ((0.20, 0.20, 0.60), "rule-heavy"),
]


def main() -> None:
    conn = get_connection()
    users = [r["id"] for r in conn.execute("SELECT id FROM users ORDER BY id")]
    print(f"BankSim users: {len(users)}")

    TEST_LIMIT = int(os.environ.get("EVAL_TEST_LIMIT", "5000"))
    MIN_TRAIN = int(os.environ.get("EVAL_MIN_TRAIN", "3"))

    # Pool every user's split into one big test set so the headline numbers
    # are averaged across users.  We still train one model per user — the
    # alternative (one global model) would conflate users' contact ids.
    all_results: dict[tuple, dict[str, list[float]]] = {}
    rows_by_weight = {w[1]: [] for w in WEIGHTS}
    test_total = 0
    train_total = 0
    skipped_users = 0
    t0 = time.perf_counter()

    for owner in users:
        txs = _load_user_txs(conn, owner)
        if len(txs) < 10:
            skipped_users += 1
            continue
        cut = int(len(txs) * 0.8)
        train, test = txs[:cut], txs[cut:]
        train_count = Counter(t.contact_id for t in train)
        train_contacts = {c for c, n in train_count.items() if n >= MIN_TRAIN}
        test = [t for t in test if t.contact_id in train_contacts]
        if not test:
            skipped_users += 1
            continue
        train_total += len(train)

        state = _train(train)
        for w, label in WEIGHTS:
            res = _score(state, test, w)
            rows_by_weight[label].append(res)
        test_total += len(test)
        if TEST_LIMIT and test_total >= TEST_LIMIT:
            break

    dt = time.perf_counter() - t0

    print(f"users skipped: {skipped_users}")
    print(f"users scored : {len(users) - skipped_users}")
    print(f"train tx total: {train_total:,}")
    print(f"test tx total : {test_total:,}")
    print(f"runtime: {dt:.1f}s")
    print()

    for w, label in WEIGHTS:
        results = rows_by_weight[label]
        if not results:
            continue
        n = sum(r["n_test"] for r in results)
        h1 = sum(r["hit@1"] * r["n_test"] for r in results) / max(n, 1)
        h3 = sum(r["hit@3"] * r["n_test"] for r in results) / max(n, 1)
        h5 = sum(r["hit@5"] * r["n_test"] for r in results) / max(n, 1)
        print(
            f"  {label:24s}  tw={w[0]:.2f} fw={w[1]:.2f} rw={w[2]:.2f}  "
            f"hit@1={h1:.3f}  hit@3={h3:.3f}  hit@5={h5:.3f}  (n={n})"
        )


if __name__ == "__main__":
    main()

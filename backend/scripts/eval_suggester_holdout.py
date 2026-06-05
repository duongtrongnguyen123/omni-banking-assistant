"""Cross-user holdout evaluation for the next-recipient suggester.

Reads the multi-user synthetic DB built by ``gen_synthetic_users.py`` and
runs TWO evaluations:

  1. **In-distribution** — for each user, sort tx by ``created_at``, train
     on the first 80 %, score Hit@1/3/5 on the last 20 %. Reports per-user
     numbers and a micro-average.
  2. **Cross-user** — train on user A, then predict for user B's test slice.
     Because A and B have separate contact-id namespaces (the generator
     prefixes contact ids with the user id), A's trained labels do NOT
     include B's contacts at all — so the cross-user Hit@K MUST be 0 by
     construction, which is a useful sanity ceiling. To get a more
     interesting cross-user number we **map** B's true label to A's
     archetype-matched contact (same ``__<arch>`` suffix) before scoring.
     A genuinely user-specific model still drops because A's day-of-month
     /day-of-week priors don't transfer to B.

The cross-user number is the honest "patterns are user-specific, not
memorised globally" proof.

Usage
-----
    OMNI_DB_PATH=backend/app/data/omni_synth_v2.db \\
        .venv/bin/python scripts/eval_suggester_holdout.py

Environment overrides:
    OMNI_DB_PATH       — point at omni_synth_v2.db (default)
    EVAL_CROSS_PAIRS   — number of (A,B) pairs to evaluate (default 30)
    EVAL_WRITE_JSON    — path to dump full per-user results as JSON
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")
os.environ.setdefault(
    "OMNI_DB_PATH",
    str(ROOT / "app" / "data" / "omni_synth_v2.db"),
)

from app.db.connection import get_connection  # noqa: E402
from app.ml import suggester  # noqa: E402


# ---------------------------------------------------------------------------
# Surrogate row type — same tactic as eval_suggester.py to skip pydantic.
# ---------------------------------------------------------------------------

class _Tx:
    __slots__ = ("contact_id", "created_at", "category")

    def __init__(self, contact_id: str, created_at: datetime, category: str) -> None:
        self.contact_id = contact_id
        self.created_at = created_at
        self.category = category


def _list_users(conn) -> list[str]:
    return [r["id"] for r in conn.execute(
        "SELECT id FROM users WHERE id LIKE 'u_synth_%' ORDER BY id").fetchall()]


def _load_txs(conn, user_id: str) -> list[_Tx]:
    rows = conn.execute(
        """SELECT contact_id, created_at, category
           FROM transactions WHERE owner_id = ?
           ORDER BY created_at""",
        (user_id,),
    ).fetchall()
    return [_Tx(r["contact_id"], datetime.fromisoformat(r["created_at"]),
                r["category"] or "other")
            for r in rows]


# ---------------------------------------------------------------------------
# Training / scoring — borrowed verbatim from eval_suggester.py shape.
# ---------------------------------------------------------------------------

def _train(train_txs: list[_Tx]) -> Optional[dict]:
    try:
        from sklearn.ensemble import RandomForestClassifier  # type: ignore
    except ImportError:
        raise RuntimeError("scikit-learn missing")

    if len(train_txs) < 3:
        return None

    X = [suggester._feature_vec(t.created_at) for t in train_txs]
    y = [t.contact_id for t in train_txs]
    labels = sorted(set(y))
    freq = Counter(y)
    total = len(y)
    prior = {c: freq[c] / total for c in labels}
    contact_stats = suggester._per_contact_stats(train_txs)

    if len(labels) < 2:
        return {"model": None, "labels": labels, "prior": prior,
                "n": total, "contact_stats": contact_stats}

    rf_kwargs = dict(n_estimators=50, max_depth=5, min_samples_leaf=1,
                     class_weight="balanced", random_state=42)
    model = RandomForestClassifier(**rf_kwargs)
    model.fit(X, y)

    return {
        "model": model,
        "labels": list(model.classes_),
        "prior": prior,
        "n": total,
        "contact_stats": contact_stats,
    }


def _score(state: dict, test_txs: list[_Tx], weights: tuple[float, float, float],
           label_mapper=None) -> dict:
    """Score test_txs with the given weights.

    ``label_mapper`` is optional: a callable that maps a test row's true
    contact_id into the namespace the trained model recognises. Used for
    cross-user eval so we score against the archetype-matched id in A's
    namespace, not B's namespace (which A never saw).
    """
    tw, fw, rw = weights
    labels = state["labels"]
    prior = state["prior"]
    stats = state["contact_stats"]
    model = state["model"]

    proba_cache: dict[tuple, dict[str, float]] = {}
    rule_cache: dict[tuple, float] = {}

    def proba_for(when: datetime) -> dict[str, float]:
        key = (when.month, when.day, when.weekday())
        hit = proba_cache.get(key)
        if hit is not None:
            return hit
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

    hit_at = {1: 0, 3: 0, 5: 0}
    n = 0
    n_inscope = 0  # how many test rows had a true label that A could rank

    for tx in test_txs:
        when = tx.created_at
        true_cid = tx.contact_id
        if label_mapper is not None:
            mapped = label_mapper(true_cid)
            if mapped is None:
                # No archetype overlap — leave row out (would otherwise be
                # an unscoreable 0). We track n separately so the metric
                # is over the *scoreable* rows.
                continue
            true_cid = mapped

        # If A's model never saw this contact at all (cross-user with
        # mapping fail), skip. Otherwise it'd be a forced miss.
        if true_cid not in prior:
            n += 1
            continue
        n_inscope += 1

        if model is not None and tw > 0:
            tree_proba = proba_for(when)
        else:
            tree_proba = {}

        scored: list[tuple[float, str]] = []
        for cid in prior:
            p_tree = float(tree_proba.get(cid, 0.0)) if tw > 0 else 0.0
            p_freq = prior[cid] if fw > 0 else 0.0
            p_rule = rule_for(cid, when) if rw > 0 else 0.0
            mixed = tw * p_tree + fw * p_freq + rw * p_rule
            scored.append((mixed, cid))

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
        "n_inscope": n_inscope,
        **{f"hit@{k}": hit_at[k] / max(n, 1) for k in hit_at},
    }


# ---------------------------------------------------------------------------
# Cross-user mapping helper — archetype suffix after "__" matches.
# ---------------------------------------------------------------------------

def _arch_suffix(cid: str) -> Optional[str]:
    if "__" not in cid:
        return None
    return cid.split("__", 1)[1]


def _make_mapper(model_user: str, b_to_a_overlap_keys: set[str]):
    """Returns a function that maps B's contact id → A's contact id when
    they share the same archetype key. Otherwise returns None to skip
    the row."""
    def mapper(b_cid: str) -> Optional[str]:
        suf = _arch_suffix(b_cid)
        if suf is None or suf not in b_to_a_overlap_keys:
            return None
        return f"{model_user}__{suf}"
    return mapper


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    t_start = time.perf_counter()
    conn = get_connection()
    users = _list_users(conn)
    if not users:
        print("No synthetic users found. Run gen_synthetic_users.py first.")
        sys.exit(1)

    print(f"DB: {os.environ.get('OMNI_DB_PATH')}")
    print(f"Users: {len(users)}")
    print()

    # The weight schedule for which we report headline numbers. We use
    # the suggester's BankSim-winning weights (tree+freq, no rule) since
    # synthetic v2 is not Vietnamese-locale-coded.
    HEADLINE_WEIGHTS = (0.60, 0.40, 0.00)

    # ----- In-distribution per-user evaluation -----
    print("=== In-distribution holdout (train 80 %, test 20 %) ===")
    indist_rows: list[dict] = []
    states: dict[str, dict] = {}
    test_slices: dict[str, list[_Tx]] = {}

    for u in users:
        txs = _load_txs(conn, u)
        if len(txs) < 20:
            print(f"  {u}: skipped ({len(txs)} tx)")
            continue
        cut = int(len(txs) * 0.8)
        train, test = txs[:cut], txs[cut:]
        # Filter test to contacts seen ≥3 times in train so we measure
        # generalisation, not cold-start.
        train_counts = Counter(t.contact_id for t in train)
        keep = {c for c, n in train_counts.items() if n >= 3}
        test = [t for t in test if t.contact_id in keep]
        if not test:
            print(f"  {u}: skipped (no testable rows)")
            continue

        state = _train(train)
        if state is None:
            print(f"  {u}: skipped (train failed)")
            continue
        states[u] = state
        test_slices[u] = test

        res = _score(state, test, HEADLINE_WEIGHTS)
        row = {"user_id": u, "n_train": len(train), "n_test": len(test), **res}
        indist_rows.append(row)
        print(f"  {u:14s}  train={len(train):4d}  test={len(test):4d}  "
              f"hit@1={res['hit@1']:.3f}  hit@3={res['hit@3']:.3f}  "
              f"hit@5={res['hit@5']:.3f}")

    print()

    # ----- Cross-user evaluation -----
    # Two flavours:
    #   (a) "raw" — feed B's tx straight into A's model. Each user has
    #       its own contact-id namespace (prefixed by user id) so A's
    #       label set NEVER contains any of B's contact ids. The model
    #       is forced to output one of A's labels, so Hit@K must be 0.
    #       This proves "no global label leakage". A model that wasn't
    #       prefixed-namespaced would silently look stronger by sharing
    #       label space — we explicitly avoid that.
    #   (b) "archetype-mapped" — translate B's "mom" → A's "mom" by
    #       matching the ``__<arch>`` suffix. Now A's day-of-month /
    #       day-of-week priors are evaluated against B's REAL test rows.
    #       The score gap vs in-distribution is the *user-specific* lift
    #       the model captures beyond shared archetype identity.
    print("=== Cross-user holdout (no mapping — must be ~0, proves namespace isolation) ===")
    pair_limit = int(os.environ.get("EVAL_CROSS_PAIRS", "30"))

    pairs: list[tuple[str, str]] = []
    eligible = [u for u in users if u in states and u in test_slices]
    for a in eligible:
        for b in eligible:
            if a == b:
                continue
            pairs.append((a, b))
    if len(pairs) > pair_limit:
        step = max(1, len(pairs) // pair_limit)
        pairs = pairs[::step][:pair_limit]

    cross_raw_rows: list[dict] = []
    for a, b in pairs:
        a_state = states[a]
        b_test = test_slices[b]
        res = _score(a_state, b_test, HEADLINE_WEIGHTS)
        cross_raw_rows.append({"train_user": a, "test_user": b, **res})

    if cross_raw_rows:
        avg = lambda k: sum(r[k] for r in cross_raw_rows) / len(cross_raw_rows)
        print(f"  Evaluated {len(cross_raw_rows)} (A,B) pairs (raw, no mapping):")
        print(f"    hit@1={avg('hit@1'):.3f}  "
              f"hit@3={avg('hit@3'):.3f}  hit@5={avg('hit@5'):.3f}")

    print()
    print("=== Cross-user holdout (archetype-mapped — measures generalisation gap) ===")

    cross_rows: list[dict] = []
    for a, b in pairs:
        a_state = states[a]
        b_test = test_slices[b]
        a_keys = {_arch_suffix(c) for c in a_state["prior"]}
        a_keys.discard(None)
        b_keys = {_arch_suffix(t.contact_id) for t in b_test}
        b_keys.discard(None)
        overlap = a_keys & b_keys
        mapper = _make_mapper(a, overlap)
        res = _score(a_state, b_test, HEADLINE_WEIGHTS, label_mapper=mapper)
        cross_rows.append({"train_user": a, "test_user": b,
                           "overlap_archetypes": len(overlap), **res})

    print(f"  Evaluated {len(cross_rows)} (A,B) pairs:")
    if cross_rows:
        avg = lambda k: sum(r[k] for r in cross_rows) / len(cross_rows)
        avg_overlap = sum(r["overlap_archetypes"] for r in cross_rows) / len(cross_rows)
        print(f"    avg archetype overlap: {avg_overlap:.1f}")
        print(f"    hit@1={avg('hit@1'):.3f}  "
              f"hit@3={avg('hit@3'):.3f}  hit@5={avg('hit@5'):.3f}")

    print()

    # ----- Aggregates -----
    print("=== Aggregate ===")
    agg_indist = None
    if indist_rows:
        total_test = sum(r["n_test"] for r in indist_rows)
        agg_indist = {
            "hit@1": sum(r["hit@1"] * r["n_test"] for r in indist_rows) / total_test,
            "hit@3": sum(r["hit@3"] * r["n_test"] for r in indist_rows) / total_test,
            "hit@5": sum(r["hit@5"] * r["n_test"] for r in indist_rows) / total_test,
            "n_users": len(indist_rows),
            "n_test": total_test,
        }
        print(f"  In-distribution (micro-avg, n_users={agg_indist['n_users']}, "
              f"n_test={agg_indist['n_test']}): "
              f"hit@1={agg_indist['hit@1']:.3f}  "
              f"hit@3={agg_indist['hit@3']:.3f}  "
              f"hit@5={agg_indist['hit@5']:.3f}")
    agg_cross_raw = None
    if cross_raw_rows:
        total_test = sum(r["n_test"] for r in cross_raw_rows)
        agg_cross_raw = {
            "hit@1": sum(r["hit@1"] * r["n_test"] for r in cross_raw_rows) / max(total_test, 1),
            "hit@3": sum(r["hit@3"] * r["n_test"] for r in cross_raw_rows) / max(total_test, 1),
            "hit@5": sum(r["hit@5"] * r["n_test"] for r in cross_raw_rows) / max(total_test, 1),
            "n_pairs": len(cross_raw_rows),
            "n_test": total_test,
        }
        print(f"  Cross-user RAW  (micro-avg, n_pairs={agg_cross_raw['n_pairs']}, "
              f"n_test={agg_cross_raw['n_test']}): "
              f"hit@1={agg_cross_raw['hit@1']:.3f}  "
              f"hit@3={agg_cross_raw['hit@3']:.3f}  "
              f"hit@5={agg_cross_raw['hit@5']:.3f}")
    agg_cross = None
    if cross_rows:
        total_test = sum(r["n_test"] for r in cross_rows)
        agg_cross = {
            "hit@1": sum(r["hit@1"] * r["n_test"] for r in cross_rows) / max(total_test, 1),
            "hit@3": sum(r["hit@3"] * r["n_test"] for r in cross_rows) / max(total_test, 1),
            "hit@5": sum(r["hit@5"] * r["n_test"] for r in cross_rows) / max(total_test, 1),
            "n_pairs": len(cross_rows),
            "n_test": total_test,
        }
        print(f"  Cross-user MAP  (micro-avg, n_pairs={agg_cross['n_pairs']}, "
              f"n_test={agg_cross['n_test']}): "
              f"hit@1={agg_cross['hit@1']:.3f}  "
              f"hit@3={agg_cross['hit@3']:.3f}  "
              f"hit@5={agg_cross['hit@5']:.3f}")

    print()
    print(f"Total wall time: {time.perf_counter() - t_start:.1f}s")

    out_path = os.environ.get("EVAL_WRITE_JSON")
    if out_path:
        payload = {
            "weights": list(HEADLINE_WEIGHTS),
            "in_distribution": indist_rows,
            "cross_user_raw": cross_raw_rows,
            "cross_user_mapped": cross_rows,
            "aggregate_in_distribution": agg_indist,
            "aggregate_cross_user_raw": agg_cross_raw,
            "aggregate_cross_user_mapped": agg_cross,
        }
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(payload, indent=2))
        print(f"Wrote JSON report to {out_path}")


if __name__ == "__main__":
    main()

"""Hold-out evaluation for the KNN category predictor.

Run from the repo root:

    .venv/bin/python -m scripts.eval_category

What it does
------------
1. Pulls every transaction row that has both an embedding and a
   non-``other`` category (the labeled corpus the predictor already
   queries at inference time).
2. Splits 80/20 with a fixed random seed so re-runs are comparable.
3. For each row in the held-out 20%, runs the same KNN majority-vote
   the orchestrator runs at chat-reply time, against the 80% as the
   reference set.
4. Prints a per-class precision table at five confidence thresholds.

Why
---
The predictor ships with a single global ``DEFAULT_MIN_CONFIDENCE``
(0.55, see app/ml/category.py). That number was picked by inspection.
This script gives an empirical floor: the threshold at which precision
for each individual class crosses a target (say 90%) — the level at
which a future migration could confidently re-write the ``other``
bucket without human review.

Output is text only; no DB writes.
"""

from __future__ import annotations

import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Make the script runnable as `.venv/bin/python -m scripts.eval_category`
# or directly via `.venv/bin/python scripts/eval_category.py`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import bootstrap  # noqa: E402
from app.db.connection import get_connection  # noqa: E402
from app.nlp.embedder import fill_missing_embeddings  # noqa: E402
from app.nlp.embeddings import cosine, unpack  # noqa: E402


SEED = 42
K = 5
THRESHOLDS = (0.35, 0.45, 0.55, 0.65, 0.75)


def _load_labeled() -> list[tuple[str, str, list[float]]]:
    """Pull ``(tx_id, category, vector)`` for every labeled row."""
    rows = get_connection().execute(
        "SELECT id, category, embedding FROM transactions "
        "WHERE embedding IS NOT NULL AND category NOT IN ('other', '')"
    ).fetchall()
    return [
        (r["id"], r["category"], unpack(r["embedding"]))
        for r in rows
        if r["embedding"]
    ]


def _knn_predict(
    q_vec: list[float],
    corpus: list[tuple[str, str, list[float]]],
    k: int = K,
) -> tuple[str, float]:
    """Return ``(predicted_category, confidence)`` using the same
    share×peer_sim formula as ``app.ml.category.predict_category``.
    """
    scored = sorted(
        ((cosine(q_vec, vec), cat) for _, cat, vec in corpus),
        key=lambda x: x[0],
        reverse=True,
    )[:k]
    votes: Counter[str] = Counter(cat for _, cat in scored)
    winner, count = votes.most_common(1)[0]
    share = count / k
    peer_sim = sum(s for s, c in scored if c == winner) / max(count, 1)
    return winner, round(share * peer_sim, 3)


def main() -> None:
    bootstrap.bootstrap_if_empty()
    fill_missing_embeddings()

    labeled = _load_labeled()
    if not labeled:
        print("No labeled rows with embeddings — run the embedder first.")
        return

    random.seed(SEED)
    indices = list(range(len(labeled)))
    random.shuffle(indices)
    split = int(0.8 * len(labeled))
    train_idx, test_idx = indices[:split], indices[split:]
    train = [labeled[i] for i in train_idx]
    test = [labeled[i] for i in test_idx]

    by_class_total: Counter[str] = Counter()
    for _, cat, _ in test:
        by_class_total[cat] += 1

    print(f"Corpus: {len(labeled)} labeled rows "
          f"({len(train)} train / {len(test)} test, seed={SEED}, k={K})")
    print(f"Class distribution in test set:")
    for cat, n in by_class_total.most_common():
        print(f"  {cat:10}  {n:4}")
    print()

    # For every threshold, compute per-class
    #   precision  = TP / (TP + FP among predictions ≥ threshold)
    #   coverage   = (TP + FP) / class_total
    #
    # The right threshold for a class is where precision climbs into
    # the "defensible re-label" band (≥90%) without coverage collapsing.

    # rows: [threshold][class] -> {tp, fp, fn}
    stats: dict[float, dict[str, dict[str, int]]] = {
        thr: defaultdict(lambda: {"tp": 0, "fp": 0, "predicted": 0})
        for thr in THRESHOLDS
    }

    for _, true_cat, vec in test:
        pred_cat, conf = _knn_predict(vec, train)
        for thr in THRESHOLDS:
            if conf < thr:
                continue
            cell = stats[thr][pred_cat]
            cell["predicted"] += 1
            if pred_cat == true_cat:
                cell["tp"] += 1
            else:
                cell["fp"] += 1

    print(f"{'Class':10} | " + " | ".join(
        f"thr={thr:.2f} (prec/recall)" for thr in THRESHOLDS
    ))
    classes = sorted(by_class_total.keys())
    for cls in classes:
        cells = [stats[thr][cls] for thr in THRESHOLDS]
        bits = []
        for cell, thr in zip(cells, THRESHOLDS):
            predicted = cell["predicted"]
            tp = cell["tp"]
            precision = (tp / predicted) if predicted else 0.0
            recall = tp / by_class_total[cls] if by_class_total[cls] else 0.0
            bits.append(f"{precision:>4.0%}/{recall:<4.0%}")
        print(f"{cls:10} | " + " | ".join(bits))

    # Macro precision per threshold — simple unweighted mean across classes
    # with at least one prediction. Useful single-number summary.
    print()
    print("Macro precision (mean across classes with ≥1 prediction):")
    for thr in THRESHOLDS:
        precs = []
        for cls in classes:
            cell = stats[thr][cls]
            if cell["predicted"]:
                precs.append(cell["tp"] / cell["predicted"])
        macro = sum(precs) / len(precs) if precs else 0.0
        coverage = sum(stats[thr][c]["predicted"] for c in classes) / len(test)
        print(f"  thr={thr:.2f}  macro_precision={macro:.2%}  coverage={coverage:.1%}")


if __name__ == "__main__":
    main()

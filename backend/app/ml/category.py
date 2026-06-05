"""KNN-cosine category predictor over transaction embeddings.

The simulation dataset lands 62% of outgoing transactions in ``category =
"other"`` because the rule-based extractor only matches a handful of
explicit terms ("trả nợ", "ăn", "mua sắm"). The remainder — "tien mang",
"tra nghiep", "donate", and many one-token notes — are semantically
informative but lexically opaque.

We already store a 384-dim MiniLM embedding per transaction (see
``nlp/embedder._transaction_text``). This module reuses that index to
suggest a category for any ``other``-bucket description by majority-voting
over its k-nearest *labeled* neighbors in cosine space.

Output is suggestion-only — callers decide whether to write the predicted
category back to the row. The orchestrator can surface it as a "Looks
like ăn uống" chip on history queries; a maintenance script can use
``predict_for_user`` in dry-run mode to audit how many rows would flip.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

from ..db.connection import get_connection
from ..nlp.embeddings import cosine, embed, unpack

log = logging.getLogger("omni.ml.category")


DEFAULT_K = 5
# Empirical floor: on the demo dataset (1175 'other' rows, MiniLM
# multilingual embeddings) very few cross-class votes clear 0.55.
# That's by design — better to leave a row as 'other' than to
# confidently mis-label it bills/charity/family.
DEFAULT_MIN_CONFIDENCE = 0.55


def _labeled_corpus(user_id: Optional[str] = None) -> list[tuple[str, list[float]]]:
    """Pull ``(category, vector)`` pairs from the transactions table for
    every row that has both an embedding and a non-``other`` category.

    The corpus is shared across all users in the demo because Omni only
    ships one — passing ``user_id`` keeps the call site honest if the
    schema gains real multi-tenancy.
    """
    conn = get_connection()
    if user_id is not None:
        rows = conn.execute(
            "SELECT category, embedding FROM transactions "
            "WHERE owner_id = ? AND embedding IS NOT NULL AND category NOT IN ('other', '')",
            (user_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT category, embedding FROM transactions "
            "WHERE embedding IS NOT NULL AND category NOT IN ('other', '')"
        ).fetchall()
    return [(r["category"], unpack(r["embedding"])) for r in rows if r["embedding"]]


def predict_category(
    description: str,
    *,
    k: int = DEFAULT_K,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    user_id: Optional[str] = None,
) -> Optional[dict]:
    """Suggest a category for ``description`` via KNN-cosine.

    Returns ``None`` when:
      - the description can't be embedded (empty / model unavailable)
      - the labeled corpus is empty
      - the winning class's confidence is below ``min_confidence``

    Confidence model — designed to penalise both *uncertain* votes and
    *low-similarity* clusters:

        share      = (votes for winning class) / k
        peer_sim   = mean cosine of the winning-class neighbours
        confidence = round(share × peer_sim, 3)

    ``peer_sim`` collapses confidence when even the "right" neighbours
    are far from the query, e.g. a brand-new phrase that has no good
    match in the labeled set at all.
    """
    text = (description or "").strip()
    if not text:
        return None

    query_vec = embed(text, task_type="RETRIEVAL_QUERY")
    if query_vec is None:
        return None

    corpus = _labeled_corpus(user_id)
    if not corpus:
        return None

    scored = [(cosine(query_vec, vec), cat) for cat, vec in corpus]
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:k]

    votes: Counter[str] = Counter(cat for _, cat in top)
    winner, votes_for_winner = votes.most_common(1)[0]
    share = votes_for_winner / max(len(top), 1)

    peer_sims = [score for score, cat in top if cat == winner]
    peer_sim = sum(peer_sims) / max(len(peer_sims), 1)

    confidence = round(share * peer_sim, 3)
    if confidence < min_confidence:
        return None

    return {
        "category": winner,
        "confidence": confidence,
        "share": round(share, 2),
        "peer_similarity": round(peer_sim, 3),
        "neighbors": [
            {"category": cat, "cosine": round(score, 3)}
            for score, cat in top
        ],
    }


def predict_for_user(
    user_id: str,
    *,
    limit: int = 20,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[dict]:
    """Dry-run: for each ``other``-bucketed transaction with an embedding,
    return what ``predict_category`` would assign.

    Audit aid only — does not mutate the DB. A future migration could
    write these back after manual review. Empty list when there's no
    embedding index or every prediction is below ``min_confidence``.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, description, embedding FROM transactions "
        "WHERE owner_id = ? AND embedding IS NOT NULL AND category IN ('other', '') "
        "LIMIT ?",
        (user_id, limit),
    ).fetchall()

    corpus = _labeled_corpus(user_id)
    if not corpus:
        return []

    out: list[dict] = []
    for r in rows:
        q_vec = unpack(r["embedding"]) if r["embedding"] else None
        if q_vec is None:
            continue
        scored = sorted(
            ((cosine(q_vec, vec), cat) for cat, vec in corpus),
            key=lambda x: x[0],
            reverse=True,
        )[:DEFAULT_K]
        if not scored:
            continue
        votes = Counter(cat for _, cat in scored)
        winner, count = votes.most_common(1)[0]
        share = count / len(scored)
        peer_sim = sum(s for s, c in scored if c == winner) / max(count, 1)
        conf = round(share * peer_sim, 3)
        if conf < min_confidence:
            continue
        out.append(
            {
                "tx_id": r["id"],
                "description": r["description"],
                "predicted_category": winner,
                "confidence": conf,
            }
        )
    # Sort by confidence so callers can preview the cleanest re-labels first.
    out.sort(key=lambda d: -d["confidence"])
    return out


__all__ = ["predict_category", "predict_for_user"]

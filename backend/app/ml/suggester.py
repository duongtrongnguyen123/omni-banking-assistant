"""Tree-based next-recipient suggester.

Given the current moment (day of month, day of week, month, weekend flag,
payday-window flag), rank the user's contacts by probability of being the
next transfer recipient. Powers the *"Có thể bạn muốn chuyển cho…"* widget
in the chat sidebar.

Model
-----
A small ``RandomForestClassifier`` (sklearn, no GPU):
    * 50 trees, ``max_depth=5`` to keep the model from memorising the
      sparse seed data (35 transactions across 30 contacts).
    * ``class_weight="balanced"`` so rare recipients aren't shadowed by
      the monthly-salary stalwarts.

Inference combines tree ``predict_proba`` with a frequency baseline at
0.6/0.4 — Bayesian-ish smoothing. Without it, the tree would gladly
assign 1.0 to a single contact whose only transaction happens to fall on
today's day-of-month.

The trained model is held per-process and re-trained on demand:
    * once on FastAPI startup (warm path)
    * again after every executed transfer (the orchestrator hits the
      retrain hook so the suggestion list reflects the user's latest
      behaviour without waiting for a restart)
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from datetime import datetime
from typing import Any, Optional

from ..store import get_store

log = logging.getLogger("omni.ml.suggester")

# Per-user cached state. Keyed by user_id because the model is small enough
# to keep one per user instead of multiplexing.
_LOCK = threading.Lock()
_STATE: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

_PAYDAY_DAYS = {1, 2, 5, 10, 15, 25}


def _feature_vec(dt: datetime) -> list[float]:
    return [
        float(dt.day),
        float(dt.weekday()),
        float(dt.month),
        1.0 if dt.weekday() >= 5 else 0.0,
        1.0 if dt.day in _PAYDAY_DAYS else 0.0,
    ]


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


def train_for(user_id: str) -> Optional[dict]:
    """(Re)train the suggester for ``user_id``. Returns a stats dict, or
    ``None`` if there isn't enough data."""
    try:
        from sklearn.ensemble import RandomForestClassifier  # type: ignore
    except ImportError:
        log.warning("scikit-learn missing — suggester disabled")
        return None

    txs = get_store().transactions_of(user_id)
    if len(txs) < 3:
        return None

    X = [_feature_vec(t.created_at) for t in txs]
    y = [t.contact_id for t in txs]

    labels = sorted(set(y))
    freq = Counter(y)
    total = len(y)
    prior = {c: freq[c] / total for c in labels}

    if len(labels) < 2:
        # Single-class corpus — predict_proba would be degenerate. Frequency
        # baseline alone is enough.
        with _LOCK:
            _STATE[user_id] = {
                "model": None, "labels": labels, "prior": prior, "n": total,
            }
        return {"trained_on": total, "labels": len(labels), "kind": "freq-only"}

    model = RandomForestClassifier(
        n_estimators=50,
        max_depth=5,
        min_samples_leaf=1,
        random_state=42,
        class_weight="balanced",
    )
    model.fit(X, y)

    with _LOCK:
        _STATE[user_id] = {
            "model": model,
            "labels": list(model.classes_),
            "prior": prior,
            "n": total,
        }
    return {"trained_on": total, "labels": len(labels), "kind": "random_forest"}


def reset_all() -> None:
    """Clear cached state — useful when seed data is reloaded."""
    with _LOCK:
        _STATE.clear()


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


def suggest(
    user_id: str,
    when: Optional[datetime] = None,
    k: int = 5,
    tree_weight: float = 0.6,
) -> list[dict]:
    """Top-K suggested contacts for ``when`` (defaults to "now").

    Each item: ``{contact, score, reason}``. The score is the mixed
    tree+prior probability; the reason is a short Vietnamese phrase
    derived from the user's past behaviour with that contact.
    """
    when = when or datetime.now().astimezone()

    state = _STATE.get(user_id)
    if state is None:
        train_for(user_id)
        state = _STATE.get(user_id)
    if state is None:
        return []

    store = get_store()
    contacts = {c.id: c for c in store.contacts_of(user_id)}

    if state["model"] is not None:
        proba = state["model"].predict_proba([_feature_vec(when)])[0]
        scored: list[tuple[str, float]] = []
        for label, p in zip(state["labels"], proba):
            mixed = tree_weight * float(p) + (1 - tree_weight) * state["prior"].get(label, 0.0)
            scored.append((label, mixed))
    else:
        scored = list(state["prior"].items())

    scored.sort(key=lambda x: x[1], reverse=True)

    # Cache history per-contact for reason generation so we don't re-scan.
    txs_by_contact: dict[str, list] = {}
    for t in store.transactions_of(user_id):
        txs_by_contact.setdefault(t.contact_id, []).append(t)

    out: list[dict] = []
    for cid, score in scored[:k]:
        c = contacts.get(cid)
        if c is None:
            continue
        out.append({
            "contact": c.model_dump(),
            "score": round(score, 4),
            "reason": _reason(txs_by_contact.get(cid, []), when),
        })
    return out


# ---------------------------------------------------------------------------
# Reason generation
# ---------------------------------------------------------------------------


def _reason(txs: list, when: datetime) -> str:
    """Short Vietnamese phrase explaining why this contact ranks highly
    for ``when``. Looks for the strongest temporal pattern in the history."""
    if not txs:
        return "Đã có giao dịch trước đây"

    days = [t.created_at.day for t in txs]
    weekdays = [t.created_at.weekday() for t in txs]
    n = len(txs)

    # Day-of-month proximity
    avg_day = sum(days) / n
    if abs(avg_day - when.day) <= 2 and n >= 2:
        return f"Thường chuyển vào ngày ~{int(round(avg_day))} hàng tháng"

    # Payday window
    payday_hits = sum(1 for d in days if d in _PAYDAY_DAYS)
    if payday_hits >= 2 and when.day in _PAYDAY_DAYS:
        return f"{payday_hits}/{n} lần trước rơi vào đầu/giữa tháng"

    # Weekend pattern
    weekend_hits = sum(1 for w in weekdays if w >= 5)
    if when.weekday() >= 5 and weekend_hits >= 2:
        return f"{weekend_hits}/{n} lần trước vào cuối tuần"

    # Frequency fallback
    if n >= 3:
        return f"Đã chuyển {n} lần trước đây"
    return f"Đã chuyển {n} lần"

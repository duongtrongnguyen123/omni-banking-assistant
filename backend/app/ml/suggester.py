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

# Vietnamese e-commerce sale dates (Shopee/Lazada/TikTok), gross
# generalisation but powerful prior for shipper / service contacts.
_SALE_DATES = {(1, 1), (2, 2), (6, 6), (7, 7), (8, 8), (9, 9),
               (10, 10), (11, 11), (12, 12)}

_SERVICE_CATEGORIES = {"daily", "friends"}      # likely shipper / cafe / food
_FAMILY_CATEGORIES = {"family"}                 # mẹ / bố / cô ...


def _feature_vec(dt: datetime) -> list[float]:
    return [
        float(dt.day),
        float(dt.weekday()),
        float(dt.month),
        1.0 if dt.weekday() >= 5 else 0.0,
        1.0 if dt.day in _PAYDAY_DAYS else 0.0,
    ]


# ---------------------------------------------------------------------------
# Rule-based scorer — hand-crafted signal that doesn't need to be learned.
# Crucial on small data where the tree can barely generalise.
# ---------------------------------------------------------------------------


def _rule_score(contact, txs: list, when: datetime) -> float:
    """Heuristic bonus in [0, 1]. Designed so that:

      * Paying mom on the 5th every month for 6 months → mom scores high
        on the 4th–6th regardless of the tree's verdict.
      * Saturday-only yoga payments → yoga boosts on Saturdays.
      * GrabFood orders last 3 days → shipper stays warm even if rare.
      * On 6/6 / 11/11 / etc., service contacts get a sale-day boost.
    """
    if not txs:
        return 0.0

    bonus = 0.0

    # 1) Day-of-month proximity. Use the *median* past day to be robust to
    #    one-off transactions (e.g. an emergency tx for mom on day 22).
    days = sorted(t.created_at.day for t in txs)
    med_day = days[len(days) // 2]
    delta = abs(med_day - when.day)
    if delta <= 2:
        bonus += 0.4 * (1 - delta / 3)  # max 0.4 at exact match
    elif delta <= 5:
        bonus += 0.15 * (1 - (delta - 2) / 4)

    # 2) Day-of-week match — strong signal for weekend services.
    dows = [t.created_at.weekday() for t in txs]
    dow_match = dows.count(when.weekday()) / len(dows)
    bonus += 0.25 * dow_match

    # 3) Recency decay — touched in last week stays warm.
    most_recent = max(t.created_at for t in txs)
    days_since = (when.date() - most_recent.date()).days
    if days_since <= 7:
        bonus += 0.2 * (1 - days_since / 7)
    elif days_since <= 30:
        bonus += 0.08 * (1 - (days_since - 7) / 23)

    # 4) Calendar prior: payday → family, sale date → service.
    last_cat = txs[-1].category if txs else ""

    if (when.month, when.day) in _SALE_DATES and last_cat in _SERVICE_CATEGORIES:
        bonus += 0.15
    if when.day in _PAYDAY_DAYS and last_cat in _FAMILY_CATEGORIES:
        bonus += 0.1

    return min(bonus, 1.0)


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


def _auto_weights(n_tx: int) -> tuple[float, float, float]:
    """Pick tree/freq/rule weights based on how much data the user has.

    Justified empirically by ``scripts/eval_suggester.py``:
      * ≤50 tx  — tree underfits; freq baseline wins (Hit@3 0.50 vs 0.00).
      * 50–200  — balanced; rules add insurance.
      * ≥200    — tree dominates (Hit@3 jumps 0.50 → 0.73); reduce rules.
    """
    if n_tx < 50:
        return (0.10, 0.65, 0.25)
    if n_tx < 200:
        return (0.35, 0.30, 0.35)
    return (0.55, 0.30, 0.15)


def suggest(
    user_id: str,
    when: Optional[datetime] = None,
    k: int = 5,
    tree_weight: Optional[float] = None,
    freq_weight: Optional[float] = None,
    rule_weight: Optional[float] = None,
    include_all: bool = False,
) -> list[dict]:
    """Top-K suggested contacts for ``when`` (defaults to "now").

    Each item: ``{contact, score, reason}``. The score mixes the tree's
    ``predict_proba`` with a frequency baseline; the reason is a short
    Vietnamese phrase derived from the user's past behaviour with that
    contact.

    When ``include_all`` is True, every contact the user has (including
    those with no transaction history) is returned ranked. Unseen
    contacts get score 0 and are sorted to the bottom alphabetically —
    so the "Danh bạ" picker can show one ranked list of everyone.
    """
    when = when or datetime.now().astimezone()

    state = _STATE.get(user_id)
    if state is None:
        train_for(user_id)
        state = _STATE.get(user_id)

    # Auto-pick weights from data size when caller didn't override.
    n_tx = state["n"] if state else 0
    auto = _auto_weights(n_tx)
    tw = tree_weight if tree_weight is not None else auto[0]
    fw = freq_weight if freq_weight is not None else auto[1]
    rw = rule_weight if rule_weight is not None else auto[2]

    store = get_store()
    contacts = {c.id: c for c in store.contacts_of(user_id)}

    # Pre-bucket txs per contact for the rule scorer.
    txs_by_contact: dict[str, list] = {}
    for t in store.transactions_of(user_id):
        txs_by_contact.setdefault(t.contact_id, []).append(t)

    scored: list[tuple[str, float]] = []
    if state is not None:
        tree_proba: dict[str, float] = {}
        if state["model"] is not None:
            proba = state["model"].predict_proba([_feature_vec(when)])[0]
            tree_proba = dict(zip(state["labels"], proba))

        for cid in state["prior"]:
            p_tree = float(tree_proba.get(cid, 0.0))
            p_freq = state["prior"][cid]
            p_rule = _rule_score(contacts.get(cid), txs_by_contact.get(cid, []), when)
            mixed = tw * p_tree + fw * p_freq + rw * p_rule
            scored.append((cid, mixed))

    # Fold in contacts the model doesn't know about yet (no tx history).
    if include_all:
        seen = {cid for cid, _ in scored}
        unseen = sorted(
            (c for cid, c in contacts.items() if cid not in seen),
            key=lambda c: c.display_name,
        )
        scored.extend((c.id, 0.0) for c in unseen)

    def _sort_key(item: tuple[str, float]) -> tuple[float, str]:
        cid, score = item
        c = contacts.get(cid)
        return (-score, c.display_name if c else "")

    scored.sort(key=_sort_key)

    # Cache history per-contact for reason generation so we don't re-scan.
    txs_by_contact: dict[str, list] = {}
    for t in store.transactions_of(user_id):
        txs_by_contact.setdefault(t.contact_id, []).append(t)

    out: list[dict] = []
    for cid, score in scored[: k if not include_all else len(scored)]:
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

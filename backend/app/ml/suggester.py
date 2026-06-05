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
    """Hand-engineered hierarchical features.

    Pre-computing interactions (X/X sale-day, decade buckets, end-of-month
    flag) lets a shallow ``max_depth=5`` Random Forest pick up the
    Vietnamese-specific behavioural patterns without having to split 2-3
    times to discover them. Higher-importance features come first so the
    tree's greedy split tends to prefer them.
    """
    day = dt.day
    dow = dt.weekday()
    month = dt.month
    return [
        # ---- Strong, explicit signals (sale-day + month edges) ----
        1.0 if day == month else 0.0,                       # X/X sale (1/1, 6/6, 7/7…)
        1.0 if (month, day) in _SALE_DATES else 0.0,        # known e-com sale dates
        1.0 if day <= 5 else 0.0,                           # đầu tháng (lương / hiếu cha mẹ)
        1.0 if day >= 26 else 0.0,                          # cuối tháng (quà sếp / tổng kết)
        1.0 if day in _PAYDAY_DAYS else 0.0,                # payday window
        # ---- Coarse decade buckets (1-10 / 11-20 / 21-end) ----
        1.0 if day <= 10 else 0.0,
        1.0 if 11 <= day <= 20 else 0.0,
        1.0 if day >= 21 else 0.0,
        # ---- Day-of-week signals (weekend + each individual day) ----
        1.0 if dow >= 5 else 0.0,
        1.0 if dow == 0 else 0.0,                           # Monday (PT, lunch)
        1.0 if dow == 4 else 0.0,                           # Friday (bestie)
        1.0 if dow == 5 else 0.0,                           # Saturday (yoga, family)
        1.0 if dow == 6 else 0.0,                           # Sunday (tạp hoá)
        # ---- Raw scalars at the tail for catch-all splits ----
        float(day),
        float(dow),
        float(month),
    ]


# ---------------------------------------------------------------------------
# Rule-based scorer — hand-crafted signal that doesn't need to be learned.
# Crucial on small data where the tree can barely generalise.
# ---------------------------------------------------------------------------


def _rule_score(stats: Optional[dict], when: datetime) -> float:
    """Heuristic bonus in [0, 1], computed from precomputed per-contact
    stats (see ``_per_contact_stats``). The previous implementation took
    the raw transaction list and re-derived these on every call — fine
    for 30 contacts × 7 tx, but quadratic on contest-scale data where
    each contact may have 500+ rows.
    """
    if not stats:
        return 0.0

    bonus = 0.0

    # 1) Day-of-month proximity (median past day vs query day).
    delta = abs(stats["median_day"] - when.day)
    if delta <= 2:
        bonus += 0.4 * (1 - delta / 3)
    elif delta <= 5:
        bonus += 0.15 * (1 - (delta - 2) / 4)

    # 2) Day-of-week match.
    dow_total = stats["n"]
    dow_match = stats["dow_counts"].get(when.weekday(), 0) / dow_total if dow_total else 0
    bonus += 0.25 * dow_match

    # 3) Recency decay.
    days_since = (when.date() - stats["most_recent"].date()).days
    if days_since <= 7:
        bonus += 0.2 * (1 - max(days_since, 0) / 7)
    elif days_since <= 30:
        bonus += 0.08 * (1 - (days_since - 7) / 23)

    # 4) Calendar priors.
    last_cat = stats["last_category"]
    if when.day == when.month and last_cat in _SERVICE_CATEGORIES:
        bonus += 0.20
    elif (when.month, when.day) in _SALE_DATES and last_cat in _SERVICE_CATEGORIES:
        bonus += 0.15

    if when.day <= 5 and last_cat in _FAMILY_CATEGORIES:
        bonus += 0.15
    elif when.day in _PAYDAY_DAYS and last_cat in _FAMILY_CATEGORIES:
        bonus += 0.08

    if when.day >= 26 and last_cat == "work":
        bonus += 0.10

    return min(bonus, 1.0)


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


def _per_contact_stats(txs: list) -> dict:
    """Precompute the per-contact stats used by ``_rule_score`` so each
    call doesn't re-scan the whole history. Speed-up is essential when
    eval runs hundreds of inferences across hundreds of contacts."""
    stats: dict[str, dict] = {}
    by_c: dict[str, list] = {}
    for t in txs:
        by_c.setdefault(t.contact_id, []).append(t)
    for cid, items in by_c.items():
        days = sorted(t.created_at.day for t in items)
        dow_counter: Counter = Counter(t.created_at.weekday() for t in items)
        stats[cid] = {
            "median_day": days[len(days) // 2],
            "dow_counts": dict(dow_counter),
            "n": len(items),
            "most_recent": max(t.created_at for t in items),
            "last_category": items[-1].category,
            "payday_hits": sum(1 for d in days if d in _PAYDAY_DAYS),
        }
    return stats


def train_for(user_id: str, txs: Optional[list] = None) -> Optional[dict]:
    """(Re)train the suggester for ``user_id``. Returns a stats dict, or
    ``None`` if there isn't enough data.

    ``txs`` lets callers (eval harness, batch backfill) supply an explicit
    transaction list so no DB write is needed to evaluate a training
    window — the harness can slice in memory and pass the slice directly.
    Production callers omit the arg and we read the user's full history
    from the store.
    """
    try:
        from sklearn.ensemble import RandomForestClassifier  # type: ignore
    except ImportError:
        log.warning("scikit-learn missing — suggester disabled")
        return None

    if txs is None:
        txs = get_store().transactions_of(user_id)
    if len(txs) < 3:
        return None

    X = [_feature_vec(t.created_at) for t in txs]
    y = [t.contact_id for t in txs]

    labels = sorted(set(y))
    freq = Counter(y)
    total = len(y)
    prior = {c: freq[c] / total for c in labels}
    contact_stats = _per_contact_stats(txs)

    if len(labels) < 2:
        with _LOCK:
            _STATE[user_id] = {
                "model": None, "labels": labels, "prior": prior, "n": total,
                "contact_stats": contact_stats,
            }
        return {"trained_on": total, "labels": len(labels), "kind": "freq-only"}

    # Auto-tune RF cost based on dataset shape. With 1000 classes
    # + class_weight=balanced + bootstrap, training on 100k+ samples can
    # take 10+ minutes — way too slow for online retrain after each
    # transfer. Drop n_estimators and parallelise across cores when the
    # dataset is large.
    if total >= 10_000:
        rf_kwargs = dict(n_estimators=20, max_depth=8, min_samples_leaf=5, n_jobs=-1)
    else:
        rf_kwargs = dict(n_estimators=50, max_depth=5, min_samples_leaf=1,
                         class_weight="balanced")
    model = RandomForestClassifier(random_state=42, **rf_kwargs)
    model.fit(X, y)

    with _LOCK:
        _STATE[user_id] = {
            "model": model,
            "labels": list(model.classes_),
            "prior": prior,
            "n": total,
            "contact_stats": contact_stats,
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

    Tuned empirically by ``scripts/eval_suggester.py`` with the
    hierarchical feature set (16 engineered features):
      * ≤50 tx     — tree underfits; freq baseline + rules win.
      * 50–200     — balanced; rules add insurance.
      * 200–500    — tree dominant; freq smoothing helps OOD stability.
      * ≥500       — pure tree-heavy; rules contribute marginally.
    """
    if n_tx < 50:
        return (0.10, 0.55, 0.35)
    if n_tx < 200:
        return (0.40, 0.30, 0.30)
    if n_tx < 500:
        return (0.65, 0.25, 0.10)
    return (0.75, 0.20, 0.05)


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

    scored: list[tuple[str, float]] = []
    if state is not None:
        tree_proba: dict[str, float] = {}
        if state["model"] is not None:
            proba = state["model"].predict_proba([_feature_vec(when)])[0]
            tree_proba = dict(zip(state["labels"], proba))

        contact_stats = state.get("contact_stats", {})
        for cid in state["prior"]:
            p_tree = float(tree_proba.get(cid, 0.0))
            p_freq = state["prior"][cid]
            p_rule = _rule_score(contact_stats.get(cid), when)
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

    # Reason-string generation needs the actual tx list; only build it
    # for the rows we return.  OPT-3 (bench): a single targeted query per
    # returned contact instead of scanning the user's full 520k-row
    # history just to filter to the top-K.  On the contest dataset the
    # previous code spent ~1.8s per ``suggest()`` call materialising
    # Pydantic transactions only to throw 99.9% of them away.
    out: list[dict] = []
    needed_ids = {cid for cid, _ in scored[: k if not include_all else len(scored)]}
    txs_for_reasons: dict[str, list] = {}
    for cid in needed_ids:
        txs_for_reasons[cid] = store.transactions_of(
            user_id, contact_id=cid, limit=30,
        )

    for cid, score in scored[: k if not include_all else len(scored)]:
        c = contacts.get(cid)
        if c is None:
            continue
        out.append({
            "contact": c.model_dump(),
            "score": round(score, 4),
            "reason": _reason(txs_for_reasons.get(cid, []), when),
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

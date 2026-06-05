"""Per-user fraud / anomaly detection via Isolation Forest.

Goes a step beyond the simple z-score check in `rules.py`:

* Trains one Isolation Forest per user on their last 6 months / 5000 tx
  of completed outgoing transactions.
* Feature set is intentionally lightweight so inference stays under ~5ms
  per call — pure sklearn, no PyTorch / TF.
* `score_draft` returns a normalised anomaly score in [0, 1]; the rule
  engine raises a `fraud_risk_high` flag when the score crosses
  `FRAUD_RISK_THRESHOLD` (default 0.7).
* `train_fraud_models` is called once at startup; per-user models are
  cached in process memory. Set `OMNI_FRAUD_DISABLE=1` to skip entirely.

Design notes
------------
Isolation Forest scores: sklearn's ``decision_function`` returns a value
where higher means more normal. We invert and squash to [0, 1] using a
calibrated logistic so the threshold is intuitive ("> 0.7 is risky").
The calibration uses the per-user score quantiles captured at fit time
so each user gets their own scale — what's anomalous for a salary
account is not the same as a small-allowance account.
"""

from __future__ import annotations

import heapq
import logging
import math
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import numpy as np


class _RunningMedian:
    """Online median over a stream of ints — O(log n) per update.

    Two heaps (max-heap of lower half, min-heap of upper half). Used at
    training time so the per-row user-median feature stays O(n log n)
    overall instead of O(n^2 log n).
    """

    __slots__ = ("_lo", "_hi")

    def __init__(self) -> None:
        self._lo: list[int] = []  # max-heap via negation
        self._hi: list[int] = []  # min-heap

    def add(self, x: int) -> None:
        if not self._lo or x <= -self._lo[0]:
            heapq.heappush(self._lo, -x)
        else:
            heapq.heappush(self._hi, x)
        # Rebalance to keep |lo| - |hi| in {0, 1}
        if len(self._lo) > len(self._hi) + 1:
            heapq.heappush(self._hi, -heapq.heappop(self._lo))
        elif len(self._hi) > len(self._lo):
            heapq.heappush(self._lo, -heapq.heappop(self._hi))

    def median(self) -> float:
        if not self._lo:
            return 0.0
        if len(self._lo) == len(self._hi):
            return (-self._lo[0] + self._hi[0]) / 2.0
        return float(-self._lo[0])

try:  # sklearn is heavy — keep the module importable even if missing.
    from sklearn.ensemble import IsolationForest

    _SKLEARN_OK = True
except ImportError:  # pragma: no cover
    IsolationForest = None  # type: ignore[assignment]
    _SKLEARN_OK = False

from ..models.schemas import Contact, Transaction

logger = logging.getLogger(__name__)

# Tuning knobs ---------------------------------------------------------------

FRAUD_RISK_THRESHOLD = 0.7
"""Score above which `fraud_risk_high` flag is raised."""

MIN_TX_FOR_TRAINING = 50
"""Skip users with fewer than this many completed outgoing tx."""

TRAINING_WINDOW_DAYS = 180  # ~6 months
"""Only fit on transactions from the last N days."""

TRAINING_CAP = 5000
"""Per-user training-set cap (most recent transactions)."""

LARGE_AMOUNT_VND = 10_000_000
"""Amount above which a transaction counts as 'large' for the
since-last-large feature."""

# Feature names — kept stable for debuggability / eval.
FEATURE_NAMES: tuple[str, ...] = (
    "log_amount",
    "hour_of_day",
    "day_of_week",
    "days_since_last_to_recipient",
    "recipient_freq_rank",
    "category_freq_rank",
    "is_new_recipient",
    "amount_vs_recipient_median_ratio",
    "amount_vs_user_median_ratio",
    "tx_since_last_large_amount",
)


# Training-side state -------------------------------------------------------


@dataclass
class _UserStats:
    """Aggregates we need both at train *and* inference time.

    Recipient / category frequency ranks are based on counts up to the
    most recent transaction the model saw — at draft-scoring time the
    user is presumably about to make a new transaction, so we ask "what
    is the rank of this recipient in everything we've already seen?".
    """

    recipient_counts: Counter = field(default_factory=Counter)
    # Per-recipient running median (instead of storing every amount —
    # keeps the dict small on heavy users).
    recipient_median: dict[str, _RunningMedian] = field(default_factory=dict)
    recipient_last_seen: dict[str, datetime] = field(default_factory=dict)
    category_counts: Counter = field(default_factory=Counter)
    last_tx_at: Optional[datetime] = None
    last_large_at: Optional[datetime] = None
    user_amount_median_estimator: _RunningMedian = field(default_factory=_RunningMedian)
    user_amount_median: float = 0.0
    n_train: int = 0
    # O(1) frequency-rank denominators — tracked incrementally so we
    # don't have to scan the counters on every row at training time.
    max_recipient_count: int = 0
    max_category_count: int = 0


@dataclass
class _UserModel:
    """Per-user fitted Isolation Forest + calibration metadata."""

    model: object  # sklearn.ensemble.IsolationForest
    stats: _UserStats
    score_p50: float  # median raw -decision_function on training set
    score_p95: float  # 95th-percentile raw -decision_function on training set
    score_p99: float  # 99th-percentile — calibration centre
    trained_at: datetime
    n_train: int


_models: dict[str, _UserModel] = {}
"""Process-local cache of per-user models."""


# Utilities ------------------------------------------------------------------


def is_enabled() -> bool:
    """Master switch — disabled if sklearn missing or env opt-out set."""
    if os.environ.get("OMNI_FRAUD_DISABLE", "").strip() in ("1", "true", "yes"):
        return False
    return _SKLEARN_OK


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _rank_from_counts(target_count: int, max_count: int) -> float:
    """Return a 0..1 frequency rank — 0 means "most frequent", 1 means
    "never seen".

    Implemented as ``1 - target_count / max_count`` rather than an exact
    sort-and-find rank: O(1) per call, monotonic with frequency, and
    cheap enough to compute 2× per row over hundreds of thousands of
    transactions without dominating training time.
    """
    if target_count <= 0 or max_count <= 0:
        return 1.0
    return max(0.0, 1.0 - target_count / max_count)


def _safe_log_amount(amount: int) -> float:
    return math.log1p(max(int(amount), 0))


def _trim_training_set(
    txs: Iterable[Transaction], reference_now: datetime
) -> list[Transaction]:
    """All completed tx within the 6-month window, ascending by ``created_at``.

    Capping to TRAINING_CAP happens later — see ``_fit_one`` — so the
    user-stats snapshot still reflects the full window even when the
    sklearn fit is on a recency-biased subset.
    """
    cutoff = reference_now - timedelta(days=TRAINING_WINDOW_DAYS)
    rows = [
        t
        for t in txs
        if t.status == "completed"
        and _ensure_aware(t.created_at) >= cutoff
    ]
    rows.sort(key=lambda t: t.created_at)
    return rows


# Feature extraction --------------------------------------------------------


def _walk_features(rows: list[Transaction]) -> tuple[np.ndarray, _UserStats]:
    """Replay history chronologically, emitting one feature vector per tx
    and returning the *final* stats — those are what inference will use
    for the next, not-yet-recorded transaction.
    """
    stats = _UserStats()
    feature_rows: list[list[float]] = []

    for tx in rows:
        amt = max(int(tx.amount), 0)
        created = _ensure_aware(tx.created_at)
        cid = tx.contact_id or ""

        recipient_count_before = stats.recipient_counts.get(cid, 0)
        category_count_before = stats.category_counts.get(tx.category or "other", 0)
        recipient_estimator = stats.recipient_median.get(cid)
        recipient_median_before = (
            recipient_estimator.median() if recipient_estimator is not None else 0.0
        )
        user_median_before = stats.user_amount_median or 0.0

        days_since_last_to_recipient = -1.0
        if cid in stats.recipient_last_seen:
            delta = (created - stats.recipient_last_seen[cid]).total_seconds() / 86400.0
            days_since_last_to_recipient = max(delta, 0.0)

        is_new_recipient = 1.0 if recipient_count_before == 0 else 0.0

        amount_vs_recipient = (
            (amt + 1) / (recipient_median_before + 1)
            if recipient_median_before > 0
            else 1.0
        )
        amount_vs_user = (
            (amt + 1) / (user_median_before + 1)
            if user_median_before > 0
            else 1.0
        )

        if stats.last_large_at is not None:
            tx_since_last_large = (
                created - stats.last_large_at
            ).total_seconds() / 86400.0
        else:
            tx_since_last_large = float(TRAINING_WINDOW_DAYS)

        recipient_rank = _rank_from_counts(
            recipient_count_before, stats.max_recipient_count
        )
        category_rank = _rank_from_counts(
            category_count_before, stats.max_category_count
        )

        feature_rows.append(
            [
                _safe_log_amount(amt),
                float(created.hour),
                float(created.weekday()),
                days_since_last_to_recipient,
                recipient_rank,
                category_rank,
                is_new_recipient,
                math.log1p(amount_vs_recipient),
                math.log1p(amount_vs_user),
                tx_since_last_large,
            ]
        )

        # Update running state *after* recording features for this tx
        stats.recipient_counts[cid] += 1
        if stats.recipient_counts[cid] > stats.max_recipient_count:
            stats.max_recipient_count = stats.recipient_counts[cid]
        if recipient_estimator is None:
            recipient_estimator = _RunningMedian()
            stats.recipient_median[cid] = recipient_estimator
        recipient_estimator.add(amt)
        stats.recipient_last_seen[cid] = created
        cat = tx.category or "other"
        stats.category_counts[cat] += 1
        if stats.category_counts[cat] > stats.max_category_count:
            stats.max_category_count = stats.category_counts[cat]
        if amt >= LARGE_AMOUNT_VND:
            stats.last_large_at = created
        stats.last_tx_at = created
        # Incremental user-wide median via two heaps — O(log n) per row.
        stats.user_amount_median_estimator.add(amt)
        stats.user_amount_median = stats.user_amount_median_estimator.median()

    stats.n_train = len(feature_rows)
    return np.asarray(feature_rows, dtype=np.float64), stats


def _build_inference_vector(
    *,
    amount: int,
    when: datetime,
    contact_id: Optional[str],
    category: str,
    stats: _UserStats,
) -> np.ndarray:
    """Produce a single 1xN feature vector for an in-progress draft using
    the stats snapshot captured at training time."""
    when = _ensure_aware(when)
    amt = max(int(amount), 0)

    recipient_count = stats.recipient_counts.get(contact_id or "", 0)
    category_count = stats.category_counts.get(category or "other", 0)
    recipient_estimator = stats.recipient_median.get(contact_id or "")
    recipient_median = (
        recipient_estimator.median() if recipient_estimator is not None else 0.0
    )
    user_median = stats.user_amount_median or 0.0

    if contact_id and contact_id in stats.recipient_last_seen:
        days_since_last_to_recipient = max(
            (when - stats.recipient_last_seen[contact_id]).total_seconds()
            / 86400.0,
            0.0,
        )
    else:
        days_since_last_to_recipient = -1.0

    is_new_recipient = 1.0 if recipient_count == 0 else 0.0

    amount_vs_recipient = (
        (amt + 1) / (recipient_median + 1) if recipient_median > 0 else 1.0
    )
    amount_vs_user = (
        (amt + 1) / (user_median + 1) if user_median > 0 else 1.0
    )

    if stats.last_large_at is not None:
        tx_since_last_large = (
            when - stats.last_large_at
        ).total_seconds() / 86400.0
    else:
        tx_since_last_large = float(TRAINING_WINDOW_DAYS)

    recipient_rank = _rank_from_counts(recipient_count, stats.max_recipient_count)
    category_rank = _rank_from_counts(category_count, stats.max_category_count)

    return np.asarray(
        [
            [
                _safe_log_amount(amt),
                float(when.hour),
                float(when.weekday()),
                days_since_last_to_recipient,
                recipient_rank,
                category_rank,
                is_new_recipient,
                math.log1p(amount_vs_recipient),
                math.log1p(amount_vs_user),
                tx_since_last_large,
            ]
        ],
        dtype=np.float64,
    )


# Training + inference ------------------------------------------------------


def _fit_one(rows: list[Transaction]) -> Optional[_UserModel]:
    if not _SKLEARN_OK:
        return None
    # ``_walk_features`` returns the FULL feature matrix and the running
    # stats *at the end* of the window. We feed sklearn at most TRAINING_CAP
    # rows (most recent first), but keep stats from the whole window — that
    # way recipient / category ranks reflect everything we know about the
    # user, not just the last 5000 events.
    X_all, stats = _walk_features(rows)
    if X_all.shape[0] < MIN_TX_FOR_TRAINING:
        return None
    X = X_all[-TRAINING_CAP:] if X_all.shape[0] > TRAINING_CAP else X_all

    # contamination=auto leans conservative; we calibrate the score
    # ourselves so the actual contamination assumption isn't load-bearing.
    model = IsolationForest(
        n_estimators=80,
        contamination="auto",
        max_samples=min(256, X.shape[0]),
        random_state=42,
        n_jobs=1,
    )
    model.fit(X)
    raw = -model.decision_function(X)  # higher = more anomalous
    p50 = float(np.quantile(raw, 0.5))
    p95 = float(np.quantile(raw, 0.95))
    p99 = float(np.quantile(raw, 0.99))
    return _UserModel(
        model=model,
        stats=stats,
        score_p50=p50,
        score_p95=p95,
        score_p99=p99,
        trained_at=datetime.now(timezone.utc),
        n_train=X.shape[0],
    )


def train_user(
    user_id: str,
    txs: list[Transaction],
    *,
    reference_now: Optional[datetime] = None,
) -> Optional[_UserModel]:
    """Train (or retrain) a single user's model in place.

    ``reference_now`` defaults to wall-clock; eval scripts replaying
    historical data should pass the latest transaction timestamp so the
    6-month window isn't degenerate.
    """
    if not is_enabled():
        return None
    if reference_now is None:
        reference_now = datetime.now(timezone.utc)
    rows = _trim_training_set(txs, reference_now)
    fitted = _fit_one(rows)
    if fitted is None:
        _models.pop(user_id, None)
        return None
    _models[user_id] = fitted
    return fitted


def train_fraud_models() -> dict[str, int]:
    """Train models for every user in the store.

    Returns ``{user_id: n_train}`` for users where a model was fit (so
    callers can log a one-line summary at startup).
    """
    if not is_enabled():
        logger.info("Fraud model disabled (OMNI_FRAUD_DISABLE or sklearn missing).")
        return {}

    # Imported lazily so this module stays importable in eval scripts that
    # don't want to bootstrap the full store.
    from ..store import get_store

    store = get_store()
    summary: dict[str, int] = {}
    t0 = time.perf_counter()
    for user_id in store.users:
        txs = store.transactions_of(user_id)
        fitted = train_user(user_id, txs)
        if fitted is not None:
            summary[user_id] = fitted.n_train
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if summary:
        logger.info(
            "Fraud model trained for %d user(s) in %.1fms (max n=%d).",
            len(summary),
            elapsed_ms,
            max(summary.values()),
        )
    else:
        logger.info("Fraud model: no user crossed the %d-tx threshold.",
                    MIN_TX_FOR_TRAINING)
    return summary


def _calibrate(raw: float, p50: float, p95: float, p99: float) -> float:
    """Map raw anomaly score -> [0, 1] using a per-user logistic.

    Calibrated so that:
      * raw == p95  -> score == 0.50  (warn-zone threshold candidates)
      * raw == p99  -> score == 0.70  (FRAUD_RISK_THRESHOLD, ~1% of train)
      * raw >> p99 -> score asymptotes towards 1.0

    Spread is ``p99 - p95`` so heavy-traffic users with a fat legit tail
    get auto-forgiven — what looks anomalous on a tight allowance
    account is normal on a high-velocity salary account.
    """
    spread = max(p99 - p95, 1e-6)
    z = (raw - p95) / spread
    # k chosen so sigmoid(k * 1.0) ≈ 0.70 → k = ln(0.7/0.3) ≈ 0.847.
    return 1.0 / (1.0 + math.exp(-0.847 * z))


def score_draft(
    *,
    user_id: str,
    amount: Optional[int],
    when: Optional[datetime] = None,
    recipient: Optional[Contact] = None,
    contact_id: Optional[str] = None,
    category: str = "other",
) -> Optional[float]:
    """Return an anomaly score in [0, 1], or None if no model is available.

    Returning ``None`` lets the rule engine treat the model as a soft
    dependency — if it isn't trained yet, the legacy z-score check still
    runs unchanged.
    """
    if not is_enabled():
        return None
    if amount is None or amount <= 0:
        return None
    fitted = _models.get(user_id)
    if fitted is None:
        return None

    if when is None:
        when = datetime.now(timezone.utc)
    cid = contact_id or (recipient.id if recipient else None)

    X = _build_inference_vector(
        amount=int(amount),
        when=when,
        contact_id=cid,
        category=category,
        stats=fitted.stats,
    )
    raw = float(-fitted.model.decision_function(X)[0])
    return _calibrate(raw, fitted.score_p50, fitted.score_p95, fitted.score_p99)


# Test helpers --------------------------------------------------------------


def clear_models() -> None:
    """Used by eval scripts that want to rebuild from scratch."""
    _models.clear()


def loaded_user_ids() -> list[str]:
    return list(_models.keys())


__all__ = [
    "FRAUD_RISK_THRESHOLD",
    "MIN_TX_FOR_TRAINING",
    "FEATURE_NAMES",
    "is_enabled",
    "train_fraud_models",
    "train_user",
    "score_draft",
    "clear_models",
    "loaded_user_ids",
]

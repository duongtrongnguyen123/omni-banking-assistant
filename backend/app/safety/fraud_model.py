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

import logging
import math
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, cast

import numpy as np

try:  # sklearn is heavy — keep the module importable even if missing.
    from sklearn.ensemble import IsolationForest

    _SKLEARN_OK = True
except ImportError:  # pragma: no cover
    IsolationForest = None  # type: ignore[assignment]
    _SKLEARN_OK = False

from ..models.schemas import Contact, Transaction

logger = logging.getLogger(__name__)
_LOG = logger  # alias kept for code that grepped for `_LOG` style.

# Tuning knobs ---------------------------------------------------------------

_DEFAULT_FRAUD_RISK_THRESHOLD = 0.5
"""Hard-coded default used when ``OMNI_FRAUD_RISK_THRESHOLD`` is unset or
unparseable. Kept private — production code should read
``FRAUD_RISK_THRESHOLD`` so an operator can retune at deploy time."""


def _recompute_threshold() -> float:
    """Re-read ``OMNI_FRAUD_RISK_THRESHOLD`` from the environment.

    Exposed for tests that monkeypatch ``os.environ``. Bad inputs
    (non-numeric, out of [0, 1]) are logged at WARNING and the default
    is preserved — the rule engine must never see a NaN threshold.
    """
    raw = os.environ.get("OMNI_FRAUD_RISK_THRESHOLD")
    if raw is None or raw.strip() == "":
        return _DEFAULT_FRAUD_RISK_THRESHOLD
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        _LOG.warning(
            "OMNI_FRAUD_RISK_THRESHOLD=%r is not a number; keeping default %.2f",
            raw,
            _DEFAULT_FRAUD_RISK_THRESHOLD,
        )
        return _DEFAULT_FRAUD_RISK_THRESHOLD
    if not math.isfinite(parsed) or parsed < 0.0 or parsed > 1.0:
        _LOG.warning(
            "OMNI_FRAUD_RISK_THRESHOLD=%r outside [0, 1]; keeping default %.2f",
            raw,
            _DEFAULT_FRAUD_RISK_THRESHOLD,
        )
        return _DEFAULT_FRAUD_RISK_THRESHOLD
    return parsed


FRAUD_RISK_THRESHOLD = _recompute_threshold()
"""Score above which `fraud_risk_high` flag is raised.

Calibrated from public-dataset eval (BankSim 0.75 recall, PaySim 0.74
recall both at threshold 0.5 → 0.95-band). Earlier 0.7 default dropped
recall to ~0.13 — mis-calibrated and documented as such in
``docs/eval-real-data.md`` since 2026-06-06.

Override at deploy time via ``OMNI_FRAUD_RISK_THRESHOLD`` (float in
``[0, 1]``). Bad values fall back to ``_DEFAULT_FRAUD_RISK_THRESHOLD``
and emit a ``_LOG.warning``."""

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
    recipient_amounts: dict[str, list[int]] = field(default_factory=dict)
    recipient_last_seen: dict[str, datetime] = field(default_factory=dict)
    category_counts: Counter = field(default_factory=Counter)
    last_tx_at: Optional[datetime] = None
    last_large_at: Optional[datetime] = None
    user_amount_median: float = 0.0
    n_train: int = 0


@dataclass
class _UserModel:
    """Per-user fitted Isolation Forest + calibration metadata."""

    model: object  # sklearn.ensemble.IsolationForest
    stats: _UserStats
    score_p50: float  # median raw -decision_function on training set
    score_p95: float  # 95th-percentile raw -decision_function on training set
    trained_at: datetime
    n_train: int


_models: dict[str, _UserModel] = {}
"""Process-local cache of per-user models.

Uvicorn dispatches requests onto a thread pool, so ``_models`` and
``_last_retrain_attempt`` are visible to concurrent ``score_draft`` /
``train_user`` calls. Guard every read/write with ``_models_lock`` and
take a *snapshot* reference (the immutable ``_UserModel`` dataclass) out
of the dict under the lock — sklearn ``predict_proba`` runs lock-free
on the snapshot so a parallel retrain can't swap the model mid-flight.
"""

_models_lock = threading.Lock()
"""Guards ``_models`` and ``_last_retrain_attempt``. Hold only for the
duration of dict ops + snapshot read; never around sklearn calls."""

# Lazy-retrain bookkeeping ---------------------------------------------------
#
# ``train_fraud_models`` runs once at startup. As the user adds new tx /
# contacts during the demo, ``stats.recipient_counts`` becomes stale and
# known recipients keep scoring as ``is_new_recipient=1`` — that spuriously
# raises ``fraud_risk_high`` for a recipient the user already paid this
# session. We retrain in-place when:
#   1. ``score_draft`` is asked about a ``contact_id`` not in the snapshot
#      stats (definitely-new evidence — retrain immediately, no cooldown);
#   2. OR the user has *any* tx newer than ``trained_at`` AND we last
#      retrained more than ``LAZY_RETRAIN_COOLDOWN_SEC`` ago.
#
# The cooldown stops hot-loops when many drafts arrive in quick succession.
LAZY_RETRAIN_COOLDOWN_SEC = 600  # 10 minutes — generous, demo-safe
_last_retrain_attempt: dict[str, float] = {}
"""``user_id -> monotonic seconds`` of the last retrain attempt. Used to
throttle the time-driven retrain branch. The new-contact branch is *not*
throttled because it's a definite cache-miss signal."""


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


def _rank_from_counts(target_count: int, counts: Counter) -> float:
    """Return a 0..1 frequency rank — 0 means "most frequent", 1 means
    "never seen". Stable across ties, derived from sorted counts."""
    if not counts:
        return 1.0
    sorted_counts = sorted(counts.values(), reverse=True)
    n = len(sorted_counts)
    # Highest count -> rank 0.0, lowest count -> rank ~1.0.
    # For never-seen items we return 1.0.
    if target_count <= 0:
        return 1.0
    # binary search would be fine; n is tiny in practice
    for idx, c in enumerate(sorted_counts):
        if c <= target_count:
            return idx / max(n - 1, 1)
    return 1.0


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
        recipient_amounts_before = stats.recipient_amounts.get(cid, [])
        recipient_median_before = (
            float(np.median(recipient_amounts_before))
            if recipient_amounts_before
            else 0.0
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
            recipient_count_before, stats.recipient_counts
        )
        category_rank = _rank_from_counts(
            category_count_before, stats.category_counts
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
        stats.recipient_amounts.setdefault(cid, []).append(amt)
        stats.recipient_last_seen[cid] = created
        stats.category_counts[tx.category or "other"] += 1
        if amt >= LARGE_AMOUNT_VND:
            stats.last_large_at = created
        stats.last_tx_at = created
        # Cheap incremental "user median" — exact median on each step is
        # O(n log n), but training tops out at 5000 rows so it's fine.
        all_amounts = [a for amounts in stats.recipient_amounts.values() for a in amounts]
        stats.user_amount_median = float(np.median(all_amounts))

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
    recipient_amounts = stats.recipient_amounts.get(contact_id or "", [])
    recipient_median = (
        float(np.median(recipient_amounts)) if recipient_amounts else 0.0
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

    recipient_rank = _rank_from_counts(recipient_count, stats.recipient_counts)
    category_rank = _rank_from_counts(category_count, stats.category_counts)

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
    assert IsolationForest is not None  # _SKLEARN_OK gated above
    max_samples: int = min(256, int(X.shape[0]))
    _IF = cast(Any, IsolationForest)
    model = _IF(
        n_estimators=80,
        contamination="auto",
        max_samples=max_samples,
        random_state=42,
        n_jobs=1,
    )
    model.fit(X)
    raw = -model.decision_function(X)  # higher = more anomalous
    p50 = float(np.quantile(raw, 0.5))
    p95 = float(np.quantile(raw, 0.95))
    return _UserModel(
        model=model,
        stats=stats,
        score_p50=p50,
        score_p95=p95,
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
    # Mutate the shared cache under the lock so concurrent ``score_draft``
    # calls see either the old model or the new one — never a partially
    # constructed dict entry.
    with _models_lock:
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
    for user_id in store.all_user_ids():
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


def _calibrate(raw: float, p50: float, p95: float) -> float:
    """Map raw anomaly score -> [0, 1] using a per-user logistic.

    Centred at p95 so that ~5% of training rows score above 0.5 and only
    truly tail-end outliers cross 0.7.

    Flat-history guard: when a user has constant-amount history (e.g. 50
    identical salary credits) ``p95 - p50`` collapses to ~0, the absolute
    floor ``1e-6`` survives, and any outlier saturates the sigmoid to
    ``1.0``. We instead floor the spread to ``5%`` of ``|p95|`` (the
    natural scale of raw scores) with ``1e-3`` as an absolute backstop —
    so a 2x outlier on flat history lands in the mid-range (0.5–0.8)
    where the rule engine can still escalate proportionally instead of
    treating every blip as catastrophic.
    """
    spread = max(p95 - p50, abs(p95) * 0.05, 1e-3)
    z = (raw - p95) / spread
    # Steeper than a vanilla sigmoid so the transition is decisive.
    return 1.0 / (1.0 + math.exp(-3.0 * z))


def _maybe_lazy_retrain(
    user_id: str,
    fitted: Optional[_UserModel],
    cid: Optional[str],
) -> Optional[_UserModel]:
    """Refresh ``fitted`` from the store when the snapshot is stale.

    Returns the (possibly retrained) model, or ``None`` if no model could
    be fit. ``score_draft`` calls this just before vectorising the draft
    so a brand-new contact_id is reflected in ``stats.recipient_counts``
    before the next score is computed.

    Two trigger conditions:
      * **definite new contact**: ``cid`` is non-empty and not present in
        ``stats.recipient_counts`` — the cached stats are demonstrably
        out of date for this scoring call. No cooldown.
      * **time-based**: the user has at least one tx newer than
        ``fitted.trained_at`` AND we haven't retrained for
        ``LAZY_RETRAIN_COOLDOWN_SEC`` seconds. Throttled to avoid a
        hot-loop on bursty traffic.
    """
    if not is_enabled():
        return fitted

    # If the user was never trained at startup (n_train < MIN, sklearn off,
    # or simply never seeded) keep the original semantics — ``score_draft``
    # returns ``None`` and the legacy z-score path runs. Only an *existing*
    # cached model is eligible for lazy refresh.
    if fitted is None:
        return None

    new_contact = bool(cid and cid not in fitted.stats.recipient_counts)

    cooldown_ok = True
    if not new_contact:
        with _models_lock:
            last_attempt = _last_retrain_attempt.get(user_id, 0.0)
        cooldown_ok = (time.monotonic() - last_attempt) >= LAZY_RETRAIN_COOLDOWN_SEC

    if not new_contact and not cooldown_ok:
        return fitted

    # Defer the store import — keeps the module importable from eval
    # scripts that never touch the runtime DB.
    try:
        from ..store import get_store

        store = get_store()
        txs = store.transactions_of(user_id)
    except Exception:  # pragma: no cover — defensive
        return fitted

    if not txs:
        return fitted

    if not new_contact and fitted is not None:
        # Time-based path: only bother if there's genuinely fresher data.
        latest = max(
            (_ensure_aware(t.created_at) for t in txs if t.status == "completed"),
            default=None,
        )
        trained_at = _ensure_aware(fitted.trained_at)
        if latest is None or latest <= trained_at:
            with _models_lock:
                _last_retrain_attempt[user_id] = time.monotonic()
            return fitted

    with _models_lock:
        _last_retrain_attempt[user_id] = time.monotonic()
    refreshed = train_user(user_id, txs)
    return refreshed if refreshed is not None else fitted


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
    # Snapshot the cached model under the lock — a parallel ``train_user``
    # can mutate ``_models[user_id]`` immediately after we release. Holding
    # the immutable ``_UserModel`` reference lets us run sklearn lock-free
    # without worrying about mid-flight swap.
    with _models_lock:
        fitted = _models.get(user_id)
    cid = contact_id or (recipient.id if recipient else None)

    # Lazy retrain if the snapshot is stale (new contact seen this session,
    # or fresh tx since last fit). Keeps ``is_new_recipient`` honest after
    # the first transfer to a contact added mid-demo. ``_maybe_lazy_retrain``
    # may itself swap the cache entry — what it returns is the model we
    # should score against right now.
    fitted = _maybe_lazy_retrain(user_id, fitted, cid)
    if fitted is None:
        return None

    if when is None:
        when = datetime.now(timezone.utc)

    X = _build_inference_vector(
        amount=int(amount),
        when=when,
        contact_id=cid,
        category=category,
        stats=fitted.stats,
    )
    raw = float(-cast(Any, fitted.model).decision_function(X)[0])
    return _calibrate(raw, fitted.score_p50, fitted.score_p95)


# Test helpers --------------------------------------------------------------


def clear_models() -> None:
    """Used by eval scripts that want to rebuild from scratch."""
    with _models_lock:
        _models.clear()
        _last_retrain_attempt.clear()


def loaded_user_ids() -> list[str]:
    with _models_lock:
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
    "_recompute_threshold",
]

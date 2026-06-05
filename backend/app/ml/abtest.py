"""In-process A/B router for suggester weight experiments.

The next-recipient suggester combines three signals — Random-Forest
``predict_proba``, a frequency prior, and the Vietnamese-locale rule
scorer — with hand-picked weights. ``docs/eval-real-data.md`` shows that
``tree+freq`` (0.6/0.4/0.0) beats ``tree+freq+rule`` on the BankSim
public dataset while the rule-heavy mix beats on the Vietnamese-tuned
synthetic seed. Production traffic likely sits somewhere in between, so
the right answer is *don't guess* — run a live A/B and let the wins
accumulate.

Design notes
------------
* **In-process only.** No external dependency, no Redis. Single dict
  guarded by a re-entrant lock. Persistence is best-effort JSON.
* **Deterministic routing.** ``hash(user_id) mod num_arms`` so the same
  user always sees the same arm until ``reset()`` is called or arms are
  reshuffled — important for honest evaluation (a user can't switch
  mid-experiment).
* **Outcome recording is explicit.** The caller — typically the
  orchestrator after a confirmed transfer — passes ``correct=True``
  iff the chosen contact equals the top-1 the suggester picked for
  that user.
* **95 % CI via Wilson interval.** Better than normal approximation
  at small ``n``, no scipy dependency.

The module never imports ``suggester`` to avoid circular imports — the
suggester pulls arm weights via ``get_arm_weights`` and reports outcomes
via ``record_outcome``.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import threading
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("omni.ml.abtest")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Arm:
    """One treatment in the experiment.

    ``weights`` is a ``(tree, freq, rule)`` triple consumed by
    ``suggester.suggest``. ``trials`` counts how many times this arm was
    *served* and an outcome was eventually recorded; ``hits`` counts how
    many of those outcomes were ``correct=True``.
    """

    name: str
    weights: tuple[float, float, float]
    trials: int = 0
    hits: int = 0


@dataclass
class _State:
    arms: dict[str, Arm] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)


_LOCK = threading.RLock()
_STATE = _State()


# ---------------------------------------------------------------------------
# Enable / disable knob (CI uses OMNI_DISABLE_ABTEST=1)
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    """Return False when ``OMNI_DISABLE_ABTEST=1`` — used by CI to keep
    deterministic suggester behaviour out of the unit tests."""
    return os.environ.get("OMNI_DISABLE_ABTEST", "0") != "1"


# ---------------------------------------------------------------------------
# Arm registry
# ---------------------------------------------------------------------------


# Headline arms — what we register at startup. The first three come
# straight from ``docs/eval-real-data.md`` §3; ``tree_only`` is a
# control to confirm the freq prior earns its keep.
DEFAULT_ARMS: list[tuple[str, tuple[float, float, float]]] = [
    ("tree_freq", (0.6, 0.4, 0.0)),    # BankSim winner
    ("rule_heavy", (0.2, 0.2, 0.6)),   # VN-locale winner on the demo seed
    ("balanced", (0.35, 0.25, 0.4)),   # middle of the road
    ("tree_only", (1.0, 0.0, 0.0)),    # control — freq prior off
]


def register_arm(name: str, weights: tuple[float, float, float]) -> None:
    """Add or replace an arm. Weights are kept as-is; the suggester
    decides how to normalise them at scoring time."""
    if not isinstance(weights, tuple) or len(weights) != 3:
        raise ValueError("weights must be a (tree, freq, rule) tuple")
    with _LOCK:
        if name not in _STATE.arms:
            _STATE.order.append(name)
        _STATE.arms[name] = Arm(name=name, weights=weights)


def register_defaults() -> None:
    """Idempotent — used by FastAPI startup."""
    with _LOCK:
        if _STATE.arms:
            return
        for name, weights in DEFAULT_ARMS:
            register_arm(name, weights)
    # After registering, attempt to restore persisted trial/hit counts
    # and bandit state. Persisted file is best-effort — if it's missing
    # or stale we silently start fresh.
    try:
        from . import bandit
        bandit.load_state()
    except Exception as e:  # never fail startup
        log.debug("bandit.load_state skipped: %s", e)


def get_arm(name: str) -> Optional[Arm]:
    with _LOCK:
        return _STATE.arms.get(name)


def get_arm_weights(name: str) -> Optional[tuple[float, float, float]]:
    arm = get_arm(name)
    return arm.weights if arm else None


def arm_names() -> list[str]:
    with _LOCK:
        return list(_STATE.order)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _hash_bucket(user_id: str, num_arms: int) -> int:
    """Deterministic stable hash. We don't use ``hash()`` because Python
    salts it per-process, which would re-assign users every restart and
    confuse our outcome buckets."""
    if num_arms <= 0:
        return 0
    h = hashlib.blake2b(user_id.encode("utf-8"), digest_size=8).digest()
    n = int.from_bytes(h, "big")
    return n % num_arms


def pick_arm(user_id: str) -> str:
    """Return the arm name for this user.

    Routing protocol:
      1. If ``OMNI_DISABLE_ABTEST=1`` or no arms registered, return the
         literal string ``"default"`` and let the caller fall back to
         auto weights.
      2. If the Thompson-sampling bandit has enough data on every arm
         (≥30 trials each), delegate to it.
      3. Otherwise route by deterministic hash so each user is sticky to
         one arm until the bandit takes over.
    """
    if not is_enabled():
        return "default"
    with _LOCK:
        if not _STATE.order:
            return "default"
        names = list(_STATE.order)

    # Delegate to bandit when it's ready. We import lazily to keep this
    # module side-effect-free at import time and to avoid circular deps.
    try:
        from . import bandit
        chosen = bandit.maybe_pick(user_id, names)
        if chosen is not None:
            return chosen
    except Exception as e:  # pragma: no cover — defensive
        log.debug("bandit.maybe_pick skipped: %s", e)

    return names[_hash_bucket(user_id, len(names))]


# ---------------------------------------------------------------------------
# Outcome recording
# ---------------------------------------------------------------------------


def record_outcome(user_id: str, arm: str, correct: bool) -> None:
    """Log a hit/miss for the named arm.

    The orchestrator calls this when a transfer confirms and the chosen
    contact id is now ground truth. ``user_id`` is accepted for future
    per-user logging hooks; we don't slice on it here.
    """
    if not is_enabled():
        return
    with _LOCK:
        a = _STATE.arms.get(arm)
        if a is None:
            log.debug("record_outcome: unknown arm %r", arm)
            return
        a.trials += 1
        if correct:
            a.hits += 1
    # Persist asynchronously of the request path — but cheap enough we
    # just inline it. Best-effort: errors are swallowed.
    try:
        from . import bandit
        bandit.save_state()
    except Exception as e:  # pragma: no cover
        log.debug("bandit.save_state skipped: %s", e)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _wilson_ci(hits: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — well-behaved at low ``trials``.

    Returns ``(lo, hi)`` in [0, 1]. For ``trials == 0`` we collapse to
    ``(0.0, 1.0)`` which is the correct uninformative answer.
    """
    if trials <= 0:
        return (0.0, 1.0)
    n = float(trials)
    p = hits / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)
    lo = max(0.0, (centre - margin) / denom)
    hi = min(1.0, (centre + margin) / denom)
    return (lo, hi)


def report() -> dict:
    """Per-arm dashboard: trials, hits, hit_rate, 95 % CI, weights.

    Output shape matches the doctring example in the task spec — easy
    for the frontend dashboard to render with no transformation.
    """
    with _LOCK:
        out: dict[str, dict] = {}
        for name in _STATE.order:
            a = _STATE.arms[name]
            rate = (a.hits / a.trials) if a.trials else 0.0
            lo, hi = _wilson_ci(a.hits, a.trials)
            out[name] = {
                "trials": a.trials,
                "hits": a.hits,
                "hit_rate": round(rate, 4),
                "ci": [round(lo, 4), round(hi, 4)],
                "weights": list(a.weights),
            }
    return out


def reset() -> None:
    """Clear trial/hit counters and bandit state. Useful for admin
    endpoint + tests + the eval script."""
    with _LOCK:
        for a in _STATE.arms.values():
            a.trials = 0
            a.hits = 0
    try:
        from . import bandit
        bandit.reset_state()
    except Exception as e:  # pragma: no cover
        log.debug("bandit.reset_state skipped: %s", e)

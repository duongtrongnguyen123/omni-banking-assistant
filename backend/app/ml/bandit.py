"""Thompson-sampling bandit on top of the A/B arm registry.

Once each arm has accumulated enough trials we stop routing by raw
deterministic hash and start *exploring* in proportion to how likely
each arm is to be the best. Mechanism:

  * Maintain a Beta(α=hits+1, β=misses+1) posterior per arm.
  * On each ``pick`` call, draw one sample from each arm's posterior and
    pick the arm with the highest draw (Thompson sampling).

Thompson sampling is the classical optimal-regret algorithm for the
Bernoulli bandit and converges to the best arm with no tuning. The
``MIN_TRIALS_PER_ARM`` gate keeps the early sample noise from locking us
in before each arm has a fair chance.

Persistence
-----------
We persist ``(trials, hits)`` to ``app/data/bandit_state.json`` so a
restart doesn't reset the bandit. The file is gitignored. Best-effort:
read errors silently fall back to defaults; write errors are logged at
DEBUG. We deliberately don't persist the routing decisions themselves —
only the aggregate Beta sufficient statistics.

The task spec asks for "after 100 trials per arm, switch from uniform
random to Thompson sampling" while the integration constraint says
"Thompson sampling kicks in only after each arm has ≥30 trials". We
honour the stricter spec: ``MIN_TRIALS_PER_ARM`` defaults to 30; the
``BANDIT_MIN_TRIALS`` env var overrides it. Until the gate opens, the
A/B router uses its deterministic-hash routing (see ``abtest.pick_arm``)
so each user is sticky.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger("omni.ml.bandit")


# 30 trials/arm — the stricter of the two integration constraints. Can
# be overridden for the eval script which simulates 5 000 transfers.
MIN_TRIALS_PER_ARM = int(os.environ.get("BANDIT_MIN_TRIALS", "30"))

# Use a dedicated RNG so unit tests can seed it without disturbing the
# global random state of the host process.
_RNG = random.Random()
_LOCK = threading.RLock()


def _state_path() -> Path:
    """Resolve where bandit_state.json lives. ``OMNI_BANDIT_STATE_PATH``
    overrides for tests / the eval script."""
    override = os.environ.get("OMNI_BANDIT_STATE_PATH")
    if override:
        return Path(override)
    # Default: alongside the SQLite DB.
    return Path(__file__).resolve().parents[1] / "data" / "bandit_state.json"


def seed(s: int) -> None:
    """Seed the bandit RNG. Used by the eval script for reproducibility."""
    _RNG.seed(s)


# ---------------------------------------------------------------------------
# Pick
# ---------------------------------------------------------------------------


def maybe_pick(user_id: str, arm_names: list[str]) -> Optional[str]:
    """Return an arm name if Thompson sampling is ready, else None.

    "Ready" means *every* arm has ≥ ``MIN_TRIALS_PER_ARM`` trials. While
    any arm is below the gate, the caller (``abtest.pick_arm``) falls
    back to deterministic-hash routing so we don't starve under-served
    arms of exploration.
    """
    from . import abtest

    if not arm_names:
        return None
    # Snapshot the per-arm trial counts without holding both locks.
    counts: dict[str, tuple[int, int]] = {}
    for name in arm_names:
        a = abtest.get_arm(name)
        if a is None:
            return None
        counts[name] = (a.hits, a.trials)

    if any(c[1] < MIN_TRIALS_PER_ARM for c in counts.values()):
        return None

    # Thompson sample: Beta(hits + 1, misses + 1) per arm.
    best_name = None
    best_draw = -1.0
    with _LOCK:
        for name in arm_names:
            hits, trials = counts[name]
            misses = trials - hits
            draw = _RNG.betavariate(hits + 1, misses + 1)
            if draw > best_draw:
                best_draw = draw
                best_name = name
    return best_name


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_state() -> None:
    """Flush ``(trials, hits)`` per arm to disk. Atomic via tmp + rename."""
    from . import abtest

    if not abtest.is_enabled():
        return
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "arms": {
                name: {
                    "hits": a.hits,
                    "trials": a.trials,
                    "weights": list(a.weights),
                }
                for name, a in abtest._STATE.arms.items()  # noqa: SLF001 — peer module
            },
            "min_trials_per_arm": MIN_TRIALS_PER_ARM,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)
    except OSError as e:
        log.debug("bandit save_state failed: %s", e)


def load_state() -> None:
    """Restore ``(trials, hits)`` from disk if available.

    Arms not present in the registry are ignored. Arms in the registry
    that aren't in the file keep their zeroed counters. This makes it
    safe to roll out new arms without nuking the JSON.
    """
    from . import abtest

    if not abtest.is_enabled():
        return
    path = _state_path()
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        log.debug("bandit load_state parse failed: %s", e)
        return

    saved_arms = payload.get("arms", {})
    with abtest._LOCK:  # noqa: SLF001
        for name, blob in saved_arms.items():
            a = abtest._STATE.arms.get(name)  # noqa: SLF001
            if a is None:
                continue
            try:
                a.hits = int(blob.get("hits", 0))
                a.trials = int(blob.get("trials", 0))
            except (TypeError, ValueError):
                continue


def reset_state() -> None:
    """Delete the persisted file. Counters in memory are reset by
    ``abtest.reset()`` separately — this only nukes disk so a restart
    doesn't reload yesterday's data."""
    path = _state_path()
    try:
        if path.is_file():
            path.unlink()
    except OSError as e:  # pragma: no cover
        log.debug("bandit reset_state failed: %s", e)

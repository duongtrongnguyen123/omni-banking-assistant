"""A/B aware wrapper around ``app.ml.suggester.suggest``.

The production suggester (``app/ml/suggester.py``) is intentionally
left untouched per task constraints — this wrapper:

  1. Picks an A/B arm for ``user_id`` from ``app.ml.abtest`` (or falls
     back to ``"default"`` when the framework is disabled).
  2. Calls the production ``suggest()`` with the arm's weight triple
     overriding the auto-weight heuristic.
  3. Remembers the top-1 ``contact_id`` returned for the user so that
     when a transfer eventually confirms we can compute ``correct``
     for the outcome record.

The "last top-1" memory is a small in-process dict — sessions are
already in-memory in this hackathon (see ``app/context/session_store.py``)
so adding one more is consistent and trivial. If we ever swap sessions
to Redis, this memory moves with them; the contract is the same.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

from ..ml import abtest, suggester

log = logging.getLogger("omni.services.suggester")


# Per-user memory: "the last time we suggested for this user, which arm
# did we use, and which contact_id was the top-1 pick?" The orchestrator
# reads this back when the user confirms a transfer.
_LOCK = threading.Lock()
_LAST: dict[str, dict] = {}


def suggest_for(
    user_id: str,
    when: Optional[datetime] = None,
    k: int = 5,
    include_all: bool = False,
) -> tuple[str, list[dict]]:
    """Return ``(arm_name, suggestions)``.

    ``arm_name`` is the arm whose weights produced the suggestions;
    ``"default"`` when the A/B router is disabled and the production
    auto-weight heuristic ran instead.
    """
    arm = abtest.pick_arm(user_id)
    weights = abtest.get_arm_weights(arm) if arm != "default" else None
    if weights is not None:
        tw, fw, rw = weights
        results = suggester.suggest(
            user_id,
            when=when,
            k=k,
            tree_weight=tw,
            freq_weight=fw,
            rule_weight=rw,
            include_all=include_all,
        )
    else:
        results = suggester.suggest(
            user_id, when=when, k=k, include_all=include_all
        )

    # Stash top-1 so the orchestrator can score outcome at confirm-time.
    top1: Optional[str] = None
    if results:
        c = results[0].get("contact") or {}
        top1 = c.get("id")
    with _LOCK:
        _LAST[user_id] = {"arm": arm, "top1": top1}
    return arm, results


def consume_outcome(user_id: str, chosen_contact_id: str) -> None:
    """Called by the orchestrator after ``_execute_and_record`` when the
    user confirms a transfer. If we have a recent suggestion record for
    this user, score it as a hit/miss against the chosen contact.

    Returns nothing — best-effort signal-recording.
    """
    if not abtest.is_enabled():
        return
    with _LOCK:
        last = _LAST.pop(user_id, None)
    if last is None:
        return
    arm = last.get("arm")
    top1 = last.get("top1")
    if not arm or arm == "default" or top1 is None:
        return
    try:
        abtest.record_outcome(user_id, arm, correct=(top1 == chosen_contact_id))
    except Exception as e:  # pragma: no cover — never break the transfer
        log.debug("record_outcome failed: %s", e)


def peek_arm(user_id: str) -> Optional[str]:
    """Test helper / introspection — which arm did we last serve ``user_id``?"""
    with _LOCK:
        last = _LAST.get(user_id)
    return last.get("arm") if last else None

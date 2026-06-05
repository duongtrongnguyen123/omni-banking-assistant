"""Per-user token-bucket rate limiter for the chat route.

stdlib only. Tracks one bucket per ``user_id`` in process memory; safe
for the single-instance demo deployment. For multi-instance we'd shift
this to Redis (the session backend), but that's overkill for the
hackathon — the goal here is purely to fend off accidental click-spam
from judges or abusive scripts during the live demo.

Usage::

    from .deps import current_user
    from ._ratelimit import enforce_user_rate_limit

    @router.post(...)
    def chat(req, user_id: str = Depends(current_user)):
        enforce_user_rate_limit(user_id)
        ...

Default: 60 requests per 60 seconds per user. Override per-process via
``OMNI_CHAT_RATE_LIMIT`` (integer, requests per minute, ``0`` disables).
"""

from __future__ import annotations

import os
import threading
import time

from fastapi import HTTPException

_RATE_PER_MIN_DEFAULT = 60

_lock = threading.Lock()
# {user_id: (tokens: float, last_refill_ts: float)}
_buckets: dict[str, tuple[float, float]] = {}


def _capacity_per_minute() -> int:
    raw = os.environ.get("OMNI_CHAT_RATE_LIMIT")
    if not raw:
        return _RATE_PER_MIN_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return _RATE_PER_MIN_DEFAULT


def reset() -> None:
    """Wipe every bucket. Test-only — not exposed via HTTP."""
    with _lock:
        _buckets.clear()


def enforce_user_rate_limit(user_id: str) -> None:
    """Raise 429 if ``user_id`` has spent its per-minute budget.

    Token bucket: capacity = ``OMNI_CHAT_RATE_LIMIT`` (default 60),
    refill rate = capacity / 60 tokens per second. A request costs 1
    token. When the bucket runs dry we compute exactly how long until
    the next token is available and surface it as ``Retry-After``.
    """
    capacity = _capacity_per_minute()
    if capacity <= 0:
        return
    refill_per_sec = capacity / 60.0
    now = time.monotonic()
    with _lock:
        tokens, last = _buckets.get(user_id, (float(capacity), now))
        elapsed = max(0.0, now - last)
        tokens = min(float(capacity), tokens + elapsed * refill_per_sec)
        if tokens < 1.0:
            # Compute seconds until the bucket has at least one whole token.
            wait_s = max(1, int((1.0 - tokens) / refill_per_sec) + 1)
            # Persist the updated `last` so subsequent calls don't accrue
            # phantom tokens between bursts.
            _buckets[user_id] = (tokens, now)
            raise HTTPException(
                status_code=429,
                detail="Bạn gửi hơi nhanh — chờ chút rồi thử lại nhé",
                headers={"Retry-After": str(wait_s)},
            )
        tokens -= 1.0
        _buckets[user_id] = (tokens, now)

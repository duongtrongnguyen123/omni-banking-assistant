"""Privacy mode + outbound LLM payload audit.

Three modes (``OMNI_PRIVACY_MODE`` env var):

* ``off``        — default. Outbound LLM payloads are sent verbatim, no
                   redaction, no audit entries beyond a size record.
* ``redact``     — every outbound ``user_message`` (and the rendered
                   history role contents) is run through
                   :func:`app.nlp.redactor.redact` before it leaves the
                   process. The audit ring buffer records (original_size,
                   redacted_size, redaction_count).
* ``local-only`` — no provider is allowed to make a network call. The
                   LLM layer reports zero enabled providers, the NLU
                   pipeline falls through to the rule-based extractor,
                   and the in-process audit buffer logs the suppressed
                   call so judges can verify nothing left the device.

The audit ring buffer is in-process, bounded to 100 entries (FIFO). It is
intentionally NOT persisted — production would push entries to a real
log sink; for the hackathon the ``/api/admin/llm-audit`` endpoint is
enough to demonstrate the trust contract.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Literal, Optional

Mode = Literal["off", "redact", "local-only"]

_VALID_MODES: tuple[str, ...] = ("off", "redact", "local-only")
_DEFAULT_MODE: Mode = "off"

# Runtime mode — initialised from env, mutable via the admin endpoint.
_current_mode: Mode = _DEFAULT_MODE
_mode_lock = threading.Lock()


def _read_env_mode() -> Mode:
    raw = (os.environ.get("OMNI_PRIVACY_MODE") or "").strip().lower()
    if raw in _VALID_MODES:
        return raw  # type: ignore[return-value]
    return _DEFAULT_MODE


# Initialise lazily so tests that mutate the env before importing this
# module still see the right default.
def _bootstrap() -> None:
    global _current_mode
    _current_mode = _read_env_mode()


_bootstrap()


def get_mode() -> Mode:
    """Return the currently-active privacy mode."""
    return _current_mode


def set_mode(mode: str) -> Mode:
    """Update the runtime privacy mode. Raises ``ValueError`` on garbage."""
    global _current_mode
    norm = (mode or "").strip().lower()
    if norm not in _VALID_MODES:
        raise ValueError(
            f"Invalid privacy mode {mode!r}. Expected one of {_VALID_MODES}."
        )
    with _mode_lock:
        _current_mode = norm  # type: ignore[assignment]
    return _current_mode


# ---------------------------------------------------------------------------
# Audit ring buffer
# ---------------------------------------------------------------------------

_AUDIT_MAX = 100
_audit: Deque[dict] = deque(maxlen=_AUDIT_MAX)
_audit_lock = threading.Lock()

# Strictly monotonic sequence number — useful for clients diffing the
# log between polls without depending on the clock.
_audit_seq = 0


def record_llm_call(
    *,
    provider: str,
    mode: Mode,
    original_size: int,
    redacted_size: int,
    redaction_count: int,
    redaction_breakdown: Optional[Dict[str, int]] = None,
    suppressed: bool = False,
    note: Optional[str] = None,
) -> dict:
    """Append a single audit entry. Returns the persisted dict (with seq).

    ``suppressed=True`` means the privacy mode (``local-only``) blocked the
    outbound call entirely — no bytes left the process.
    """
    global _audit_seq
    with _audit_lock:
        _audit_seq += 1
        entry = {
            "seq": _audit_seq,
            "ts": time.time(),
            "provider": provider,
            "mode": mode,
            "original_size": original_size,
            "redacted_size": redacted_size,
            "redaction_count": redaction_count,
            "redaction_breakdown": redaction_breakdown or {},
            "suppressed": suppressed,
            "note": note,
        }
        _audit.append(entry)
        return entry


def recent_audit(limit: int = _AUDIT_MAX) -> List[dict]:
    """Return up to ``limit`` most-recent audit entries (newest last)."""
    with _audit_lock:
        items = list(_audit)
    if limit <= 0 or limit >= len(items):
        return items
    return items[-limit:]


def clear_audit() -> None:
    """Drop every audit entry. Reserved for tests."""
    global _audit_seq
    with _audit_lock:
        _audit.clear()
        _audit_seq = 0


__all__ = [
    "Mode",
    "get_mode",
    "set_mode",
    "record_llm_call",
    "recent_audit",
    "clear_audit",
]

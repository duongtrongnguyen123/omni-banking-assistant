"""Short-term conversation memory.

Tracks the in-flight draft per user so follow-ups like
"chọn Trần Hoàng Minh" or "xác nhận" can be tied back to the right
transaction.

State is held in a pluggable :class:`SessionBackend`
(see :mod:`session_store`) — in-memory by default, Redis when
``OMNI_SESSION_BACKEND=redis`` is set, ``fakeredis`` for tests.

The :class:`Session` class is a thin per-user facade that decodes /
encodes Pydantic drafts on the way in/out of the backend, so
orchestrator code keeps using ``session.current_draft``,
``session.set_draft(...)``, ``session.append(...)`` exactly as before.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

from ..models.schemas import ContactDraft, ScheduleDraft, TransactionDraft
from .session_store import (
    SessionBackend,
    build_backend,
    history_max_messages,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Draft auto-cancel — an unconfirmed draft is dropped after this many
# seconds of inactivity. The per-field TTL in session_store only fires on
# the in-memory backend (Redis EXPIRE is per-key, not per-field), so we
# enforce it deterministically here with an expiry stamp wrapped around the
# draft payload. Refreshed on every ``set_*`` (each edit resets the clock).
# ---------------------------------------------------------------------------


def _draft_auto_cancel_seconds() -> int:
    raw = os.environ.get("OMNI_DRAFT_AUTO_CANCEL_S", "").strip()
    if not raw:
        return 60
    try:
        return max(1, int(raw))
    except ValueError:
        return 60


def _wrap_expiry(model) -> str:
    """JSON-encode a draft with an absolute expiry timestamp."""
    return json.dumps(
        {
            "__exp": time.time() + _draft_auto_cancel_seconds(),
            "d": json.loads(model.model_dump_json()),
        },
        ensure_ascii=False,
    )


def _unwrap_expiry(raw: Optional[str]) -> tuple[Optional[str], bool]:
    """Return (inner-draft-json, expired?). Back-compatible with a plain
    (un-enveloped) draft payload, which is treated as never-expiring."""
    if not raw:
        return None, False
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return raw, False
    if isinstance(obj, dict) and "__exp" in obj:
        if obj["__exp"] < time.time():
            return None, True
        return json.dumps(obj.get("d"), ensure_ascii=False), False
    return raw, False


# ---------------------------------------------------------------------------
# Backend bootstrap (process-global, lazy)
# ---------------------------------------------------------------------------

_backend_lock = threading.Lock()
_backend: Optional[SessionBackend] = None


def get_backend() -> SessionBackend:
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                _backend = build_backend()
                logger.info("Session backend = %s", _backend.name)
    return _backend


def set_backend(backend: SessionBackend) -> None:
    """Override the process-wide backend (used by tests)."""
    global _backend
    with _backend_lock:
        _backend = backend


def reset_backend() -> None:
    """Drop the cached backend; next access re-reads env."""
    global _backend
    with _backend_lock:
        _backend = None


# ---------------------------------------------------------------------------
# Per-user facade
# ---------------------------------------------------------------------------


class Session:
    """Per-user view over the configured session backend.

    Exposes the same attributes / methods the orchestrator used on
    the old :class:`ConversationMemory`:

    * ``current_draft``  / ``set_draft`` / ``clear_draft``
    * ``current_contact_draft`` / ``set_contact_draft`` / ``clear_contact_draft``
    * ``current_schedule_draft`` / ``set_schedule_draft`` / ``clear_schedule_draft``
    * ``has_any_draft``
    * ``append(role, content)`` and ``conversation_messages(...)``
    """

    def __init__(self, user_id: str, backend: Optional[SessionBackend] = None) -> None:
        self.user_id = user_id
        self._backend = backend or get_backend()

    @property
    def backend(self) -> SessionBackend:
        return self._backend

    # ------------------------------------------------------------------
    # Transaction draft
    # ------------------------------------------------------------------

    @property
    def current_draft(self) -> Optional[TransactionDraft]:
        raw = self._backend.get_draft(self.user_id)
        inner, expired = _unwrap_expiry(raw)
        if expired:
            self._backend.clear_draft(self.user_id)  # auto-cancel after timeout
            return None
        return _decode(inner, TransactionDraft)

    def set_draft(self, draft: TransactionDraft) -> None:
        self._backend.set_draft(self.user_id, _wrap_expiry(draft))

    def clear_draft(self) -> None:
        self._backend.clear_draft(self.user_id)

    # ------------------------------------------------------------------
    # Contact draft
    # ------------------------------------------------------------------

    @property
    def current_contact_draft(self) -> Optional[ContactDraft]:
        raw = self._backend.get_contact_draft(self.user_id)
        inner, expired = _unwrap_expiry(raw)
        if expired:
            self._backend.clear_contact_draft(self.user_id)
            return None
        return _decode(inner, ContactDraft)

    def set_contact_draft(self, draft: ContactDraft) -> None:
        self._backend.set_contact_draft(self.user_id, _wrap_expiry(draft))

    def clear_contact_draft(self) -> None:
        self._backend.clear_contact_draft(self.user_id)

    # ------------------------------------------------------------------
    # Schedule draft
    # ------------------------------------------------------------------

    @property
    def current_schedule_draft(self) -> Optional[ScheduleDraft]:
        raw = self._backend.get_schedule_draft(self.user_id)
        inner, expired = _unwrap_expiry(raw)
        if expired:
            self._backend.clear_schedule_draft(self.user_id)
            return None
        return _decode(inner, ScheduleDraft)

    def set_schedule_draft(self, draft: ScheduleDraft) -> None:
        self._backend.set_schedule_draft(self.user_id, _wrap_expiry(draft))

    def clear_schedule_draft(self) -> None:
        self._backend.clear_schedule_draft(self.user_id)

    # ------------------------------------------------------------------
    # Aggregate predicate
    # ------------------------------------------------------------------

    def has_any_draft(self) -> bool:
        return (
            self.current_draft is not None
            or self.current_contact_draft is not None
            or self.current_schedule_draft is not None
        )

    # ------------------------------------------------------------------
    # Conversation history
    # ------------------------------------------------------------------

    @property
    def history(self) -> list[dict]:
        return self._backend.get_history(self.user_id)

    def append(self, role: str, content: str) -> None:
        """Append a message to the per-user history (with auto-truncate)."""
        self._backend.append_message(self.user_id, role, content)

    # Alias to match the deliverable's spec name.
    append_message = append

    def clear_history(self) -> None:
        """Wipe the user's conversation history at the backend level.

        Audit fix: callers previously did ``session.history.clear()`` to
        reset history, but the ``history`` property returns a *fresh
        list copy* from the backend (see ``InMemorySessionStore.get_history``:
        ``return list(raw)``), so mutating it had no effect on stored
        state. This helper writes an empty list through ``set_history``
        so both the in-memory and Redis backends actually drop the
        messages."""
        self._backend.set_history(self.user_id, [])

    def set_conversation_history(self, history: list[dict]) -> None:
        """Replace history wholesale (used by tests / admin)."""
        max_n = history_max_messages()
        if len(history) > max_n:
            history = history[-max_n:]
        self._backend.set_history(self.user_id, history)

    def conversation_messages(self, max_turns: int = 8) -> list[dict]:
        """Recent turns as OpenAI-compatible chat messages.

        Used as conversational context for both NLU (so the model
        can resolve references like "còn tháng trước?") and response
        phrasing.
        """
        role_map = {"user": "user", "omni": "assistant"}
        return [
            {"role": role_map.get(h.get("role", "user"), "user"), "content": h.get("content", "")}
            for h in self.history[-max_turns:]
        ]


# ---------------------------------------------------------------------------
# Pydantic JSON helpers
# ---------------------------------------------------------------------------


def _encode(model) -> str:
    return model.model_dump_json()


def _decode(raw: Optional[str], cls):
    if not raw:
        return None
    try:
        return cls.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not decode %s payload (%s) — discarding draft.",
            cls.__name__,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Backwards-compatible name + entry point
# ---------------------------------------------------------------------------


# Older code (and a few tests) imported ConversationMemory directly. We
# keep the alias so existing imports don't break — Session is the
# preferred name going forward.
ConversationMemory = Session


_seen_user_ids: set[str] = set()
_seen_lock = threading.Lock()


def session_for(user_id: str) -> Session:
    # Track first-time-seen user IDs so the ``omni_session_active`` gauge
    # reflects unique sessions in this process. Best-effort — a metric
    # import failure must not break the chat path.
    try:
        with _seen_lock:
            is_new = user_id not in _seen_user_ids
            if is_new:
                _seen_user_ids.add(user_id)
        if is_new:
            from ..services import metrics as _m

            _m.session_active.set(len(_seen_user_ids))
    except Exception:
        pass
    return Session(user_id)


def _reset_session_metrics_for_tests() -> None:
    """Test hook — clear the seen-user set so per-test counts start fresh."""
    with _seen_lock:
        _seen_user_ids.clear()
    try:
        from ..services import metrics as _m

        _m.session_active.set(0)
    except Exception:
        pass

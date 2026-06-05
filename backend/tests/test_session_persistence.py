"""Persistence + fault-tolerance tests for the session backends.

We deliberately exercise the *Redis* code path via :class:`FakeRedisSessionStore`
so the wire-level behaviour (HSET / HGET / EXPIRE, JSON round-trips,
TTL semantics) is covered without docker.

What we prove here:

1. A draft written by one ``Session`` is readable by a freshly
   constructed ``Session`` against the same backend — i.e. drafts
   survive a "process restart" as long as the backend persists.
2. Drafts honour their configured TTL: setting ``OMNI_DRAFT_TTL_S``
   to a low value and advancing the fakeredis clock causes the draft
   to disappear.
3. If the user picks ``OMNI_SESSION_BACKEND=redis`` but Redis is
   actually unreachable, the bootstrap silently falls back to
   :class:`InMemorySessionStore` (the demo must not crash).
4. Conversation history is bounded — appending more than the
   configured cap keeps only the most recent ``OMNI_HISTORY_MAX``
   messages.
"""

from __future__ import annotations

import importlib
import os
from datetime import datetime

import pytest

from app.context import session as session_module
from app.context.session import Session, reset_backend, set_backend
from app.context.session_store import (
    FakeRedisSessionStore,
    InMemorySessionStore,
    RedisSessionStore,
    build_backend,
    history_max_messages,
)
from app.models.schemas import Contact, ScheduleDraft, TransactionDraft


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_draft(draft_id: str = "d1") -> TransactionDraft:
    return TransactionDraft(
        id=draft_id,
        recipient=None,
        source_text="chuyển mẹ 5 triệu",
        amount=5_000_000,
        description="Quà tháng",
        awaiting_otp=True,
    )


def _sample_contact() -> Contact:
    return Contact(
        id="c1",
        owner_id="u_an",
        display_name="Nguyễn Thị Lan",
        bank="VCB",
        account_number="0123456789",
        account_masked="***6789",
        aliases=["mẹ"],
    )


def _sample_schedule_draft() -> ScheduleDraft:
    return ScheduleDraft(
        id="s1",
        recipient=_sample_contact(),
        amount=2_000_000,
        cron="0 9 1 * *",
        cron_label="mùng 1 hàng tháng",
        next_run=datetime(2026, 7, 1, 9, 0, 0),
    )


@pytest.fixture(autouse=True)
def _isolate_backend_singleton():
    """Reset the module-level backend cache around every test."""
    reset_backend()
    yield
    reset_backend()


# ---------------------------------------------------------------------------
# 1. Draft survives a "process restart"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend_factory",
    [InMemorySessionStore, FakeRedisSessionStore],
    ids=["memory", "fake-redis"],
)
def test_draft_survives_new_session_object(backend_factory):
    """Two ``Session`` instances pointing at the same backend see the
    same draft — that's the core promise of "persist past restart"."""
    backend = backend_factory()

    s1 = Session("u_an", backend=backend)
    s1.set_draft(_sample_draft())

    # Simulate a fresh request handler / new uvicorn worker.
    s2 = Session("u_an", backend=backend)
    assert s2.current_draft is not None
    assert s2.current_draft.id == "d1"
    assert s2.current_draft.awaiting_otp is True
    assert s2.current_draft.amount == 5_000_000


def test_schedule_and_contact_drafts_round_trip_through_fake_redis():
    backend = FakeRedisSessionStore()
    s = Session("u_an", backend=backend)

    s.set_schedule_draft(_sample_schedule_draft())
    assert s.current_schedule_draft is not None
    assert s.current_schedule_draft.cron == "0 9 1 * *"
    assert s.current_schedule_draft.recipient.display_name == "Nguyễn Thị Lan"

    s.clear_schedule_draft()
    assert s.current_schedule_draft is None


def test_has_any_draft_reflects_backend_state():
    backend = FakeRedisSessionStore()
    s = Session("u_an", backend=backend)
    assert s.has_any_draft() is False

    s.set_draft(_sample_draft())
    assert s.has_any_draft() is True

    s.clear_draft()
    assert s.has_any_draft() is False


# ---------------------------------------------------------------------------
# 2. TTL semantics
# ---------------------------------------------------------------------------


def test_draft_expires_after_ttl(monkeypatch):
    """Set a 1-second draft TTL, advance fakeredis time past it, draft
    must be gone."""
    monkeypatch.setenv("OMNI_DRAFT_TTL_S", "1")
    monkeypatch.setenv("OMNI_SESSION_TTL_S", "1")

    backend = FakeRedisSessionStore()
    s = Session("u_an", backend=backend)
    s.set_draft(_sample_draft())
    assert s.current_draft is not None

    # fakeredis uses real wall-clock for EXPIRE; advance it explicitly.
    import time as _time

    _time.sleep(1.2)

    assert s.current_draft is None, "Draft should have expired"


def test_inmemory_draft_expires_after_ttl(monkeypatch):
    monkeypatch.setenv("OMNI_DRAFT_TTL_S", "1")

    backend = InMemorySessionStore()
    s = Session("u_an", backend=backend)
    s.set_draft(_sample_draft())
    assert s.current_draft is not None

    import time as _time

    _time.sleep(1.1)

    assert s.current_draft is None


# ---------------------------------------------------------------------------
# 3. Fallback when Redis is unreachable
# ---------------------------------------------------------------------------


def test_build_backend_falls_back_when_redis_url_invalid(monkeypatch):
    """An unroutable Redis URL must not crash bootstrap — we degrade
    to in-memory and log a warning."""
    monkeypatch.setenv("OMNI_SESSION_BACKEND", "redis")
    # 192.0.2.0/24 is reserved for documentation — guaranteed
    # unroutable. The 1s socket timeout keeps the test snappy.
    monkeypatch.setenv("OMNI_REDIS_URL", "redis://192.0.2.1:6379/0")

    backend = build_backend()
    assert isinstance(backend, InMemorySessionStore)


def test_redis_session_store_demotes_to_memory_on_write_failure():
    """Simulate Redis dying mid-flow. The backend must keep responding
    (no-op writes) so the orchestrator can finish the request."""

    class _DyingClient:
        def __init__(self):
            self.calls = 0

        def hset(self, *args, **kwargs):
            self.calls += 1
            raise ConnectionError("server gone")

        def expire(self, *args, **kwargs):
            return True

        def hget(self, *args, **kwargs):
            raise ConnectionError("server gone")

        def hdel(self, *args, **kwargs):
            return 0

        def close(self):
            pass

    from app.context.session_store import _RedisBackedStore  # noqa: WPS437

    store = _RedisBackedStore(_DyingClient())
    assert store.healthy() is True

    # First write fails — demote.
    store.set_draft("u_an", _sample_draft().model_dump_json())
    assert store.healthy() is False, "Backend should have demoted itself"

    # Subsequent reads return None rather than raising.
    assert store.get_draft("u_an") is None


def test_unknown_backend_name_falls_back_to_memory(monkeypatch):
    monkeypatch.setenv("OMNI_SESSION_BACKEND", "potato")
    assert isinstance(build_backend(), InMemorySessionStore)


# ---------------------------------------------------------------------------
# 4. Conversation history append + truncation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend_factory",
    [InMemorySessionStore, FakeRedisSessionStore],
    ids=["memory", "fake-redis"],
)
def test_history_appends_and_truncates(backend_factory, monkeypatch):
    monkeypatch.setenv("OMNI_HISTORY_MAX", "20")
    # Bust the module's cached constant by re-importing — the helpers
    # already read os.environ each call, so this is belt-and-braces.
    importlib.reload(session_module)
    set_backend(backend_factory())

    s = session_module.Session("u_an")
    for i in range(30):
        s.append("user" if i % 2 == 0 else "omni", f"msg-{i}")

    history = s.history
    assert len(history) == 20
    # Most recent messages retained.
    assert history[-1]["content"] == "msg-29"
    assert history[0]["content"] == "msg-10"


def test_conversation_messages_maps_roles_for_llm():
    backend = FakeRedisSessionStore()
    s = Session("u_an", backend=backend)
    s.append("user", "Chuyển mẹ 5 triệu")
    s.append("omni", "Xác nhận chuyển 5.000.000đ cho mẹ?")

    msgs = s.conversation_messages(max_turns=4)
    assert msgs == [
        {"role": "user", "content": "Chuyển mẹ 5 triệu"},
        {"role": "assistant", "content": "Xác nhận chuyển 5.000.000đ cho mẹ?"},
    ]


def test_history_entries_carry_timestamps():
    backend = FakeRedisSessionStore()
    s = Session("u_an", backend=backend)
    s.append("user", "ping")
    h = s.history
    assert len(h) == 1
    assert "ts" in h[0]
    assert isinstance(h[0]["ts"], (int, float))


# ---------------------------------------------------------------------------
# Bonus: real Redis backend construction is gated behind redis-py being
# importable. We skip if not.
# ---------------------------------------------------------------------------


def test_real_redis_constructor_pings_on_init(monkeypatch):
    """The real ``RedisSessionStore`` should attempt a ping during
    ``__init__``; if the server is unreachable that raises and the
    bootstrap logic (tested above) handles it."""
    redis = pytest.importorskip("redis")

    # Point at a definitely-dead URL with a fast timeout.
    monkeypatch.setenv("OMNI_REDIS_URL", "redis://192.0.2.1:6379/0")

    with pytest.raises(Exception):
        RedisSessionStore()

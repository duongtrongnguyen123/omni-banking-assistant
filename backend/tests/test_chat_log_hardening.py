"""Regression tests for chat-log audit fixes.

Covers:

* **A (atomic append).** The INSERT into ``chat_messages`` and the
  UPDATE on ``chat_sessions.updated_at`` must land as a single
  transaction. We force the UPDATE to fail and verify the INSERT
  was rolled back too — neither side leaks.
* **B (ownership defence-in-depth).** Calling ``append_message`` with
  a session_id that doesn't belong to the caller must raise
  ``PermissionError`` (rather than silently appending into user B's
  conversation). Confirms the gate is enforced at the data layer,
  not just in the route.
* **C (user message archived on 500).** When ``handle_message``
  raises, the route still archives the user's typed message so the
  sidebar shows what they sent. The omni reply is naturally absent
  because the orchestrator never produced one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import chat_log
from app.db.connection import get_connection
from app.main import app
from app.routes._ratelimit import reset as _rate_reset


@pytest.fixture
def client():
    _rate_reset()
    return TestClient(app)


# ---------------------------------------------------------------------------
# Bug A — atomicity
# ---------------------------------------------------------------------------


def test_append_message_is_atomic_on_failure(monkeypatch):
    """If the second statement of ``append_message`` fails, the first
    must be rolled back too — no orphan ``chat_messages`` row."""
    user = "atomic_test_user"
    sid = chat_log.create_session(user)["id"]

    conn = get_connection()
    real_execute = conn.execute
    calls = {"n": 0}

    class _ProxyConn:
        """Wrap the real connection so we can intercept .execute without
        mutating the read-only attribute on ``sqlite3.Connection``."""

        def execute(self, sql, *args, **kwargs):
            if sql.strip().upper().startswith("UPDATE CHAT_SESSIONS"):
                calls["n"] += 1
                raise RuntimeError("simulated mid-transaction crash")
            return real_execute(sql, *args, **kwargs)

    proxy = _ProxyConn()
    monkeypatch.setattr("app.db.chat_log.get_connection", lambda: proxy)
    with pytest.raises(RuntimeError, match="simulated"):
        chat_log.append_message(sid, user, "user", "hello")
    monkeypatch.undo()

    # No partially-inserted message row should exist for this session.
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM chat_messages WHERE session_id = ?",
        (sid,),
    ).fetchone()
    assert rows["n"] == 0, "INSERT must roll back when the UPDATE fails"
    assert calls["n"] == 1, "the flaky UPDATE was reached exactly once"


def test_append_message_happy_path_writes_both_sides():
    """Sanity check: with the new transaction wrapper, normal calls
    still produce a message row AND bump ``updated_at``."""
    user = "atomic_happy_user"
    row = chat_log.create_session(user)
    sid = row["id"]
    initial_updated = row["updated_at"]
    chat_log.append_message(sid, user, "user", "ping")
    conn = get_connection()
    msg = conn.execute(
        "SELECT content FROM chat_messages WHERE session_id = ?", (sid,)
    ).fetchone()
    assert msg["content"] == "ping"
    sess = conn.execute(
        "SELECT updated_at, title FROM chat_sessions WHERE id = ?", (sid,)
    ).fetchone()
    assert sess["updated_at"] >= initial_updated
    assert sess["title"] == "ping"


# ---------------------------------------------------------------------------
# Bug B — ownership defence-in-depth
# ---------------------------------------------------------------------------


def test_append_message_rejects_foreign_session():
    """User A must not be able to append into user B's session even if
    a future caller forgets to gate through ``resolve_session``."""
    user_a = "owner_user_a"
    user_b = "intruder_user_b"
    sid_a = chat_log.create_session(user_a)["id"]

    with pytest.raises(PermissionError):
        chat_log.append_message(sid_a, user_b, "user", "should never land")

    conn = get_connection()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM chat_messages "
        "WHERE session_id = ? AND user_id = ?",
        (sid_a, user_b),
    ).fetchone()["n"]
    assert n == 0, "no row may be inserted on the foreign-ownership path"


def test_append_message_rejects_unknown_session():
    """Unknown session_id is treated the same as a foreign one — refuse,
    don't silently invent."""
    with pytest.raises(PermissionError):
        chat_log.append_message(
            "this-session-id-does-not-exist", "any_user", "user", "x"
        )


# ---------------------------------------------------------------------------
# Bug C — 500 path preserves the user's typed message
# ---------------------------------------------------------------------------


def test_user_message_archived_even_when_handle_message_raises(client, monkeypatch):
    """If the orchestrator blows up, the user's typed text must still
    land in the conversation archive so the sidebar shows what they
    sent. Otherwise the user loses both the reply AND their own input.

    Uses a dedicated ``x-user-id`` so the route's per-user
    ``_LAST_SESSION_BY_USER`` cache (and any draft state) doesn't bleed
    into other tests sharing the default demo user.
    """
    user_header = {"x-user-id": "bugc_archive_user"}

    def boom(user_id, message):
        raise RuntimeError("orchestrator boom")

    monkeypatch.setattr("app.routes.chat.handle_message", boom)

    # Create the session up front so we can inspect it after the 500.
    sid = client.post("/api/chat/sessions", headers=user_header).json()["id"]

    # The 500 is expected. TestClient surfaces it as raise_app_exceptions
    # by default; we explicitly disable that so we can assert status.
    with TestClient(app, raise_server_exceptions=False) as raw:
        r = raw.post(
            "/api/chat",
            json={"message": "câu mất tích nếu bug C", "session_id": sid},
            headers=user_header,
        )
    assert r.status_code == 500

    # The user message must still be archived.
    body = client.get(
        f"/api/chat/sessions/{sid}", headers=user_header
    ).json()
    user_msgs = [m["content"] for m in body["messages"] if m["role"] == "user"]
    assert "câu mất tích nếu bug C" in user_msgs, (
        "user message must survive a 500 from handle_message — see Bug C"
    )
    # And no omni reply was forged for the failed turn.
    omni_msgs = [m for m in body["messages"] if m["role"] == "omni"]
    assert omni_msgs == [], "no omni reply should be archived on the failure path"

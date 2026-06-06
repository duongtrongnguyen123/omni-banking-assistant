"""Tests for durable chat history (the left-hand conversations sidebar).

Covers the contract the frontend relies on:

* a turn sent to ``/api/chat`` is persisted and the conversation id
  comes back via the ``X-Chat-Session-Id`` header;
* the conversation is listed, titled from the first user message, and
  re-openable with both sides of the turn in order;
* delete removes it (and its messages via ON DELETE CASCADE).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes._ratelimit import reset as _rate_reset


@pytest.fixture
def client():
    _rate_reset()
    return TestClient(app)


def test_chat_persists_and_returns_session_header(client):
    r = client.post("/api/chat", json={"message": "xin chào"})
    assert r.status_code == 200
    sid = r.headers.get("X-Chat-Session-Id")
    assert sid, "chat response must carry the conversation id header"

    # The conversation is now listed for the demo user.
    sessions = client.get("/api/chat/sessions").json()
    assert any(s["id"] == sid for s in sessions)


def test_first_user_message_becomes_title(client):
    r = client.post("/api/chat", json={"message": "kiểm tra số dư giúp mình"})
    sid = r.headers["X-Chat-Session-Id"]
    row = next(s for s in client.get("/api/chat/sessions").json() if s["id"] == sid)
    assert "kiểm tra số dư" in row["title"]


def test_reopen_session_returns_both_sides_in_order(client):
    sid = client.post("/api/chat", json={"message": "xin chào"}).headers[
        "X-Chat-Session-Id"
    ]
    body = client.get(f"/api/chat/sessions/{sid}").json()
    roles = [m["role"] for m in body["messages"]]
    # user turn first, omni reply second.
    assert roles[:2] == ["user", "omni"]
    assert body["messages"][0]["content"] == "xin chào"


def test_explicit_session_id_groups_turns(client):
    sid = client.post("/api/chat/sessions").json()["id"]
    client.post("/api/chat", json={"message": "câu một", "session_id": sid})
    client.post("/api/chat", json={"message": "câu hai", "session_id": sid})
    msgs = client.get(f"/api/chat/sessions/{sid}").json()["messages"]
    user_msgs = [m["content"] for m in msgs if m["role"] == "user"]
    assert user_msgs == ["câu một", "câu hai"]


def test_delete_session_removes_it(client):
    sid = client.post("/api/chat", json={"message": "xin chào"}).headers[
        "X-Chat-Session-Id"
    ]
    assert client.delete(f"/api/chat/sessions/{sid}").status_code == 200
    assert client.get(f"/api/chat/sessions/{sid}").status_code == 404
    assert all(s["id"] != sid for s in client.get("/api/chat/sessions").json())


def test_get_unknown_session_is_404(client):
    assert client.get("/api/chat/sessions/does-not-exist").status_code == 404

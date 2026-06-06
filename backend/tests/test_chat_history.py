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
    assert body["messages"][1]["response"]["intent"] == body["messages"][1]["intent"]


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


def test_admin_can_review_chat_logs_across_users(client, monkeypatch):
    monkeypatch.delenv("OMNI_ADMIN_TOKEN", raising=False)
    r1 = client.post(
        "/api/chat",
        json={"message": "kiểm tra số dư"},
        headers={"x-user-id": "admin_log_user_a"},
    )
    r2 = client.post(
        "/api/chat",
        json={"message": "chuyển 2 triệu cho Minh"},
        headers={"x-user-id": "admin_log_user_b"},
    )
    sid_a = r1.headers["X-Chat-Session-Id"]
    sid_b = r2.headers["X-Chat-Session-Id"]

    listing = client.get("/api/admin/chat/sessions").json()
    ids = {s["id"] for s in listing["sessions"]}
    assert sid_a in ids
    assert sid_b in ids

    filtered = client.get(
        "/api/admin/chat/sessions?user_id=admin_log_user_a"
    ).json()
    assert all(s["user_id"] == "admin_log_user_a" for s in filtered["sessions"])
    assert any(s["id"] == sid_a for s in filtered["sessions"])
    assert all(s["id"] != sid_b for s in filtered["sessions"])

    detail = client.get(f"/api/admin/chat/sessions/{sid_a}").json()
    assert detail["user_id"] == "admin_log_user_a"
    assert [m["role"] for m in detail["messages"][:2]] == ["user", "omni"]
    assert detail["messages"][1]["response"]["text"] == detail["messages"][1]["content"]


def test_admin_can_review_transaction_ledger(client, monkeypatch):
    monkeypatch.delenv("OMNI_ADMIN_TOKEN", raising=False)
    body = client.get("/api/admin/transactions?user_id=u_an&limit=5").json()
    assert "transactions" in body
    assert body["limit"] == 5
    assert all(tx["user_id"] == "u_an" for tx in body["transactions"])

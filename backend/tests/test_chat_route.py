"""Tests for the pre-pitch hardening on the chat route:

* Empty body → 400 with Vietnamese detail (not 422, not 500).
* Per-user rate limiter trips on burst → 429 with Retry-After.
* Admin auth: open when env unset, gated when env set.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes._ratelimit import reset as _rate_reset


@pytest.fixture
def client():
    _rate_reset()
    return TestClient(app)


def test_chat_empty_body_returns_400(client):
    r = client.post("/api/chat", json={})
    assert r.status_code == 400
    assert "tin nhắn" in r.json()["detail"]


def test_chat_missing_message_field_returns_400(client):
    r = client.post("/api/chat", json={"foo": "bar"})
    assert r.status_code == 400


def test_chat_empty_string_returns_400(client):
    # message="" violates min_length=1 — covered by the same handler.
    r = client.post("/api/chat", json={"message": ""})
    assert r.status_code == 400


def test_chat_valid_message_returns_200(client):
    r = client.post("/api/chat", json={"message": "Chào Omni"})
    assert r.status_code == 200
    body = r.json()
    assert "text" in body
    assert "intent" in body


def test_rate_limiter_trips_on_burst(client, monkeypatch):
    # Cap at 3 req/min so the test is fast.
    monkeypatch.setenv("OMNI_CHAT_RATE_LIMIT", "3")
    _rate_reset()
    headers = {"x-user-id": "rate_test_user"}
    # 3 successes, then 429.
    for _ in range(3):
        r = client.post("/api/chat", json={"message": "Chào"}, headers=headers)
        assert r.status_code == 200, r.text
    r = client.post("/api/chat", json={"message": "Chào"}, headers=headers)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert int(r.headers["Retry-After"]) >= 1
    assert "thử lại" in r.json()["detail"].lower() or "nhanh" in r.json()["detail"].lower()


def test_rate_limiter_disabled_with_zero(client, monkeypatch):
    monkeypatch.setenv("OMNI_CHAT_RATE_LIMIT", "0")
    _rate_reset()
    headers = {"x-user-id": "rate_zero_user"}
    # Plenty of requests — none should 429.
    for _ in range(20):
        r = client.post("/api/chat", json={"message": "Chào"}, headers=headers)
        assert r.status_code == 200


def test_admin_open_when_token_unset(client, monkeypatch):
    monkeypatch.delenv("OMNI_ADMIN_TOKEN", raising=False)
    r = client.get("/api/admin/privacy-mode")
    assert r.status_code == 200


def test_admin_requires_token_when_set(client, monkeypatch):
    monkeypatch.setenv("OMNI_ADMIN_TOKEN", "s3cret-token")
    # Missing header.
    r = client.get("/api/admin/privacy-mode")
    assert r.status_code == 401
    assert "Authorization" in r.json()["detail"] or "Bearer" in r.json()["detail"]
    # Wrong token.
    r = client.get(
        "/api/admin/privacy-mode",
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401
    # Right token.
    r = client.get(
        "/api/admin/privacy-mode",
        headers={"Authorization": "Bearer s3cret-token"},
    )
    assert r.status_code == 200
    # Restore.
    monkeypatch.delenv("OMNI_ADMIN_TOKEN", raising=False)


def test_admin_token_is_constant_time_length_check(client, monkeypatch):
    """Different-length tokens must always reject regardless of prefix match."""
    monkeypatch.setenv("OMNI_ADMIN_TOKEN", "abcdef")
    r = client.get(
        "/api/admin/privacy-mode",
        headers={"Authorization": "Bearer abcde"},  # prefix match, wrong length
    )
    assert r.status_code == 401

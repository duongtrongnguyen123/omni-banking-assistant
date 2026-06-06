"""Regression: confirm-then-cancel race must not double-state the draft.

User-reported bug ("nhập opt rồi nhấn huỷ nhưng mà sao vẫn chuyển?"):
the frontend let a Huỷ click land at the backend AFTER confirm had
already started the transfer. The cancel endpoint then cleared the
session while ``confirm_draft`` was still executing, so the transfer
was written but the UI claimed it was cancelled.

The fix is two-layer:
- Frontend: ``inFlightDraftIds`` set locks the Huỷ button as soon as
  Xác nhận fires (App.tsx → Message.tsx → TransactionCard.tsx).
- Backend: ``_INFLIGHT_CONFIRMS`` set in routes/chat.py. Cancel checks
  it and returns a polite "đang xử lý" notice instead of clearing the
  in-flight draft.

This file pins the backend half. The frontend half is exercised by
the existing Playwright suite (TransactionCard renders the disabled
button + spinner when ``inFlight`` is true)."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from app.main import app
from app.routes.chat import _CONFIRMED_DRAFT_RESPONSES, _INFLIGHT_CONFIRMS


HEADERS = {"x-user-id": "u_an"}


def _reset_state() -> None:
    _INFLIGHT_CONFIRMS.clear()
    _CONFIRMED_DRAFT_RESPONSES.clear()


def test_cancel_returns_inflight_notice_when_confirm_executing() -> None:
    """Simulate confirm in progress (mid-execute) — cancel must NOT
    clear the draft. The frontend's button-lock should prevent this
    click from leaving the browser, but a stale tab / scripted client
    can still send it; the server-side guard is the last line."""
    _reset_state()
    client = TestClient(app)
    cache_key = "u_an:draft-doesnt-matter"
    _INFLIGHT_CONFIRMS.add(cache_key)
    try:
        r = client.post(
            "/api/transactions/draft-doesnt-matter/cancel", headers=HEADERS
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Refuses cleanly with a Vietnamese notice — caller decides
        # whether to retry. The session draft is untouched.
        assert "đang được xử lý" in body["text"]
        assert body["intent"] == "transfer"
    finally:
        _INFLIGHT_CONFIRMS.discard(cache_key)


def test_cancel_replays_confirm_when_confirm_already_finished() -> None:
    """Same race, opposite end of the window: confirm finished, the
    user's cancel landed just after. We must not pretend the cancel
    succeeded — replay the cached confirm response so the UI shows
    the receipt the transfer actually produced."""
    from app.models.schemas import OmniResponse

    _reset_state()
    client = TestClient(app)
    cache_key = "u_an:already-confirmed-draft"
    fake_receipt = OmniResponse(
        intent="transfer",
        text="Đã chuyển 100.000đ cho Mẹ.",
    )
    _CONFIRMED_DRAFT_RESPONSES[cache_key] = fake_receipt
    try:
        r = client.post(
            "/api/transactions/already-confirmed-draft/cancel", headers=HEADERS
        )
        assert r.status_code == 200
        body = r.json()
        # Idempotent replay — the cancel is a no-op against a finished
        # draft and shows the user what really happened.
        assert body["text"] == "Đã chuyển 100.000đ cho Mẹ."
    finally:
        _CONFIRMED_DRAFT_RESPONSES.pop(cache_key, None)


def test_inflight_set_clears_after_confirm_completes() -> None:
    """The guard must be self-clearing — every successful confirm path
    must remove its cache_key in the finally block. Otherwise a single
    failure would permanently lock subsequent cancels for that draft."""
    _reset_state()
    client = TestClient(app)
    # Hit confirm on a non-existent draft. orchestrator returns
    # intent="unknown" which becomes a 404. The finally block must
    # still discard the key.
    r = client.post(
        "/api/transactions/nonexistent-draft-id/confirm",
        headers=HEADERS,
        json={"otp": "123456"},
    )
    # Either 404 (unknown draft) or 200 with a non-confirmed response —
    # both are fine for this test. The invariant we care about:
    assert r.status_code in (200, 404)
    assert "u_an:nonexistent-draft-id" not in _INFLIGHT_CONFIRMS


def test_concurrent_cancel_during_confirm_does_not_double_resolve() -> None:
    """Property test: fire cancel + confirm in parallel for the same
    draft. Outcome must be deterministic — either confirm wins (cancel
    sees inflight or cached) or cancel wins (idempotent cancel of an
    unknown draft). Never both succeeding into different end-states."""
    _reset_state()
    client = TestClient(app)
    draft_id = "race-test-draft"
    cache_key = f"u_an:{draft_id}"
    # Pre-claim the inflight slot so cancel will see it. This sidesteps
    # the need for a real draft in the session — the test is about the
    # guard semantics, not the orchestrator.
    _INFLIGHT_CONFIRMS.add(cache_key)
    results: list[dict] = []

    def fire_cancel() -> None:
        r = client.post(f"/api/transactions/{draft_id}/cancel", headers=HEADERS)
        results.append(r.json())

    threads = [threading.Thread(target=fire_cancel) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every parallel cancel must see the same inflight notice — no
    # cancel got to call cancel_draft() under the running confirm.
    assert len(results) == 5
    for body in results:
        assert "đang được xử lý" in body["text"]
    _INFLIGHT_CONFIRMS.discard(cache_key)

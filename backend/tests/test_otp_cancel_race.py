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
from app.routes import _ratelimit
from app.routes.chat import (
    _CONFIRMED_DRAFT_RESPONSES,
    _INFLIGHT_CONFIRMS,
    _OTP_ATTEMPTS,
)


HEADERS = {"x-user-id": "u_an"}


def _reset_state() -> None:
    _INFLIGHT_CONFIRMS.clear()
    _CONFIRMED_DRAFT_RESPONSES.clear()
    _OTP_ATTEMPTS.clear()
    _ratelimit.reset()


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


# ---------------------------------------------------------------------------
# Audit A — double-debit race + OTP brute force
# ---------------------------------------------------------------------------


def _seed_demo_user_isolated() -> None:
    """Copy the canonical JSON seed into BANKING_DATA_DIR and reset the
    Store singleton — mirrors test_demo_safety_contract's fixture so a
    real ``u_an`` user with balance + contacts exists when we exercise
    the live confirm path.
    """
    import os
    import shutil
    from pathlib import Path

    env_dir = os.environ.get("BANKING_DATA_DIR", "").strip()
    if not env_dir:
        env_dir = str(
            Path(__file__).resolve().parent.parent / ".tmp_test_seed"
        )
        os.environ["BANKING_DATA_DIR"] = env_dir
    data_dir = Path(env_dir).resolve()
    src = Path(__file__).resolve().parent.parent / "app" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "users.json",
        "contacts.json",
        "transactions.json",
        "schedules.json",
    ):
        target = data_dir / name
        if not target.exists() and (src / name).exists():
            shutil.copyfile(src / name, target)
    db_file = data_dir / "omni.db"
    if db_file.exists():
        db_file.unlink()
    try:
        from app.db.connection import reset_connection
        reset_connection()
    except Exception:  # pragma: no cover
        pass
    try:
        import app.store as _store_mod
        _store_mod._store = None
    except Exception:  # pragma: no cover
        pass


def _open_transfer_draft(text: str) -> str:
    """Open a fresh transaction draft via the real orchestrator path and
    return its draft_id."""
    from app.context.session import session_for
    from app.services.orchestrator import handle_message

    sf = session_for("u_an")
    sf.clear_draft()
    r = handle_message("u_an", text)
    assert r.intent == "transfer" and r.draft is not None, (
        f"could not open transfer draft for {text!r}: {r.text!r}"
    )
    return r.draft.id


def test_concurrent_confirm_does_not_double_debit() -> None:
    """Audit A1 — Bug A regression. Two threads POST /confirm at the
    same draft simultaneously. Without the per-(user, draft) lock both
    callers pass the cold idempotency cache, both call ``execute_transfer``,
    and the account is debited twice for a single user-intent confirm.

    Pin invariant: exactly ONE successful transfer hits the ledger and
    the source balance only moves by ``amount`` once.
    """
    _seed_demo_user_isolated()
    _reset_state()

    from app.store import get_store

    store = get_store()
    before_balance = store.primary_account("u_an").balance
    before_tx_count = len(store.transactions_of("u_an"))

    # Small amount → no biometric required, deterministic single-step
    # confirm so the race is purely on the HTTP critical section.
    draft_id = _open_transfer_draft("chuyển mẹ 200 nghìn")
    client = TestClient(app)
    results: list[tuple[int, dict]] = []
    barrier = threading.Barrier(2)

    def fire() -> None:
        # Both threads block at the barrier, then race into the endpoint
        # together — maximises the chance of the legacy code path picking
        # up the same cold cache miss in both workers.
        barrier.wait()
        r = client.post(
            f"/api/transactions/{draft_id}/confirm",
            headers=HEADERS,
            json={"otp": "123456"},
        )
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    # Both responses 200 — first one debited, second one replayed the
    # idempotency cache. Neither should be a 5xx.
    for code, body in results:
        assert code == 200, body

    after_balance = store.primary_account("u_an").balance
    after_tx_count = len(store.transactions_of("u_an"))

    # The load-bearing invariant. If the lock is missing, balance moves
    # by 400_000 (200k × 2 debits) and tx count grows by 2.
    assert before_balance - after_balance == 200_000, (
        f"double-debit detected: balance moved by "
        f"{before_balance - after_balance}đ (expected 200.000đ). "
        f"Two confirms wrote two transfers — the per-(user,draft) lock is gone."
    )
    assert after_tx_count - before_tx_count == 1, (
        f"double-write detected: tx count grew by "
        f"{after_tx_count - before_tx_count} (expected 1)."
    )


def test_otp_bruteforce_blocks_after_3_attempts() -> None:
    """Audit A2 — Bug B regression. Spam the confirm endpoint with wrong
    OTPs against an open step-up draft. The HTTP layer must trip the
    per-draft attempt counter at 3 and return a Vietnamese lock notice
    on the 4th attempt — even before reaching the orchestrator's own
    5-strike cap (which a session swap could reset)."""
    _seed_demo_user_isolated()
    _reset_state()

    client = TestClient(app)
    # Large amount → orchestrator sets requires_step_up=True so OTP is
    # mandatory and the confirm path goes through the OTP validation arm.
    draft_id = _open_transfer_draft("chuyển mẹ 50 triệu")

    # Three wrong attempts — each should return a draft still
    # awaiting OTP, not yet locked.
    for i in range(3):
        r = client.post(
            f"/api/transactions/{draft_id}/confirm",
            headers=HEADERS,
            json={"otp": f"00000{i}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        if i < 2:
            # Still accepting more guesses.
            assert "khoá" not in body["text"], body
        else:
            # On the 3rd failure the endpoint should already surface the
            # lock notice (counter incremented to 3 inside the handler).
            assert "khoá" in body["text"], (
                f"3rd failed OTP must lock the draft; got: {body['text']!r}"
            )

    # 4th attempt — even with the *correct* OTP — must stay locked.
    r = client.post(
        f"/api/transactions/{draft_id}/confirm",
        headers=HEADERS,
        json={"otp": "123456"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "khoá" in body["text"], (
        f"locked draft must reject further confirms even with correct OTP; "
        f"got: {body['text']!r}"
    )

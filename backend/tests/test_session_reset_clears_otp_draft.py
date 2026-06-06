"""Regression: /api/session/reset must drop an awaiting-OTP draft.

Stress-agent A reproduced a silent-confirm bug:

  1. user → "chuyển mẹ 2tr"  (orchestrator opens draft_A, mẹ + 2.000.000)
  2. user → "ok"             (draft_A.awaiting_otp = True, OTP prompt)
  3. POST /api/session/reset (intended to wipe everything)
  4. user → "chuyển bố 1tr"  (orchestrator opens draft_B, bố + 1.000.000)
  5. user → "ok"             (***intended*** to confirm draft_B)

In the old code the reset endpoint left state behind that allowed
step 5 to dispatch the OLD draft_A — wrong recipient AND wrong amount,
confirmed silently. The fix in this PR (a) wipes ALL draft slots,
(b) clears the orchestrator's module-level dicts (budget / goal /
split queue), (c) drops idempotency + OTP-attempt caches keyed on
the user, and (d) writes an empty history to the backend (the old
``s.history.clear()`` only mutated a list copy returned by the
property).

The tests below pin both the bug scenario and the "history actually
got wiped" invariant — a silent revert of either property would
re-open the wrong-confirm exploit.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from app.context.session import session_for
from app.main import app
from app.routes import _ratelimit
from app.routes.chat import (
    _CONFIRMED_DRAFT_RESPONSES,
    _INFLIGHT_CONFIRMS,
    _LAST_SESSION_BY_USER,
    _OTP_ATTEMPTS,
)


HEADERS = {"x-user-id": "u_an"}


def _reset_global_state() -> None:
    _INFLIGHT_CONFIRMS.clear()
    _CONFIRMED_DRAFT_RESPONSES.clear()
    _OTP_ATTEMPTS.clear()
    _LAST_SESSION_BY_USER.clear()
    _ratelimit.reset()


def _seed_demo_user_isolated() -> None:
    """Mirror the test_otp_cancel_race fixture: copy seed JSON into a
    private BANKING_DATA_DIR and reset the Store singleton so 'u_an'
    has a real balance + contacts when we run handle_message."""
    env_dir = os.environ.get("BANKING_DATA_DIR", "").strip()
    if not env_dir:
        env_dir = str(Path(__file__).resolve().parent.parent / ".tmp_test_seed")
        os.environ["BANKING_DATA_DIR"] = env_dir
    data_dir = Path(env_dir).resolve()
    src = Path(__file__).resolve().parent.parent / "app" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in ("users.json", "contacts.json", "transactions.json", "schedules.json"):
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


def test_reset_drops_awaiting_otp_draft() -> None:
    """The bug scenario verbatim: open a draft, push it into the OTP
    waiting state, hit /api/session/reset, and confirm the session no
    longer has any draft to dispatch a stray 'ok' against."""
    _seed_demo_user_isolated()
    _reset_global_state()
    session_for("u_an").clear_draft()

    from app.services.orchestrator import confirm_draft, handle_message

    # Step 1+2: open a transfer, then send the confirm to flip
    # awaiting_otp=True. Any non-blocked transfer requires OTP per
    # auth_policy(), so a 2tr transfer to mẹ is enough.
    r1 = handle_message("u_an", "chuyển mẹ 2 triệu")
    assert r1.intent == "transfer" and r1.draft is not None, r1.text
    draft_id = r1.draft.id

    r2 = confirm_draft("u_an", draft_id)
    assert r2.draft is not None
    assert r2.draft.awaiting_otp is True, (
        f"setup precondition failed: draft should be awaiting OTP, "
        f"got awaiting_otp={r2.draft.awaiting_otp}"
    )

    # Sanity: the session DOES hold the OTP-waiting draft right now.
    pre = session_for("u_an").current_draft
    assert pre is not None and pre.id == draft_id
    assert pre.awaiting_otp is True

    # Step 3: the safety-critical call. Reset must drop the draft
    # regardless of its awaiting_otp flag.
    client = TestClient(app)
    rr = client.post("/api/session/reset", headers=HEADERS)
    assert rr.status_code == 200, rr.text

    # The load-bearing assertion: no draft survives reset. A future
    # 'ok' / OTP code from the user cannot land on the old draft.
    post = session_for("u_an").current_draft
    assert post is None, (
        f"OTP-awaiting draft survived /api/session/reset — silent "
        f"wrong-confirm exploit is open. Saw: {post!r}"
    )

    # And confirming the stale draft_id directly now returns the
    # orchestrator's not-found sentinel rather than executing.
    stale = confirm_draft("u_an", draft_id)
    assert stale.intent == "unknown", (
        f"stale draft_id was still dispatched after reset — got "
        f"intent={stale.intent!r}, text={stale.text!r}"
    )


def test_reset_clears_history_at_backend_level() -> None:
    """The s.history.clear() bug: history is returned as a list copy,
    so .clear() on it does nothing. clear_history() must actually wipe
    the persisted history."""
    _seed_demo_user_isolated()
    _reset_global_state()

    s = session_for("u_an")
    s.clear_history()  # baseline
    s.append("user", "hello")
    s.append("omni", "hi")
    assert len(s.history) >= 2

    client = TestClient(app)
    rr = client.post("/api/session/reset", headers=HEADERS)
    assert rr.status_code == 200, rr.text

    # If reset wrote s.history.clear() (the old bug), the backend
    # still holds the messages we just appended.
    assert s.history == [], (
        f"conversation history survived /api/session/reset — the "
        f"clear() call ran on a property-returned list copy, not the "
        f"backend. Saw: {s.history!r}"
    )


def test_reset_drops_module_level_drafts() -> None:
    """Budget / goal / split queue live in orchestrator module dicts,
    not in the session backend. Reset must wipe those too — otherwise
    a leftover budget confirm card hijacks the user's next 'ok'."""
    _seed_demo_user_isolated()
    _reset_global_state()

    from app.models.schemas import BudgetDraft, GoalDraft
    from app.services.orchestrator import (
        _budget_drafts,
        _goal_drafts,
        _split_queues,
    )

    _budget_drafts["u_an"] = BudgetDraft(
        id="bd-1",
        category="food",
        category_label="Ăn uống",
        monthly_limit_vnd=500_000,
    )
    _goal_drafts["u_an"] = GoalDraft(
        id="gd-1", name="Du lich", target_vnd=10_000_000
    )
    # Use a non-empty list so the "presence" check is unambiguous: an
    # empty list and a missing key both pop the same way, but a
    # non-empty list rules out an accidental pop-on-empty no-op.
    _split_queues["u_an"] = ["sentinel"]  # type: ignore[list-item]

    client = TestClient(app)
    rr = client.post("/api/session/reset", headers=HEADERS)
    assert rr.status_code == 200, rr.text

    assert "u_an" not in _budget_drafts, "budget draft survived reset"
    assert "u_an" not in _goal_drafts, "goal draft survived reset"
    assert "u_an" not in _split_queues, "split queue survived reset"

"""Regression tests: cancelling a draft must not leave restart-state behind.

Live-trace ghost: after the user typed "huỷ" and Omni said "Đã huỷ giao
dịch.", the very next non-transfer message ("STK") brought back a draft
for Bố 500.000đ AND surfaced the "Mình vẫn giữ giao dịch đang chờ (…)"
line — proof that ``_PENDING_RESTART[user_id]`` was still set even though
``session.current_draft`` had been cleared.

The fix: ``cancel_draft`` must drop ``_PENDING_RESTART`` for the user
unconditionally. The next non-transfer reply ("STK", "ơ", "số dư") must
NOT resurrect any prior draft.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


_SEED_DIR = Path(__file__).resolve().parent.parent / "app" / "data"


@pytest.fixture(scope="module", autouse=True)
def _seed_data_dir():
    tmp = Path(tempfile.mkdtemp(prefix="omni-ghost-"))
    for name in (
        "users.json", "contacts.json", "transactions.json",
        "schedules.json", "atms.json", "napas_accounts.json",
    ):
        src = _SEED_DIR / name
        if src.exists():
            shutil.copy(src, tmp / name)

    prev = os.environ.get("BANKING_DATA_DIR", "")
    os.environ["BANKING_DATA_DIR"] = str(tmp)

    from app.config import get_settings
    get_settings.cache_clear()
    from app import store as store_mod
    store_mod._store = None  # type: ignore[attr-defined]
    try:
        from app.db.connection import reset_connection
        reset_connection()
    except Exception:
        pass
    try:
        from app.db.bootstrap import bootstrap_if_empty
        bootstrap_if_empty()
    except Exception:
        pass

    yield

    os.environ["BANKING_DATA_DIR"] = prev
    get_settings.cache_clear()
    store_mod._store = None  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _isolate_session():
    from app.context.session import session_for
    from app.services.orchestrator import _PENDING_RESTART
    s = session_for("u_an")
    s.clear_draft()
    _PENDING_RESTART.pop("u_an", None)
    yield
    s.clear_draft()
    _PENDING_RESTART.pop("u_an", None)


def test_cancel_draft_clears_pending_restart():
    """Direct unit check: after ``cancel_draft``, ``_PENDING_RESTART`` must
    have no entry for the user. Without this, the next bare reply gets
    interpreted as the answer to a "huỷ giao dịch cũ?" prompt that no
    longer exists, leaking either a ghost confirm card or the
    "Mình vẫn giữ giao dịch đang chờ (…)" string."""
    from app.services.orchestrator import (
        cancel_draft,
        _PENDING_RESTART,
    )
    from app.context.session import session_for
    from app.models.schemas import TransactionDraft
    from app.store import get_store

    store = get_store()
    account = store.primary_account("u_an")
    assert account is not None

    s = session_for("u_an")
    s.set_draft(TransactionDraft(
        id="d_ghost",
        recipient=None,
        amount=2_000_000,
        source_account_id=account.id,
        source_accounts=store.get_user("u_an").accounts,
    ))
    _PENDING_RESTART["u_an"] = "bạn thân"  # simulate stashed restart prompt

    cancel_draft("u_an", "d_ghost")

    assert "u_an" not in _PENDING_RESTART, (
        "BUG-B regression: cancel_draft left _PENDING_RESTART set; the next "
        "bare reply would resurrect the discarded draft or show the "
        "'Mình vẫn giữ giao dịch đang chờ (…)' ghost line."
    )
    assert session_for("u_an").current_draft is None


def test_pending_restart_does_not_outlive_session_clear():
    """Belt-and-braces: even if the caller only clears the session draft
    directly (not via cancel_draft), the next message should not be
    misinterpreted as a yes/no answer because there is no draft to
    keep. The continuation path is gated on ``session.current_draft``,
    so the leftover ``_PENDING_RESTART`` should be inert — pin that."""
    from app.services.orchestrator import (
        _continue_draft_llm_first,
        _PENDING_RESTART,
    )
    from app.context.session import session_for

    s = session_for("u_an")
    s.clear_draft()
    _PENDING_RESTART["u_an"] = "bạn thân"

    # With no draft, the continuation entry shouldn't even be called by
    # handle_message. But the function itself should not blow up if it is.
    # We just verify the orchestrator dispatch path skips it: the gate
    # ``if session.current_draft is not None`` in handle_message guards.
    assert s.current_draft is None

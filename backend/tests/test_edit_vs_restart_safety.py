"""Regression tests for the edit-vs-restart safety boundary.

Live-trace bug: with a pending draft for Bố 2.000.000đ, a bare follow-up
message "bạn thân" was classified as ``action="edit"`` by the LLM. The
edit path preserved the pending 2.000.000đ amount and silently swapped
the recipient to Vũ Quốc Bảo — i.e. Bố's money carried over to a
different person without any user-typed correction cue. That's a money
redirect under the user's nose.

The safety contract these tests pin:

  * A bare recipient surface (no transfer verb, no correction cue) with
    a pending draft must be ``restart``, not ``edit``. The orchestrator
    must convert ``edit`` → restart via ``_should_force_restart_over_edit``
    so the amount can't silently carry.
  * Correction-cued messages ("à thôi bạn thân", "đổi sang bạn thân")
    remain ``edit``: the user explicitly signalled an in-place change.
  * Edits that touch only the amount/account (no recipient change) stay
    ``edit`` — there is no money-redirect risk.

LLM providers are force-disabled (see conftest.py); these tests exercise
the deterministic guardrail directly with a synthesised LLM decision dict.
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
    """Point the store at the real demo seed (Bố / Bạn thân exist there)."""
    tmp = Path(tempfile.mkdtemp(prefix="omni-edit-restart-"))
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


def _bo_draft():
    """A pending draft for Bố with 2.000.000đ — the live-trace shape."""
    from app.models.schemas import TransactionDraft
    from app.store import get_store
    store = get_store()
    contacts = store.contacts_of("u_an")
    bo = next(c for c in contacts if "Lê Văn Hùng" in c.display_name)
    account = store.primary_account("u_an")
    return TransactionDraft(
        id="d_test_bo",
        recipient=bo,
        amount=2_000_000,
        source_account_id=account.id if account else None,
        source_accounts=store.get_user("u_an").accounts,
    )


# ---------------------------------------------------------------------------
# Guardrail: bare recipient swap with no cue → force restart
# ---------------------------------------------------------------------------


def test_bare_alias_swap_no_cue_forces_restart():
    """The live-trace bug. Pending = Bố 2tr. Message = "bạn thân" with no
    correction cue. The LLM emitted ``edit + recipient_text="bạn thân"`` —
    which would have carried 2tr to Bảo. The guardrail must catch this."""
    from app.services.orchestrator import _should_force_restart_over_edit
    draft = _bo_draft()
    decision = {"action": "edit", "recipient_text": "bạn thân"}
    assert _should_force_restart_over_edit(draft, decision, "bạn thân") is True


def test_bare_sep_alias_no_cue_forces_restart():
    """Same shape, different alias. "sếp" as a one-word follow-up to a
    Bố draft is a fresh request, not an edit."""
    from app.services.orchestrator import _should_force_restart_over_edit
    draft = _bo_draft()
    decision = {"action": "edit", "recipient_text": "sếp"}
    assert _should_force_restart_over_edit(draft, decision, "sếp") is True


# ---------------------------------------------------------------------------
# Counter-examples: legitimate edits must still pass through
# ---------------------------------------------------------------------------


def test_correction_cue_preserves_edit_semantics():
    """An explicit cue ("à thôi", "không, X") IS the user's signal that the
    pending amount should carry. The guardrail must NOT convert these."""
    from app.services.orchestrator import _should_force_restart_over_edit
    draft = _bo_draft()
    decision = {"action": "edit", "recipient_text": "bạn thân"}
    # "à thôi" cue — preserve edit (amount carries).
    assert _should_force_restart_over_edit(draft, decision, "à thôi bạn thân") is False
    # "không, X" cue — preserve edit.
    assert _should_force_restart_over_edit(draft, decision, "không, bạn thân") is False
    # "đổi sang" cue — preserve edit.
    assert _should_force_restart_over_edit(draft, decision, "đổi sang bạn thân") is False
    # "khoan" cue.
    assert _should_force_restart_over_edit(draft, decision, "khoan, bạn thân") is False


def test_amount_only_edit_passes_through():
    """Pending = Bố 2tr. User says "3 triệu" — amount-only edit, recipient
    unchanged. No money-redirect risk. Must NOT force restart."""
    from app.services.orchestrator import _should_force_restart_over_edit
    draft = _bo_draft()
    decision = {"action": "edit", "amount_vnd": 3_000_000}
    assert _should_force_restart_over_edit(draft, decision, "3 triệu") is False


def test_relative_amount_op_passes_through():
    """"gấp đôi lên" is a pure amount edit. No recipient change → safe."""
    from app.services.orchestrator import _should_force_restart_over_edit
    draft = _bo_draft()
    decision = {
        "action": "edit",
        "amount_op": "multiply",
        "amount_operand": 2,
    }
    assert _should_force_restart_over_edit(draft, decision, "gấp đôi lên") is False


def test_account_switch_passes_through():
    """"dùng tài khoản phụ" — no recipient/amount change. Safe edit."""
    from app.services.orchestrator import _should_force_restart_over_edit
    draft = _bo_draft()
    decision = {"action": "edit", "account_hint": "phụ"}
    assert _should_force_restart_over_edit(draft, decision, "dùng tài khoản phụ") is False


def test_simultaneous_recipient_and_amount_edit_passes_through():
    """Pending = Bố 2tr. Message = "đổi sang chị Thảo 3 triệu" — explicit
    cue AND amount change. The user is consciously editing both slots.
    The amount in the decision overrides the pending one anyway, so no
    silent-carry risk; honour the edit."""
    from app.services.orchestrator import _should_force_restart_over_edit
    draft = _bo_draft()
    decision = {
        "action": "edit",
        "recipient_text": "chị Thảo",
        "amount_vnd": 3_000_000,
    }
    assert _should_force_restart_over_edit(
        draft, decision, "đổi sang chị Thảo 3 triệu"
    ) is False


def test_transfer_verb_passes_through_to_existing_restart_logic():
    """"chuyển cho bạn thân" has the transfer verb — the existing LLM
    few-shot already classifies this as restart. The guardrail must NOT
    fire for transfer-verb messages (they have their own paths)."""
    from app.services.orchestrator import _should_force_restart_over_edit
    draft = _bo_draft()
    decision = {"action": "edit", "recipient_text": "bạn thân"}
    assert _should_force_restart_over_edit(
        draft, decision, "chuyển cho bạn thân"
    ) is False


def test_same_recipient_restated_is_noop_edit():
    """If the LLM re-emits the SAME recipient (re-stating, not changing),
    there's nothing to redirect. Pass through."""
    from app.services.orchestrator import _should_force_restart_over_edit
    draft = _bo_draft()
    decision = {"action": "edit", "recipient_text": "Lê Văn Hùng"}
    assert _should_force_restart_over_edit(
        draft, decision, "Lê Văn Hùng"
    ) is False

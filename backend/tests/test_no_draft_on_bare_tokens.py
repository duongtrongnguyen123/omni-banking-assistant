"""Regression tests: bare non-transfer tokens must never mint a draft.

The class of bug: a one-word non-transfer reply ("STK", "QR", "ATM", "ơ",
"abc") arriving with no active draft must NOT silently open a transfer
draft — even when the upstream LLM's FOLLOW-UP rule re-emits
``intent=transfer`` with stale ``recipient_text`` / ``temporal_reference``
slots inherited from earlier turns.

The previous ``_msg_has_transfer_signal`` check accepted a leaked
``temporal_reference`` as a transfer signal; ``_handle_transfer`` would
then resolve the inherited recipient and the predictor would fill a
suggested amount, producing a confirm-card-shaped UI for a message that
contained zero transfer cues.

The fix: ``_handle_transfer`` bails to ``intent=unknown`` when the raw
text carries no transfer verb, no typed amount, AND no rule-grounded
recipient surface — regardless of what the LLM inherited.

These tests force the rule-only path (LLM disabled in conftest) and also
exercise the explicit guard against a synthesised LLM-style NLU result
that leaks ``temporal_reference`` for a bare token.
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
    tmp = Path(tempfile.mkdtemp(prefix="omni-bare-tokens-"))
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


def _synth_transfer_nlu(
    raw_text: str,
    recipient_text=None,
    amount=None,
    temporal_reference=None,
):
    """Build the kind of LLM-style NLU result that would leak through —
    intent=transfer with slots that don't appear in raw_text."""
    from app.models.schemas import NLUResult, ExtractedEntities
    return NLUResult(
        intent="transfer",
        confidence=0.9,
        entities=ExtractedEntities(
            recipient_text=recipient_text,
            amount=amount,
            temporal_reference=temporal_reference,
        ),
        raw_text=raw_text,
        source="llm",
    )


def test_bare_stk_token_bails_to_unknown():
    """The live-trace bug. "STK" arriving with a stale LLM-inherited
    recipient ("bố") + temporal reference must NOT mint a draft."""
    from app.services.orchestrator import _handle_transfer
    nlu = _synth_transfer_nlu(
        "STK",
        recipient_text="bố",
        temporal_reference="last month",
    )
    resp = _handle_transfer("u_an", nlu)
    assert resp.draft is None, (
        "BUG-C regression: bare 'STK' minted a transfer draft for the "
        "LLM-inherited recipient 'bố'."
    )
    assert resp.intent == "unknown"


def test_bare_qr_token_bails_to_unknown():
    """QR is the receive-QR keyword in another intent; in the transfer
    path it must NOT mint a draft."""
    from app.services.orchestrator import _handle_transfer
    nlu = _synth_transfer_nlu(
        "QR", recipient_text="mẹ", temporal_reference="hôm trước",
    )
    resp = _handle_transfer("u_an", nlu)
    assert resp.draft is None
    assert resp.intent == "unknown"


def test_bare_atm_token_does_not_mint_draft():
    """Bare "ATM" must not mint a transfer draft. The pre-existing
    ``_msg_has_transfer_signal`` backstop already catches this case by
    bailing with a clarifying question; pin it so a future refactor that
    relaxes either gate is caught."""
    from app.services.orchestrator import _handle_transfer
    nlu = _synth_transfer_nlu("ATM", recipient_text="bố")
    resp = _handle_transfer("u_an", nlu)
    assert resp.draft is None


def test_bare_filler_token_bails_to_unknown():
    """Short filler / hesitation tokens — "ơ", "ờ", "abc" — must not
    open a draft even if the LLM's FOLLOW-UP rule re-emitted transfer
    slots from history."""
    from app.services.orchestrator import _handle_transfer
    for text in ("ơ", "ờ", "abc", "uh"):
        nlu = _synth_transfer_nlu(
            text,
            recipient_text="bố",
            amount=2_000_000,
            temporal_reference="tháng trước",
        )
        resp = _handle_transfer("u_an", nlu)
        assert resp.draft is None, (
            f"BUG-C regression: '{text}' minted a draft via leaked LLM slots."
        )


def test_legitimate_transfer_verb_still_works():
    """Counter-example: "chuyển bố 2tr" must STILL build a draft — the
    tightened gate may not over-fire on genuine transfer commands."""
    from app.services.orchestrator import _handle_transfer
    from app.models.schemas import NLUResult, ExtractedEntities
    nlu = NLUResult(
        intent="transfer",
        confidence=0.9,
        entities=ExtractedEntities(
            recipient_text="bố", amount=2_000_000,
        ),
        raw_text="chuyển bố 2tr",
        source="llm",
    )
    resp = _handle_transfer("u_an", nlu)
    assert resp.draft is not None


def test_temporal_reference_alone_does_not_mint_draft():
    """Pure "tháng trước" with no verb and no recipient surface — even
    when the LLM marks intent=transfer, no draft must open. The user
    might be asking a history question on the next turn."""
    from app.services.orchestrator import _handle_transfer
    nlu = _synth_transfer_nlu(
        "tháng trước",
        recipient_text="bố",
        temporal_reference="tháng trước",
    )
    resp = _handle_transfer("u_an", nlu)
    # No transfer verb, no typed amount, no rule-grounded recipient in
    # "tháng trước" → bail.
    assert resp.draft is None


def test_so_du_does_not_mint_transfer_draft():
    """"số dư" must route through balance, not silently mint a transfer
    via leaked slots. The transfer handler must self-defend even if
    upstream misroutes."""
    from app.services.orchestrator import _handle_transfer
    nlu = _synth_transfer_nlu(
        "số dư", recipient_text="bố", amount=500_000,
    )
    resp = _handle_transfer("u_an", nlu)
    assert resp.draft is None

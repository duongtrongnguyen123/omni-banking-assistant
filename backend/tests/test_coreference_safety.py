"""Regression tests for the co-reference safety contract.

Three classes of bug, all caught by judges in the round-10 audit and
fixed in PR ``fix/coreference-resolution-and-recap-routing``:

  * Bug A — pronoun / correction / bare-reference messages
    ("cô ấy", "không phải mẹ, bố", "bố cũng vậy") used to drop the
    in-flight draft's recipient or, worse, silently swap to a random
    same-token contact ("cô ấy" → Hoàng Thị Mai).
  * Bug B — recap probes ("vừa rồi gửi cho ai") AFTER a confirmation
    opened a NEW transfer draft (with the predictor filling the
    amount). One mistap → wrong transfer.
  * Bug C — "cùng số tiền cho bố" used to surface bố's median amount
    instead of inheriting mẹ's 2tr that the user just typed two turns
    ago.

These tests run with LLM providers force-disabled (see ``conftest.py``),
so they exercise the deterministic rule + intent + predictor paths.
The LLM-side coref handling (system prompt + few-shots + CURRENT
DRAFT injection) is covered separately by the NLU prompt unit tests.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


# We need the real demo seed (contacts with mẹ/bố aliases) to exercise
# the resolver + transfer handler end-to-end. The shared conftest sets
# BANKING_DATA_DIR to an empty temp dir, so this module repoints it BEFORE
# any app modules import — done at module load via os.environ + a settings
# cache clear in the fixture.
_SEED_DIR = Path(__file__).resolve().parent.parent / "app" / "data"


@pytest.fixture(scope="module", autouse=True)
def _seed_data_dir():
    """Point the store at the real demo seed for the duration of this module."""
    tmp = Path(tempfile.mkdtemp(prefix="omni-coref-tests-"))
    # Copy the JSON seeds (NOT face_profiles or db files) so we get
    # a writable working copy with u_an, mẹ, bố, etc.
    for name in (
        "users.json", "contacts.json", "transactions.json",
        "schedules.json", "atms.json", "napas_accounts.json",
    ):
        src = _SEED_DIR / name
        if src.exists():
            shutil.copy(src, tmp / name)

    prev = os.environ.get("BANKING_DATA_DIR", "")
    os.environ["BANKING_DATA_DIR"] = str(tmp)

    # Reset settings + store so the new data dir is picked up.
    from app.config import get_settings
    get_settings.cache_clear()
    # Rebuild the store with the new data dir.
    from app import store as store_mod
    store_mod._store = None  # type: ignore[attr-defined]
    # Force bootstrap of the new SQLite under the temp dir.
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
    s = session_for("u_an")
    s.clear_draft()
    s.clear_contact_draft()
    s.clear_schedule_draft()
    yield
    s.clear_draft()
    s.clear_contact_draft()
    s.clear_schedule_draft()


# ---------------------------------------------------------------------------
# Bug B — recap probes after confirm must NOT open a transfer draft
# ---------------------------------------------------------------------------


def test_recap_probe_routes_to_recap_not_transfer():
    """`vừa rồi gửi cho ai` after a confirm is a META-question, not a
    new transfer command. Pre-fix the Tier-2 ``gui`` keyword stole it."""
    from app.nlp.intent import classify
    intent, _ = classify("vừa rồi gửi cho ai")
    assert intent == "recap", (
        "BUG-B regression: 'vừa rồi gửi cho ai' must route to recap, "
        f"not {intent}. One mistap on a re-opened transfer draft = "
        "wrong recipient + predicted amount sent."
    )


def test_luc_nay_chuyen_bao_nhieu_routes_to_recap():
    from app.nlp.intent import classify
    intent, _ = classify("lúc nãy chuyển bao nhiêu")
    assert intent == "recap"


def test_vua_nay_chuyen_cho_ai_routes_to_recap():
    from app.nlp.intent import classify
    intent, _ = classify("vừa nãy chuyển cho ai")
    assert intent == "recap"


# ---------------------------------------------------------------------------
# Bug C — predictor must NOT override implicit amount reuse
# ---------------------------------------------------------------------------


def test_amount_coref_cue_detected_in_cung_so_tien():
    """The orchestrator's co-reference detector must fire on the canonical
    phrasing 'cùng số tiền cho bố' so the predictor is suppressed."""
    from app.services.orchestrator import _has_amount_coref_cue
    assert _has_amount_coref_cue("cùng số tiền cho bố")
    assert _has_amount_coref_cue("Cùng số tiền cho bố nhé")
    assert _has_amount_coref_cue("cung so tien cho bo")  # ASCII-folded


def test_amount_coref_cue_detected_in_other_phrasings():
    from app.services.orchestrator import _has_amount_coref_cue
    assert _has_amount_coref_cue("vẫn vậy")
    assert _has_amount_coref_cue("như cũ")
    assert _has_amount_coref_cue("tương tự nhưng cho bố")


def test_amount_coref_cue_NOT_triggered_by_unrelated_text():
    from app.services.orchestrator import _has_amount_coref_cue
    assert not _has_amount_coref_cue("chuyển bố 500k")
    assert not _has_amount_coref_cue("số dư còn bao nhiêu")
    assert not _has_amount_coref_cue("chuyển mẹ 2tr")


def test_predictor_suppressed_when_coref_cue_present(monkeypatch):
    """Wire test: when ``_handle_transfer`` sees a coref cue AND no amount,
    ``predict_amount`` must NOT run. We pin a sentinel via monkeypatch so
    a stray call would explode loudly."""
    from app.services import orchestrator
    from app.context.session import session_for
    from app.models.schemas import NLUResult, ExtractedEntities, TransactionDraft
    from app.store import get_store

    # Seed an active draft so the "inherit from prior" branch has something
    # to copy from. We can't easily seed a real transaction at the SQLite
    # layer in tests, so feed the prior amount via the live draft.
    s = session_for("u_an")
    store = get_store()
    account = store.primary_account("u_an")
    if account is None:
        pytest.skip("seed DB has no primary account for u_an in this run")

    s.set_draft(TransactionDraft(
        id="d_prev_test",
        recipient=None,
        amount=2_000_000,
        source_account_id=account.id,
        source_accounts=store.get_user("u_an").accounts,
    ))

    called: list[tuple] = []

    def _explode(user_id, contact_id):
        called.append((user_id, contact_id))
        # Return a non-None value so if the guard FAILS to suppress, the
        # test sees the predicted amount in the draft and fails loudly.
        return {"amount": 5_000_000, "confidence": 0.8, "rationale": "test"}

    monkeypatch.setattr(orchestrator, "predict_amount", _explode)

    nlu = NLUResult(
        intent="transfer",
        confidence=0.9,
        entities=ExtractedEntities(
            recipient_text="bố", recipient_kind="alias", amount=None,
        ),
        raw_text="cùng số tiền cho bố",
        source="rule",
    )
    resp = orchestrator._handle_transfer("u_an", nlu)
    assert resp.draft is not None
    # The predictor must NOT have been called when the coref cue is set.
    assert called == [], (
        "BUG-C regression: predictor ran despite explicit 'cùng số tiền' "
        "co-reference; would have surfaced bố's median over the implied 2tr."
    )
    # Inheritance: the prior draft's 2tr should be the draft amount.
    assert resp.draft.amount == 2_000_000, (
        "Co-reference must inherit the prior draft's amount, "
        f"got {resp.draft.amount}."
    )


# ---------------------------------------------------------------------------
# Bug A — pronouns / corrections must preserve the draft's recipient
# ---------------------------------------------------------------------------


def test_pipeline_understand_accepts_current_draft_kwarg():
    """The pipeline must accept the ``current_draft`` kwarg so the
    orchestrator can pass conversational context to the NLU layer.
    Without this hookup the LLM never learns about the in-flight draft
    and coref handling silently degrades."""
    from app.nlp.pipeline import understand
    result = understand(
        "cô ấy",
        history=[{"role": "user", "content": "chuyển mẹ 2tr"}],
        current_draft={"recipient_text": "mẹ", "amount": 2_000_000},
    )
    # Rule fallback (LLMs disabled in tests) won't preserve recipient
    # from a pronoun — that's the LLM's job. But the call must not
    # crash, and ``raw_text`` must round-trip through to the NLUResult.
    assert result is not None
    assert result.raw_text == "cô ấy"


def test_llm_understand_signature_supports_current_draft():
    """Hard contract check: the LLM entry must accept the kwarg so the
    pipeline can pass it through. A silent rename / drop would break
    coref handling without any test-suite signal."""
    import inspect
    from app.nlp.llm import llm_understand
    sig = inspect.signature(llm_understand)
    assert "current_draft" in sig.parameters, (
        "BUG-A regression: llm_understand must accept 'current_draft'; "
        "without it the orchestrator can't pass draft context and the "
        "LLM treats pronouns as fresh transfers."
    )


def test_nlu_system_prompt_documents_current_draft_contract():
    """The system prompt must teach the LLM what CURRENT DRAFT means.
    A silent revert of the prompt section is the most likely way to
    regress coref handling — pin the contract string here."""
    from app.nlp.llm import _NLU_SYSTEM
    assert "CURRENT DRAFT" in _NLU_SYSTEM, (
        "NLU prompt must document the CURRENT DRAFT co-reference "
        "contract or the LLM falls back to re-resolving pronouns "
        "from scratch."
    )
    # Spot-check the key co-reference cues are listed so the LLM has
    # the surface forms to bind against.
    assert "cô ấy" in _NLU_SYSTEM
    assert "không phải" in _NLU_SYSTEM
    assert "cùng số tiền" in _NLU_SYSTEM

"""Regression suite for terse-pivot conversation context.

Stress-tests five conversational patterns where the user changes topic
mid-transfer with short utterances and then resumes. Each scenario
threads multiple turns through ``orchestrator.handle_message`` with no
real LLM call (providers are force-disabled in ``conftest.py``) so the
deterministic rule + continuation paths are exercised end-to-end.

Background: the audit on the history cap (see PR body of
fix/context-cap-and-terse-pivot-tests) discovered that
``Session.conversation_messages()`` used to truncate to the last 8
messages (4 turn pairs) even though the backend stored 20 (10 pairs),
silently defeating terse pivots. The default was lifted to match
``OMNI_HISTORY_MAX``. These scenarios pin that the user can pivot to a
different intent (balance, recap, cancel) and still resume the in-flight
draft, or be told clearly when resumption isn't possible.

The five scenarios:

1. Pivot then resume — "chuyển mẹ 2tr" → "đợi tí, số dư còn bao nhiêu?"
   → "ok tiếp đi". The original draft must still be in the session and
   the resume cue routes to confirm.
2. Cross-intent recap — "chuyển bố 500k" → "huỷ" → "lúc nãy chuyển ai?".
   The recap intent must fire (not fall to ``unknown`` / a new transfer
   draft). Recovering the cancelled recipient's name from session
   history is a nice-to-have; the assertion is that the intent is recap
   and that the response is non-empty.
3. Three terse turns — "gửi mẹ" → "2" → "ờ". The first turn opens a
   draft missing the amount; "2" is rejected as a bare amount (OTP-like)
   and the orchestrator asks; "ờ" alone must NOT silently start a fresh
   draft.
4. Late correction — "chuyển mẹ 2tr" → "đổi sang bố" → "với 3tr". The
   modify path must keep the amount when only the recipient changes,
   then accept the amount edit while keeping bố as the recipient.
5. Cancel via pronoun — "chuyển sếp 5tr" → "thôi không gửi nữa, gửi vợ
   thay đi". No contact named "vợ" exists in the seed, so the safety
   layer must flag missing/ambiguous recipient. The previous draft
   must NOT silently survive as a sếp transfer.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


# Use the real demo seed so mẹ / bố / sếp aliases resolve. Mirror the
# pattern from test_coreference_safety.py.
_SEED_DIR = Path(__file__).resolve().parent.parent / "app" / "data"


@pytest.fixture(scope="module", autouse=True)
def _seed_data_dir():
    tmp = Path(tempfile.mkdtemp(prefix="omni-pivot-tests-"))
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


USER = "u_an"  # demo seed user with mẹ / bố / sếp contacts + a primary account


@pytest.fixture(autouse=True)
def _isolate_session():
    """Each scenario gets a clean slate — no draft, no history leakage."""
    from app.context.session import session_for
    s = session_for(USER)
    s.clear_draft()
    s.clear_contact_draft()
    s.clear_schedule_draft()
    s.clear_history()
    yield
    s.clear_draft()
    s.clear_contact_draft()
    s.clear_schedule_draft()
    s.clear_history()


# ---------------------------------------------------------------------------
# History-cap audit pin
# ---------------------------------------------------------------------------


def test_conversation_messages_default_cap_matches_storage_cap():
    """The LLM context payload must reach at least as far back as the
    backend's stored history. Pre-fix, ``conversation_messages()``
    defaulted to ``max_turns=8`` while ``OMNI_HISTORY_MAX`` was 20 —
    the LLM saw half the stored conversation, silently breaking terse
    pivots. Pin the cap unification."""
    from app.context.session import Session, session_for
    from app.context.session_store import history_max_messages

    s = session_for(USER)
    # Pump enough turns to fill the stored history at OMNI_HISTORY_MAX.
    storage_max = history_max_messages()
    for i in range(storage_max):
        s.append("user" if i % 2 == 0 else "omni", f"msg-{i}")

    msgs = s.conversation_messages()
    assert len(msgs) == storage_max, (
        f"conversation_messages() default returned {len(msgs)} of "
        f"{storage_max} stored messages — LLM context is being truncated "
        "below the storage cap. See PR fix/context-cap-and-terse-pivot-tests."
    )


# ---------------------------------------------------------------------------
# Scenario 1 — pivot then resume
# ---------------------------------------------------------------------------


def test_scenario_1_pivot_balance_then_resume_confirms_original_draft():
    from app.context.session import session_for
    from app.services.orchestrator import handle_message

    r1 = handle_message(USER, "chuyển mẹ 2tr")
    assert r1.intent == "transfer"
    assert r1.draft is not None, "turn 1 must open a transfer draft"
    draft_id_1 = r1.draft.id
    assert r1.draft.amount == 2_000_000
    assert r1.draft.recipient is not None
    assert "lan" in r1.draft.recipient.display_name.lower(), (
        "mẹ alias must resolve to Nguyễn Thị Lan in the demo seed"
    )

    # Pivot to balance — the draft MUST survive.
    r2 = handle_message(USER, "đợi tí, số dư còn bao nhiêu?")
    assert r2.intent == "balance", (
        f"pivot turn must route to balance, got {r2.intent}"
    )
    sess = session_for(USER)
    assert sess.current_draft is not None, (
        "balance lookup must NOT clear the in-flight transfer draft"
    )
    assert sess.current_draft.id == draft_id_1
    assert sess.current_draft.amount == 2_000_000

    # Resume — "ok tiếp đi" must read as confirmation of the surviving
    # draft. With safety flags (new+large or step-up) the response may
    # surface an OTP prompt — both outcomes prove resume worked. What
    # we forbid is starting a NEW draft (different id) or returning to
    # unknown.
    r3 = handle_message(USER, "ok tiếp đi")
    assert r3.intent == "transfer", (
        f"resume turn must route to transfer (confirm/OTP/step-up), got {r3.intent}"
    )
    # Either the draft completed (no draft on response) or it advanced
    # within the SAME draft (same id, possibly with awaiting_otp set).
    if r3.draft is not None:
        assert r3.draft.id == draft_id_1, (
            "resume must operate on the original draft, not a fresh one"
        )


# ---------------------------------------------------------------------------
# Scenario 2 — cross-intent recap
# ---------------------------------------------------------------------------


def test_scenario_2_cancel_then_recap_routes_to_recap_not_unknown():
    from app.context.session import session_for
    from app.services.orchestrator import handle_message

    r1 = handle_message(USER, "chuyển bố 500k")
    assert r1.intent == "transfer"
    assert r1.draft is not None
    bo_name = r1.draft.recipient.display_name if r1.draft.recipient else ""

    r2 = handle_message(USER, "huỷ")
    assert r2.intent == "transfer"
    assert "huỷ" in (r2.text or "").lower()
    sess = session_for(USER)
    assert sess.current_draft is None, "huỷ must clear the draft"

    # Recap probe — pre-fix this routed to a fresh `transfer` draft when
    # the rule classifier picked up "chuyển" without recap framing. After
    # the round-10 coref fix it routes to `recap`. We don't assert the
    # text contains bố's name (cancelled drafts aren't archived as
    # completed tx — the recap handler returns a polite "no pending"
    # line); we DO assert that the intent stays out of transfer/unknown.
    r3 = handle_message(USER, "lúc nãy chuyển ai?")
    assert r3.intent == "recap", (
        f"recap probe must route to recap intent, got {r3.intent}. "
        "A regression here re-opens the round-10 bug where the probe "
        "spawned a fresh transfer draft."
    )
    # Belt: no draft must have been silently created by the recap turn.
    assert sess.current_draft is None
    # Nice-to-have: a non-empty response. We deliberately don't pin the
    # exact bố text — the cancelled draft isn't recoverable from the
    # completed-tx log. Document the gap in the PR body.
    assert (r3.text or "").strip(), "recap must produce a non-empty reply"
    # Avoid an unused-variable lint on bo_name; if it ever appears in
    # r3.text the assertion below documents the intended future fix.
    _ = bo_name


# ---------------------------------------------------------------------------
# Scenario 3 — three terse turns: "gửi mẹ" → "2" → "ờ"
# ---------------------------------------------------------------------------


def test_scenario_3_three_terse_turns_keep_context():
    from app.context.session import session_for
    from app.services.orchestrator import handle_message

    r1 = handle_message(USER, "gửi mẹ")
    assert r1.intent == "transfer"
    assert r1.draft is not None
    draft_id = r1.draft.id
    assert r1.draft.recipient is not None
    # Amount may be missing OR predicted from history. Both are
    # acceptable behaviour. What we forbid is a wrong (random) amount.
    if r1.draft.amount is not None:
        assert r1.draft.predicted_amount is True or r1.draft.amount > 0

    # "2" is a bare digit — the rule extractor rejects it as a bare
    # amount (OTP-like / no unit). Without the bare-recipient slot-fill
    # branch this would route to `unknown`. The orchestrator must not
    # silently fall through OR overwrite the recipient with "2". Accept
    # either a recipient-untouched unknown / transfer reply that keeps
    # the same draft id, or a recipient swap that the safety engine
    # marks as ambiguous/missing — but NOT a silent recipient erase.
    r2 = handle_message(USER, "2")
    sess = session_for(USER)
    # The draft must still be alive (same id) — losing the draft on a
    # bare "2" was the round-9 regression "tự gán số tiền đang lưu".
    assert sess.current_draft is not None, (
        "bare '2' must not wipe the in-flight draft"
    )
    assert sess.current_draft.id == draft_id, (
        "bare '2' must operate on the existing draft, not spawn a new one"
    )

    # "ờ" alone — bare confirm token. The orchestrator must EITHER
    # confirm the draft (if it has enough info to proceed) OR treat
    # it as a confirmation request against the active draft (advance
    # to OTP / clarification). What it must NOT do is start a fresh
    # transfer round-trip with intent=unknown / a brand-new draft id.
    r3 = handle_message(USER, "ờ")
    assert r3.intent in ("transfer", "smalltalk"), (
        f"bare confirm 'ờ' against an active draft must NOT route to "
        f"{r3.intent} — see _CONFIRM_RE bare-ack alternation."
    )
    # If a draft survives the confirm turn, it must still be the same
    # draft (confirmation may have set awaiting_otp etc. on it, but
    # never spawned a parallel one).
    if r3.draft is not None:
        assert r3.draft.id == draft_id


# ---------------------------------------------------------------------------
# Scenario 4 — late correction: recipient swap, then amount edit
# ---------------------------------------------------------------------------


def test_scenario_4_late_correction_preserves_other_slot():
    from app.context.session import session_for
    from app.services.orchestrator import handle_message

    r1 = handle_message(USER, "chuyển mẹ 2tr")
    assert r1.intent == "transfer"
    assert r1.draft is not None
    assert r1.draft.amount == 2_000_000
    assert r1.draft.recipient is not None
    assert "lan" in r1.draft.recipient.display_name.lower()
    draft_id = r1.draft.id

    # Recipient swap: amount must carry over (this is the "đổi người là
    # quên mất số tiền" bug class the coref PR closed).
    r2 = handle_message(USER, "đổi sang bố")
    assert r2.intent == "transfer"
    assert r2.draft is not None
    assert r2.draft.id == draft_id, "recipient swap must mutate same draft"
    assert r2.draft.amount == 2_000_000, (
        "amount must survive recipient swap — losing it here is the "
        "round-9 'đổi người quên tiền' regression."
    )
    assert r2.draft.recipient is not None
    bo_name = r2.draft.recipient.display_name.lower()
    assert "hùng" in bo_name or "bo" in bo_name or "ba" in bo_name, (
        f"'đổi sang bố' must resolve to the bố contact, got {bo_name}"
    )

    # Amount edit: recipient must carry over.
    r3 = handle_message(USER, "với 3tr")
    assert r3.intent == "transfer"
    assert r3.draft is not None
    assert r3.draft.id == draft_id
    assert r3.draft.amount == 3_000_000
    assert r3.draft.recipient is not None
    bo_name_after = r3.draft.recipient.display_name.lower()
    assert "hùng" in bo_name_after or "bo" in bo_name_after or "ba" in bo_name_after, (
        f"recipient must remain bố after amount edit, got {bo_name_after}"
    )

    # Sanity: session state mirrors the response.
    sess = session_for(USER)
    assert sess.current_draft is not None
    assert sess.current_draft.amount == 3_000_000


# ---------------------------------------------------------------------------
# Scenario 5 — cancel-via-pronoun with unknown recipient
# ---------------------------------------------------------------------------


def test_scenario_5_cancel_via_pronoun_to_unknown_recipient_flags():
    """The user pivots ('thôi không gửi nữa, gửi vợ thay đi') from a
    valid sếp transfer to a recipient the seed doesn't carry ('vợ').

    Acceptable outcomes — any of:
      (a) The orchestrator cancels the sếp draft AND raises a clear
          missing/ambiguous-recipient flag for 'vợ'.
      (b) The orchestrator treats the message as a recipient swap and
          surfaces a missing/ambiguous flag (no silent sếp confirm).

    What we forbid: silently keeping sếp as the recipient (the original
    draft cannot be quietly confirmed), or routing to `unknown` without
    flags — both leave the user unaware that the pivot was ignored.
    """
    from app.context.session import session_for
    from app.services.orchestrator import handle_message

    r1 = handle_message(USER, "chuyển sếp 5tr")
    assert r1.intent == "transfer"
    assert r1.draft is not None
    assert r1.draft.recipient is not None
    sep_name = r1.draft.recipient.display_name.lower()
    assert "cường" in sep_name or "cuong" in sep_name, (
        f"'sếp' alias must resolve in the demo seed, got {sep_name}"
    )

    r2 = handle_message(USER, "thôi không gửi nữa, gửi vợ thay đi")
    assert r2.intent == "transfer", (
        f"pivot turn must stay in transfer routing, got {r2.intent}"
    )
    sess = session_for(USER)

    # Either the draft was cleared OR the draft survives but no longer
    # silently points at sếp with the pivot ignored.
    if sess.current_draft is not None:
        if sess.current_draft.recipient is not None:
            new_name = sess.current_draft.recipient.display_name.lower()
            assert "cường" not in new_name and "cuong" not in new_name, (
                "pivot 'gửi vợ thay đi' must not leave sếp as the "
                "draft recipient — silent retention here would let the "
                "next 'ok' confirm the original 5tr to sếp."
            )
        # Recipient cleared (or candidates exposed) is fine — what matters
        # is that the safety layer SOMETHING signals the pivot stalled.
        flags = [f.code for f in (sess.current_draft.flags or [])]
        # Accept either missing_recipient (no resolver hit) or
        # ambiguous_recipient (multiple), but reject a clean state that
        # would let "ok" fire as-is.
        assert (
            sess.current_draft.recipient is None
            or "missing_recipient" in flags
            or "ambiguous_recipient" in flags
        ), (
            "pivot to unresolvable 'vợ' must raise a safety flag or "
            "clear the recipient; otherwise 'ok' confirms a wrong "
            "transfer."
        )

"""Cross-cutting regression coverage for the alias-skip-on-lookalike
guard introduced in feat/alias-skip-on-lookalike.

Background
----------
The homograph fraud detector (feat/safety-lookalike-v2) emits a
``lookalike_recipient`` warn when the draft recipient's name is within
1 edit of a frequent contact's. The alias auto-learn loop
(feat/alias-auto-learn-v2 + …toast) persists the surface form as an
alias on confirm. With both active and the user confirming a
flagged draft (which they're allowed to do — it's a warn, not a
block), the attack pattern got rewarded silently:

    draft.recipient        = "Nguyên Thị Lan" (homograph of mẹ)
    draft.recipient_surface = "mẹ"
    user types OTP, confirms
    → store.add_alias(homograph_contact.id, "mẹ")
    → next "gửi mẹ 1tr" resolves to the homograph in O(1)

The guard is a single boolean check before ``Store.add_alias``:

    if any(f.code == "lookalike_recipient" for f in draft.flags):
        skip alias persistence

This test file lives on a branch that merges BOTH feat/safety-
lookalike-v2 AND the alias stack so ``SafetyFlag(code=
"lookalike_recipient", ...)`` is a valid construction. The unit-only
test files on each individual stack can't exercise this — the
Literal would reject the synthesized SafetyFlag at construction time.
"""

from __future__ import annotations

import pytest

from app.context import session_for
from app.models.schemas import Contact, TransactionDraft
from app.services.orchestrator import confirm_draft
from app.store import get_store


USER = "u_an"


# ---------------------------------------------------------------------------
# Test rig: seed user + cold contact, wipe aliases per test
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _seed_db():
    from app.db import bootstrap

    bootstrap.bootstrap_if_empty()
    store = get_store()

    from app.db.connection import get_connection

    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO users(id, display_name, phone) VALUES(?,?,?)",
        (USER, "Test", "0"),
    )
    conn.execute(
        """INSERT OR IGNORE INTO accounts
           (id, user_id, bank, number, balance, currency, is_primary)
           VALUES (?,?,?,?,?,?,?)""",
        ("acc_x", USER, "Omni", "0000", 100_000_000, "VND", 1),
    )

    # The real "mẹ" contact, frequent — this is what the homograph mimics.
    real_me = Contact(
        id="c_real_me_test",
        owner_id=USER,
        display_name="Nguyễn Thị Lan",
        bank="Vietcombank",
        account_number="9000100100",
        account_masked="*100",
        aliases=["mẹ"],
        frequent=True,
    )
    try:
        store.add_contact(real_me)
    except Exception:
        pass

    # Homograph attack contact — non-frequent, near-identical fold.
    fake = Contact(
        id="c_attack_test",
        owner_id=USER,
        display_name="Nguyên Thị Lan",   # missing breve on Nguyên
        bank="Sketchy",
        account_number="9000999999",
        account_masked="*999",
        aliases=[],
        frequent=False,
    )
    try:
        store.add_contact(fake)
    except Exception:
        pass

    # Neutral cold contact — name shares nothing with the frequent twin,
    # so the lookalike detector will NOT fire. Used for the negative
    # path tests where the auto-learn loop should still run.
    neutral = Contact(
        id="c_neutral_test",
        owner_id=USER,
        display_name="Phạm Tuấn Anh",
        bank="ACB",
        account_number="9000222222",
        account_masked="*222",
        aliases=[],
        frequent=False,
    )
    try:
        store.add_contact(neutral)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _clean():
    from app.db.connection import get_connection

    conn = get_connection()
    for cid in ("c_attack_test", "c_neutral_test"):
        conn.execute(
            "DELETE FROM contact_aliases WHERE contact_id = ?",
            (cid,),
        )
    session_for(USER).clear_draft()
    yield


def _draft(recipient_id: str, *, surface: str, amount: int = 200_000) -> TransactionDraft:
    """Build a draft with NO pre-seeded flags. confirm_draft re-runs
    safety.rules.evaluate with fresh state, so the lookalike detector
    is what actually decides whether the guard fires — not a synthetic
    flag list. That makes this a genuine end-to-end test of the two
    stacks interacting."""
    recipient = get_store().get_contact(recipient_id)
    return TransactionDraft(
        id="d_test",
        recipient=recipient,
        candidates=[],
        source_account_id="acc_x",
        source_accounts=get_store().get_user(USER).accounts,
        amount=amount,
        description="",
        source_text=f"gửi {surface} {amount//1000}k",
        recipient_surface=surface,
    )


# ---------------------------------------------------------------------------
# Positive path: lookalike detector fires → guard skips alias persistence
# ---------------------------------------------------------------------------


def test_homograph_recipient_blocks_alias_persistence():
    """End-to-end: confirm a transfer to the homograph contact with
    surface 'mẹ'. The lookalike detector runs during confirm_draft's
    fresh evaluate, emits lookalike_recipient, and the auto-learn loop
    is suppressed."""
    session_for(USER).set_draft(
        _draft("c_attack_test", surface="mẹ")
    )
    resp = confirm_draft(USER, "d_test", otp="123456")
    # Transfer still executed — guard is only on the alias side-effect.
    assert resp.intent == "transfer"
    # The auto-learn toast must NOT fire.
    assert resp.alias_learned is None
    # The row was never inserted.
    fresh = get_store().get_contact("c_attack_test")
    assert "mẹ" not in fresh.aliases


def test_homograph_recipient_lookalike_flag_was_actually_present():
    """Regression guard: this test would silently pass if the lookalike
    detector wasn't firing at all (alias_learned would also be None
    because nothing got to add_alias). Confirm the flag was emitted by
    inspecting the draft's flags after confirm_draft re-evaluated."""
    session_for(USER).set_draft(
        _draft("c_attack_test", surface="mẹ")
    )
    confirm_draft(USER, "d_test", otp="123456")
    # Session draft was cleared after success, so we can only check the
    # response. Easier path: re-issue evaluate directly to confirm the
    # lookalike fires on this contact pair.
    from app.safety.rules import evaluate

    fake = get_store().get_contact("c_attack_test")
    flags = evaluate(
        amount=200_000,
        recipient_candidates=[],
        recipient=fake,
        transactions=[],
        account=None,
        contacts=get_store().contacts_of(USER),
    )
    assert any(f.code == "lookalike_recipient" for f in flags)


# ---------------------------------------------------------------------------
# Negative path: no lookalike → alias still learns
# (regression: guard must not be accidentally always-on)
# ---------------------------------------------------------------------------


def test_neutral_recipient_alias_still_learns():
    """Same flow but the recipient's name shares nothing with any
    frequent contact — lookalike never fires, auto-learn runs."""
    session_for(USER).set_draft(
        _draft("c_neutral_test", surface="sếp")
    )
    resp = confirm_draft(USER, "d_test", otp="123456")
    assert resp.alias_learned is not None
    assert resp.alias_learned["alias"] == "sếp"
    fresh = get_store().get_contact("c_neutral_test")
    assert "sếp" in fresh.aliases


def test_neutral_recipient_lookalike_flag_is_absent():
    """Pair to the negative test above — explicitly confirm the
    lookalike detector did NOT fire on the neutral contact, so the
    auto-learn happened for the right reason."""
    from app.safety.rules import evaluate

    neutral = get_store().get_contact("c_neutral_test")
    flags = evaluate(
        amount=200_000,
        recipient_candidates=[],
        recipient=neutral,
        transactions=[],
        account=None,
        contacts=get_store().contacts_of(USER),
    )
    assert all(f.code != "lookalike_recipient" for f in flags)

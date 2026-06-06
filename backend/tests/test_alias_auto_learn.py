"""Regression coverage for the alias auto-learn stack.

Three layers under test, top-to-bottom:

  * ``services.orchestrator._is_worth_learning_alias`` — the in-memory
    redundancy filter that decides whether a surface form is worth
    persisting at all.
  * ``store.Store.add_alias`` — the idempotent SQLite insert. Returns
    True only on the first successful insertion; INSERT OR IGNORE
    means subsequent calls with the same (contact_id, alias) return
    False with no DB change.
  * Orchestrator confirm flow — end-to-end, with the OmniResponse
    ``alias_learned`` toast firing exactly when the bottom layer
    inserted a new row.

Tests bypass the LLM (forced off by conftest) so the rule pipeline
alone is exercised. The Pydantic test DB lives in a tempdir per
session — see ``conftest._bootstrap_test_env``.
"""

from __future__ import annotations

import pytest

from app.context import session_for
from app.models.schemas import Contact, TransactionDraft
from app.services.orchestrator import (
    _is_worth_learning_alias,
    confirm_draft,
)
from app.store import get_store


USER = "u_an"


# ---------------------------------------------------------------------------
# Bootstrap a seed user + contact set into the tmp DB once per session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _seed_db():
    """Insert one user, one account, one frequent contact (with prior
    aliases), and one cold contact (no aliases). The session-scoped DB
    is bootstrapped fresh per pytest run — we never collide with the
    dev seed."""
    from app.db import bootstrap

    bootstrap.bootstrap_if_empty()
    store = get_store()

    # Seed a user if the test bootstrap didn't.
    from app.db.connection import get_connection

    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO users(id, display_name, phone) VALUES(?,?,?)",
        (USER, "Test User", "0000000000"),
    )
    conn.execute(
        """INSERT OR IGNORE INTO accounts
           (id, user_id, bank, number, balance, currency, is_primary)
           VALUES (?,?,?,?,?,?,?)""",
        ("acc_test", USER, "Omni Bank", "0011223344", 100_000_000, "VND", 1),
    )

    # Frequent contact with prior aliases.
    me = Contact(
        id="c_test_me",
        owner_id=USER,
        display_name="Nguyễn Thị Lan",
        bank="Vietcombank",
        account_number="9000111222",
        account_masked="*222",
        aliases=["mẹ", "me", "mom"],
        frequent=True,
    )
    try:
        store.add_contact(me)
    except Exception:
        # Already exists from a prior run; refresh aliases via add_alias.
        for a in me.aliases:
            store.add_alias(me.id, a)

    # Cold contact, no aliases. Display name has three tokens so the
    # "all-tokens-in-name" redundancy filter has something to bite on.
    cold = Contact(
        id="c_test_cold",
        owner_id=USER,
        display_name="Trần Văn Tuấn",
        bank="MB Bank",
        account_number="9000333444",
        account_masked="*444",
        aliases=[],
        frequent=False,
    )
    try:
        store.add_contact(cold)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _clean_test_alias_table():
    """Wipe any cold-contact aliases between tests so toast / idempotent
    expectations stay deterministic across run order. The frequent
    contact's seed aliases are left alone — those rows are part of the
    expected state."""
    from app.db.connection import get_connection

    get_connection().execute(
        "DELETE FROM contact_aliases WHERE contact_id = ?",
        ("c_test_cold",),
    )
    session_for(USER).clear_draft()
    yield


# ---------------------------------------------------------------------------
# Layer 1: _is_worth_learning_alias filter
# ---------------------------------------------------------------------------


def test_filter_rejects_existing_alias_after_fold():
    me = get_store().get_contact("c_test_me")
    assert me is not None
    # "mẹ" is in aliases; the literal string AND its accent-folded form
    # both fold to "me" so neither should learn.
    assert _is_worth_learning_alias("mẹ", me) is False
    assert _is_worth_learning_alias("me", me) is False
    assert _is_worth_learning_alias("MOM", me) is False  # case-insensitive


def test_filter_rejects_full_display_name():
    me = get_store().get_contact("c_test_me")
    assert _is_worth_learning_alias("Nguyễn Thị Lan", me) is False
    # ASCII strip equivalent also folds to the same.
    assert _is_worth_learning_alias("Nguyen Thi Lan", me) is False


def test_filter_rejects_all_tokens_in_display_name():
    cold = get_store().get_contact("c_test_cold")
    assert cold is not None
    # 'tuấn' is one of {tran, van, tuan} → all-in → reject.
    assert _is_worth_learning_alias("Tuấn", cold) is False
    # Multi-token but every token in name → reject.
    assert _is_worth_learning_alias("Văn Tuấn", cold) is False


def test_filter_accepts_relational_prefix():
    """'anh Tuấn' contains 'anh' which is NOT in the display name
    tokens {tran, van, tuan} → worth learning."""
    cold = get_store().get_contact("c_test_cold")
    assert _is_worth_learning_alias("anh Tuấn", cold) is True


def test_filter_accepts_nickname():
    """A nickname that shares zero tokens with the display name is
    obviously worth learning."""
    cold = get_store().get_contact("c_test_cold")
    assert _is_worth_learning_alias("sếp", cold) is True


def test_filter_rejects_too_short():
    cold = get_store().get_contact("c_test_cold")
    assert _is_worth_learning_alias("a", cold) is False
    assert _is_worth_learning_alias("", cold) is False


# ---------------------------------------------------------------------------
# Layer 2: Store.add_alias
# ---------------------------------------------------------------------------


def test_store_add_alias_inserts_then_idempotent():
    s = get_store()
    inserted_first = s.add_alias("c_test_cold", "anh hai")
    assert inserted_first is True
    inserted_second = s.add_alias("c_test_cold", "anh hai")
    assert inserted_second is False  # already present


def test_store_add_alias_normalizes_for_dedup():
    """A second call with a different surface form that folds to the
    same string MUST be a no-op (we already have the canonical row)."""
    s = get_store()
    s.add_alias("c_test_cold", "sếp")
    inserted_again = s.add_alias("c_test_cold", "SẾP")
    assert inserted_again is False


def test_store_add_alias_rejects_empty():
    s = get_store()
    assert s.add_alias("c_test_cold", "") is False
    assert s.add_alias("c_test_cold", "   ") is False


# ---------------------------------------------------------------------------
# Layer 3: end-to-end orchestrator confirm flow
# ---------------------------------------------------------------------------


def _draft(recipient: Contact, *, surface: str, amount: int = 50_000) -> TransactionDraft:
    return TransactionDraft(
        id=f"d_{surface}",
        recipient=recipient,
        candidates=[],
        source_account_id="acc_test",
        source_accounts=get_store().get_user(USER).accounts,
        amount=amount,
        description="",
        source_text=f"gửi {surface} {amount//1000}k",
        recipient_surface=surface,
    )


def test_confirm_first_time_fires_toast():
    cold = get_store().get_contact("c_test_cold")
    session_for(USER).set_draft(_draft(cold, surface="anh hai"))
    r = confirm_draft(USER, "d_anh hai", otp="123456")
    assert r.alias_learned is not None
    assert r.alias_learned["alias"] == "anh hai"
    assert r.alias_learned["contact_id"] == cold.id
    fresh = get_store().get_contact(cold.id)
    assert any(a == "anh hai" for a in fresh.aliases)


def test_confirm_same_alias_twice_no_toast_second_time():
    cold = get_store().get_contact("c_test_cold")
    # First confirm seeds the alias.
    session_for(USER).set_draft(_draft(cold, surface="sếp", amount=10_000))
    confirm_draft(USER, "d_sếp", otp="123456")
    # Second confirm of the same phrase — toast must stay None.
    session_for(USER).set_draft(_draft(cold, surface="sếp", amount=20_000))
    r2 = confirm_draft(USER, "d_sếp", otp="123456")
    assert r2.alias_learned is None


def test_confirm_redundant_surface_does_not_fire_toast():
    """Surface == display_name is filtered upstream; no toast."""
    cold = get_store().get_contact("c_test_cold")
    session_for(USER).set_draft(_draft(cold, surface=cold.display_name))
    r = confirm_draft(USER, f"d_{cold.display_name}", otp="123456")
    assert r.alias_learned is None
    fresh = get_store().get_contact(cold.id)
    assert cold.display_name not in fresh.aliases


def test_confirm_without_surface_does_not_fire_toast():
    """A draft built with recipient_surface=None (e.g. from a contact-
    picker tap rather than NLU) must not learn anything."""
    cold = get_store().get_contact("c_test_cold")
    draft = TransactionDraft(
        id="d_nosurface",
        recipient=cold,
        candidates=[],
        source_account_id="acc_test",
        source_accounts=get_store().get_user(USER).accounts,
        amount=50_000,
        description="",
        source_text="",
        recipient_surface=None,
    )
    session_for(USER).set_draft(draft)
    r = confirm_draft(USER, "d_nosurface", otp="123456")
    assert r.alias_learned is None

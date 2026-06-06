"""Regression coverage for ``app.safety.lookalike``.

The detector pins three behaviours from feat/safety-lookalike-v2 +
feat/safety-lookalike-details:

  * **homograph** (distance 0 after accent fold) is always flagged.
  * **near-match** (distance 1) is flagged ONLY when first AND last
    tokens match exactly — kills the sibling-name false-positive class
    that dominates real Vietnamese contact lists (Nguyễn Anh Anh vs
    Nguyễn Anh An).
  * The resulting ``SafetyFlag`` carries a structured ``details``
    payload with ``match_kind``, ``edit_distance``, candidate /twin
    contact metadata — the shape the new UI cards consume.

All tests bypass the orchestrator and call the detector directly so a
future re-shape of the orchestrator's safety hook can't silently break
the underlying contract.
"""

from __future__ import annotations

from app.models.schemas import Contact
from app.safety.lookalike import detect_lookalike, detect_lookalike_match
from app.safety.rules import evaluate


# ---------------------------------------------------------------------------
# Fixtures (plain functions — no pytest fixture overhead needed)
# ---------------------------------------------------------------------------


def _make_contact(
    name: str,
    *,
    frequent: bool = False,
    cid: str = "c_test",
    bank: str = "BankX",
) -> Contact:
    return Contact(
        id=cid,
        owner_id="u_an",
        display_name=name,
        bank=bank,
        account_number="9999999999",
        account_masked="*999",
        aliases=[],
        frequent=frequent,
    )


def _frequent_pool() -> list[Contact]:
    """Three frequent contacts used as homograph/near-match targets."""
    return [
        _make_contact("Nguyễn Thị Lan", frequent=True,
                       cid="c_freq_1", bank="Vietcombank"),
        _make_contact("Nguyễn Văn Minh", frequent=True,
                       cid="c_freq_2", bank="MB Bank"),
        _make_contact("Trần Hoàng Phương", frequent=True,
                       cid="c_freq_3", bank="VPBank"),
    ]


# ---------------------------------------------------------------------------
# Positive: homograph & near-match must flag
# ---------------------------------------------------------------------------


def test_homograph_distance_zero_flags():
    """'Nguyên Thị Lan' (no breve on Nguyên) folds to the same string as
    'Nguyễn Thị Lan'. Distance after fold = 0 → always flag."""
    fake = _make_contact("Nguyên Thị Lan", cid="c_attack")
    match = detect_lookalike_match(fake, _frequent_pool())
    assert match is not None
    twin, distance = match
    assert twin.id == "c_freq_1"
    assert distance == 0


def test_ascii_strip_homograph_flags():
    """'Nguyen Thi Lan' (raw ASCII) folds same as 'Nguyễn Thị Lan'."""
    fake = _make_contact("Nguyen Thi Lan", cid="c_attack")
    twin = detect_lookalike(fake, _frequent_pool())
    assert twin is not None and twin.id == "c_freq_1"


def test_near_match_with_matching_outer_tokens_flags():
    """'Trần Hoành Phương' — middle token 'Hoàng' → 'Hoành' is a 1-edit
    swap (g→h after fold), but first ('tran') and last ('phuong') tokens
    match the frequent contact exactly. That's the strict near-match
    profile the guard accepts."""
    fake = _make_contact("Trần Hoành Phương", cid="c_attack")
    match = detect_lookalike_match(fake, _frequent_pool())
    assert match is not None
    twin, distance = match
    assert twin.id == "c_freq_3"
    assert distance == 1


def test_near_match_with_last_token_diff_does_not_flag():
    """'Nguyễn Văn Mihn' — typo lands in the LAST token. The strict guard
    requires outer-token equality so this is dropped to avoid false
    positives on sibling names ('Nguyễn Anh An' vs 'Nguyễn Anh Anh')."""
    fake = _make_contact("Nguyễn Văn Mihn", cid="c_attack")
    twin = detect_lookalike(fake, _frequent_pool())
    assert twin is None


# ---------------------------------------------------------------------------
# Negative: sibling-name + cold-contact-vs-cold + short-name must NOT flag
# ---------------------------------------------------------------------------


def test_sibling_name_does_not_flag():
    """'Nguyễn Anh An' (frequent imagined separately) and 'Nguyễn Anh
    Anh' differ by distance 1, but the last token differs ('an' vs
    'anh') — sibling names sharing a surname are NOT lookalikes."""
    sibling_freq = _make_contact("Nguyễn Anh An", frequent=True,
                                  cid="c_sibling_freq")
    fake = _make_contact("Nguyễn Anh Anh", cid="c_sibling_cold")
    twin = detect_lookalike(fake, [sibling_freq])
    assert twin is None


def test_candidate_that_is_itself_frequent_never_flagged():
    """A frequent contact is by definition trusted — we never flag a
    frequent → frequent collision (that's a different problem)."""
    pool = _frequent_pool()
    one_of_them = pool[0]  # Nguyễn Thị Lan, already frequent
    twin = detect_lookalike(one_of_them, pool)
    assert twin is None


def test_short_name_skipped():
    """Names ≤ min_name_len (5 chars after fold) are too tiny to
    discriminate — 'An' vs 'Bằng' would flag spuriously."""
    fake = _make_contact("An", cid="c_short")
    short_freq = _make_contact("Bằng", frequent=True, cid="c_short_freq")
    twin = detect_lookalike(fake, [short_freq])
    assert twin is None


def test_no_frequent_contacts_in_pool_means_no_flag():
    """Lookalike requires SOMETHING for the candidate to look like."""
    cold_pool = [
        _make_contact("Nguyễn Thị Lan", frequent=False, cid="c_cold_1"),
    ]
    fake = _make_contact("Nguyên Thị Lan", cid="c_attack")
    twin = detect_lookalike(fake, cold_pool)
    assert twin is None


# ---------------------------------------------------------------------------
# Details payload on the SafetyFlag emitted by `safety.rules.evaluate`
# ---------------------------------------------------------------------------


def test_evaluate_emits_homograph_details_payload():
    fake = _make_contact("Nguyên Thị Lan", cid="c_attack")
    contacts = _frequent_pool() + [fake]
    flags = evaluate(
        amount=200_000,
        recipient_candidates=[],
        recipient=fake,
        transactions=[],
        account=None,
        contacts=contacts,
    )
    lookalike_flags = [f for f in flags if f.code == "lookalike_recipient"]
    assert len(lookalike_flags) == 1
    f = lookalike_flags[0]
    assert f.severity == "warn"
    d = f.details
    assert d is not None
    assert d["kind"] == "lookalike"
    assert d["match_kind"] == "homograph"
    assert d["edit_distance"] == 0
    assert d["candidate_name"] == "Nguyên Thị Lan"
    assert d["twin_name"] == "Nguyễn Thị Lan"
    assert d["twin_bank"] == "Vietcombank"
    assert d["twin_account_masked"] == "*999"  # from our fixture


def test_evaluate_emits_near_match_details_payload():
    fake = _make_contact("Trần Hoành Phương", cid="c_attack")
    contacts = _frequent_pool() + [fake]
    flags = evaluate(
        amount=200_000,
        recipient_candidates=[],
        recipient=fake,
        transactions=[],
        account=None,
        contacts=contacts,
    )
    lookalike_flags = [f for f in flags if f.code == "lookalike_recipient"]
    assert len(lookalike_flags) == 1
    d = lookalike_flags[0].details
    assert d["match_kind"] == "near_match"
    assert d["edit_distance"] == 1
    assert d["twin_name"] == "Trần Hoàng Phương"


def test_evaluate_without_contacts_kwarg_does_not_run_lookalike():
    """Backward compat — callers that don't pass contacts= must NOT see
    lookalike flags (the rest of the engine still runs)."""
    fake = _make_contact("Nguyên Thị Lan", cid="c_attack")
    flags = evaluate(
        amount=200_000,
        recipient_candidates=[],
        recipient=fake,
        transactions=[],
        account=None,
    )
    assert all(f.code != "lookalike_recipient" for f in flags)

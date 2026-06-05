"""Tests for the contact dedup detector and merge logic.

False-positive guard is the most important test here — merging two
*different* people permanently fragments the user's data.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.banking.dedup import (
    find_duplicate_groups,
    merge_contacts,
)
from app.models.schemas import Contact, Transaction


def C(
    cid: str,
    name: str,
    *,
    bank: str = "Vietcombank",
    acct: str = "0123456789",
    aliases: list[str] | None = None,
    label: str | None = None,
) -> Contact:
    return Contact(
        id=cid,
        owner_id="u_an",
        display_name=name,
        bank=bank,
        account_number=acct,
        account_masked="*" + acct[-4:],
        aliases=aliases or [],
        label=label,
        verified=True,
        frequent=False,
    )


def T(tid: str, contact_id: str, amount: int) -> Transaction:
    return Transaction(
        id=tid,
        owner_id="u_an",
        contact_id=contact_id,
        amount=amount,
        description="",
        category="other",
        status="completed",
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


# --- detection -------------------------------------------------------------


def test_finds_duplicates_by_same_account_number():
    """Two contacts with the same account_number always merge, even if
    the display_name diverges — same account = same person."""
    contacts = [
        C("c1", "Mẹ", acct="0123456456", aliases=["mẹ"]),
        C("c2", "Nguyễn Thị Lan", acct="0123456456", aliases=["mom"]),
        C("c3", "Bạn X", acct="9999999999"),
    ]
    groups = find_duplicate_groups(contacts)
    assert len(groups) == 1
    g = groups[0]
    assert {g.primary.id, *(c.id for c in g.candidates)} == {"c1", "c2"}
    assert g.reason == "same_account_number"


def test_finds_duplicates_by_bank_prefix_and_alias_overlap():
    """Same bank + first-4-digit prefix + token overlap above threshold
    — likely a typo on the last digit. Should merge."""
    contacts = [
        C(
            "c1",
            "Trần Quốc Hùng",
            bank="ACB",
            acct="9990001234",
            aliases=["hùng", "anh hùng"],
        ),
        C(
            "c2",
            "Trần Quốc Hùng",
            bank="ACB",
            acct="9990001235",  # last digit typo
            aliases=["hùng"],
        ),
        # Same bank but different prefix — no merge.
        C("c3", "Khác", bank="ACB", acct="1110001234"),
    ]
    groups = find_duplicate_groups(contacts)
    assert len(groups) == 1
    g = groups[0]
    assert {g.primary.id, *(c.id for c in g.candidates)} == {"c1", "c2"}
    assert g.reason in (
        "same_bank_prefix_and_alias_overlap",
        "alias_exact_match",
    )


def test_finds_duplicates_by_alias_exact_match():
    """One contact's display_name appears as another's alias AND they
    share enough identity tokens to cross the overlap floor — merge.

    Example: "Nguyễn Thị Lan" with alias "mẹ" + a separate "mẹ Lan"
    entry on the same bank-prefix. The proper-name token "lan" gives
    overlap; the alias gives the exact match."""
    contacts = [
        C(
            "c1",
            "Nguyễn Thị Lan",
            bank="Vietcombank",
            acct="0123456456",
            aliases=["mẹ", "mẹ Lan"],
        ),
        C(
            "c2",
            "mẹ Lan",
            bank="Vietcombank",
            acct="0123459999",  # same bank, same 4-digit prefix
            aliases=["mẹ"],
        ),
    ]
    groups = find_duplicate_groups(contacts)
    assert len(groups) == 1
    # Either reason is acceptable here — both rules fire.
    assert groups[0].reason in (
        "alias_exact_match",
        "same_bank_prefix_and_alias_overlap",
    )


def test_does_not_merge_ambiguous_short_honorific_names():
    """Two contacts both literally named "Mẹ" on different banks with
    no other tokens to compare against. This is ambiguous (wife's mom
    vs husband's mom?) — do not auto-merge."""
    contacts = [
        C("c1", "Mẹ", bank="Vietcombank", acct="0123456456", aliases=["mẹ"]),
        C(
            "c2",
            "Mẹ",
            bank="BIDV",
            acct="5555550000",
            aliases=["mẹ", "mom"],
        ),
    ]
    groups = find_duplicate_groups(contacts)
    assert groups == []


def test_does_not_merge_when_only_alias_overlaps_different_people():
    """The seed has c_minh_mb and c_minh_tcb — both have alias "minh" but
    DIFFERENT account_numbers, DIFFERENT banks, DIFFERENT display_names.
    They are different people. This must not merge."""
    contacts = [
        C(
            "c_minh_mb",
            "Nguyễn Văn Minh",
            bank="MB Bank",
            acct="9990000789",
            aliases=["minh", "anh minh"],
            label="Bạn",
        ),
        C(
            "c_minh_tcb",
            "Trần Hoàng Minh",
            bank="Techcombank",
            acct="9990000234",
            aliases=["minh", "hoàng minh"],
            label="Đồng nghiệp",
        ),
    ]
    groups = find_duplicate_groups(contacts)
    assert groups == []


def test_does_not_merge_honorifics_only_overlap():
    """Two contacts that share only a Vietnamese honorific token (anh,
    chị, em) must not merge — honorifics are stopwords."""
    contacts = [
        C(
            "c1",
            "Anh Sơn",
            bank="Vietcombank",
            acct="0123450555",
            aliases=["anh sơn"],
        ),
        C(
            "c2",
            "Anh Tuấn",
            bank="MB Bank",
            acct="9991110321",
            aliases=["anh tuấn"],
        ),
    ]
    groups = find_duplicate_groups(contacts)
    assert groups == []


def test_handles_diacritics_consistently():
    """`Mẹ` and `Me` (no diacritic) should be treated as the same token
    for overlap purposes; combined with same account, they merge."""
    contacts = [
        C("c1", "Mẹ Lan", acct="0123456456", aliases=["mẹ"]),
        C("c2", "Me Lan", acct="0123456456", aliases=["me"]),
    ]
    groups = find_duplicate_groups(contacts)
    assert len(groups) == 1


def test_empty_and_single_contact_inputs():
    assert find_duplicate_groups([]) == []
    assert find_duplicate_groups([C("c1", "Mẹ")]) == []


# --- merge -----------------------------------------------------------------


def test_merge_reattributes_transactions_and_deduplicates_aliases():
    """Merge folds aliases (deduped), re-attributes every tx, hard-deletes
    candidates, and reports the count."""
    contacts = {
        "c1": C("c1", "Mẹ", acct="0123456456", aliases=["mẹ", "me"]),
        "c2": C(
            "c2",
            "Nguyễn Thị Lan",
            acct="0123456456",
            aliases=["mom", "mẹ"],
            label="Mẹ ruột",
        ),
        "c3": C("c3", "Khác", acct="9999"),
    }
    transactions = {
        "t1": T("t1", "c1", 100_000),
        "t2": T("t2", "c1", 200_000),
        "t3": T("t3", "c2", 50_000),
        "t4": T("t4", "c3", 999_000),
    }

    result = merge_contacts(
        contacts, transactions, primary_id="c1", candidate_ids=["c2"]
    )

    # All c2 txs now point to c1.
    assert transactions["t3"].contact_id == "c1"
    # Untouched.
    assert transactions["t4"].contact_id == "c3"
    assert result["merged_tx_count"] == 1

    # c2 is removed.
    assert "c2" not in contacts
    # c1 remains.
    assert "c1" in contacts

    # Aliases deduped, candidate's display_name + label folded in for
    # search-by-old-name.
    aliases = contacts["c1"].aliases
    normalized = {a.lower() for a in aliases}
    # original kept
    assert "mẹ" in aliases or "me" in normalized
    # candidate's mom alias merged in
    assert "mom" in aliases
    # candidate's display_name folded in as a recoverable alias
    assert "Nguyễn Thị Lan" in aliases
    # No exact duplicates
    assert len(aliases) == len(set(aliases))


def test_merge_leaves_a_single_contact_for_the_group():
    """3-way merge: 2 candidates → 1 surviving primary."""
    contacts = {
        "c1": C("c1", "Mẹ", acct="111", aliases=["mẹ"]),
        "c2": C("c2", "Mẹ (cũ)", acct="111", aliases=["mẹ cũ"]),
        "c3": C("c3", "mẹ Lan", acct="111", aliases=["lan"]),
    }
    transactions = {
        "t1": T("t1", "c1", 1),
        "t2": T("t2", "c2", 2),
        "t3": T("t3", "c3", 3),
    }
    result = merge_contacts(
        contacts, transactions, primary_id="c1", candidate_ids=["c2", "c3"]
    )
    assert set(contacts.keys()) == {"c1"}
    assert all(t.contact_id == "c1" for t in transactions.values())
    assert result["merged_tx_count"] == 2  # t2 + t3
    assert len(result["audit"]["merged_candidates"]) == 2


def test_merge_audit_snapshots_candidates():
    """Audit must record original aliases so the merge can be reconstructed."""
    contacts = {
        "c1": C("c1", "Mẹ", acct="111", aliases=["mẹ"]),
        "c2": C("c2", "Nguyễn Thị Lan", acct="111", aliases=["lan"]),
    }
    result = merge_contacts(
        contacts, {}, primary_id="c1", candidate_ids=["c2"]
    )
    snap = result["audit"]["merged_candidates"]
    assert len(snap) == 1
    assert snap[0]["id"] == "c2"
    assert snap[0]["aliases"] == ["lan"]
    assert snap[0]["display_name"] == "Nguyễn Thị Lan"


def test_merge_rejects_missing_primary():
    with pytest.raises(KeyError):
        merge_contacts({}, {}, primary_id="nope", candidate_ids=[])


# --- integration smoke on the demo seed -----------------------------------


def test_demo_seed_no_false_positives():
    """The hand-curated demo seed has two "Minh" contacts on different
    banks/accounts. They MUST NOT be flagged. Other contacts must also
    not collide."""
    import json
    from pathlib import Path

    seed_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "data"
        / "contacts.json"
    )
    raw = json.loads(seed_path.read_text(encoding="utf-8"))
    contacts = [Contact(**r) for r in raw]
    groups = find_duplicate_groups(contacts)
    # No duplicates in the curated demo seed.
    assert groups == [], (
        f"Demo seed flagged a false positive group: "
        f"{[(g.primary.display_name, [c.display_name for c in g.candidates]) for g in groups]}"
    )

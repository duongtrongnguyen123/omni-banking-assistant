"""Tone-sensitive recipient resolution — safety regression.

The original ``_fold`` stripped every Vietnamese diacritic, so
"bà" (grandma) and "ba" (dad / father-alias) collapsed to the same
key. A judge typing "bà" got a confident draft to a dad-aliased
contact and had to actively cancel to avoid a wrong transfer —
that violates the slide-deck "Safety stays on" contract.

These tests pin the two-tier match:

  * Tier A (strict, tone-preserved) is the only tier that runs when
    the user supplies tones. "bà" must not collide with "ba".
  * Tier B (loose ASCII) is the historical match, allowed only when
    the surface itself carries no tones — so a user on a tone-less
    keyboard still gets "ba" → dad.

We also re-pin the existing kinship aliases (mẹ / bố / sếp /
bạn thân) so the fix can't silently break the canonical demo set.
"""

from __future__ import annotations

import pytest

from app.context.alias import (
    _fold,
    _fold_keep_tones,
    _has_diacritics,
    resolve_recipient,
)
from app.models.schemas import Contact


def _mk(
    *,
    id: str,
    name: str,
    aliases: list[str],
    label: str | None = None,
    account: str = "0000000000",
) -> Contact:
    return Contact(
        id=id,
        owner_id="u_test",
        display_name=name,
        bank="Vietcombank",
        account_number=account,
        account_masked="*" + account[-3:],
        aliases=aliases,
        label=label,
        verified=True,
        frequent=True,
    )


@pytest.fixture
def book() -> list[Contact]:
    # Mirrors the canonical seed (backend/app/data/contacts.json).
    return [
        _mk(
            id="c_mom",
            name="Nguyễn Thị Lan",
            aliases=["mẹ", "me", "mom", "má"],
            label="Mẹ",
            account="0123456456",
        ),
        _mk(
            id="c_dad",
            name="Lê Văn Hùng",
            aliases=["bố", "ba", "papa"],
            label="Bố",
            account="0123459988",
        ),
        _mk(
            id="c_best",
            name="Vũ Quốc Bảo",
            aliases=["bảo", "bảo bro"],
            label="Bạn thân",
            account="0002233445",
        ),
        _mk(
            id="c_uncle",
            name="Phạm Văn Đạt",
            aliases=["chú đạt", "chú"],
            label="Chú",
            account="3344556677",
        ),
        _mk(
            id="c_bro",
            name="Phạm Anh Tuấn",
            aliases=["anh tuấn", "tuấn"],
            label="Anh trai",
            account="9991110321",
        ),
    ]


# ---------------------------------------------------------------------------
# Bug — tone collision must not pick the wrong recipient
# ---------------------------------------------------------------------------


def test_ba_with_huyen_tone_does_not_collide_with_dad_alias(book):
    """The headline bug. "bà" (grandma) used to ASCII-fold to "ba" and
    silently match "Lê Văn Hùng" whose alias list is
    ['bố', 'ba', 'papa']. Strict tier returns []."""
    assert resolve_recipient("bà", book) == []


def test_capitalised_ba_does_not_collide(book):
    """Capitalisation must not flip the safety behaviour."""
    assert resolve_recipient("Bà", book) == []


def test_ma_with_sac_tone_resolves_to_mom_not_a_ghost_collision(book):
    """"má" must resolve to mẹ (which carries 'má' in its alias list).
    The ASCII fallback would have matched anything with 'ma' — there's
    nothing else here, so this is mostly a positive-path pin, but it
    also documents the tone-preserved canonical: ``_fold_keep_tones('má')
    == _fold_keep_tones('ma') + sắc``, so the strict tier discriminates."""
    res = resolve_recipient("má", book)
    ids = [r.contact.id for r in res]
    assert ids == ["c_mom"]


def test_tone_less_ba_still_resolves_to_dad_via_ascii_tier(book):
    """User on a tone-less keyboard types "ba" — ASCII tier B is
    allowed because the surface itself carries no diacritics, so the
    historical behaviour ("ba" → dad-alias) is preserved."""
    res = resolve_recipient("ba", book)
    ids = [r.contact.id for r in res]
    assert ids == ["c_dad"]


def test_test_local_grandma_contact_resolves_strict(book):
    """If a user genuinely saves grandma under alias 'bà', the strict
    tier must route "bà" to HER and NOT to the dad-aliased contact."""
    grandma = _mk(
        id="c_grandma",
        name="Nguyễn Thị Hoa",
        aliases=["bà", "bà nội"],
        label="Bà",
        account="9990001111",
    )
    book_plus = book + [grandma]
    res = resolve_recipient("bà", book_plus)
    ids = [r.contact.id for r in res]
    # Strict match — exactly one contact, the grandma.
    assert ids == ["c_grandma"]


# ---------------------------------------------------------------------------
# Regression — the existing 16-case alias plan must still work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "surface, expected_id",
    [
        ("mẹ", "c_mom"),
        ("me", "c_mom"),
        ("mom", "c_mom"),
        ("má", "c_mom"),
        ("Mẹ", "c_mom"),
        ("mẹ tôi", "c_mom"),
        ("mẹ mình", "c_mom"),
        ("bố", "c_dad"),
        ("ba", "c_dad"),     # ASCII surface, loose tier wins
        ("papa", "c_dad"),
        ("bạn thân", "c_best"),
        ("anh tuấn", "c_bro"),
        ("tuấn", "c_bro"),
        ("anh Tuấn của tôi", "c_bro"),
        ("chú", "c_uncle"),
        ("chú đạt", "c_uncle"),
    ],
)
def test_canonical_aliases_still_resolve(book, surface, expected_id):
    res = resolve_recipient(surface, book)
    ids = [r.contact.id for r in res]
    assert expected_id in ids, (
        f"alias '{surface}' lost its match after the tone-safety fix; "
        f"got {ids}, expected {expected_id} in the list"
    )


# ---------------------------------------------------------------------------
# Unit-level tests for the helpers — pin the contract directly
# ---------------------------------------------------------------------------


def test_fold_keep_tones_preserves_huyen_sac_nga_hoi_nang():
    # The five canonical Vietnamese tones must survive fold_keep_tones.
    assert _fold_keep_tones("bà") != _fold_keep_tones("ba")
    assert _fold_keep_tones("bá") != _fold_keep_tones("ba")
    assert _fold_keep_tones("bã") != _fold_keep_tones("ba")
    assert _fold_keep_tones("bả") != _fold_keep_tones("ba")
    assert _fold_keep_tones("bạ") != _fold_keep_tones("ba")


def test_fold_keep_tones_strips_circumflex_breve_horn():
    # Base-letter decorations (NOT tone marks) get stripped — these are
    # not the ambiguity axis. So "ô" and "o" share the same keep-tones
    # key once both are tone-less.
    assert _fold_keep_tones("ô") == _fold_keep_tones("o")
    assert _fold_keep_tones("ă") == _fold_keep_tones("a")
    assert _fold_keep_tones("ơ") == _fold_keep_tones("o")
    assert _fold_keep_tones("ư") == _fold_keep_tones("u")


def test_fold_keep_tones_lowercases_and_collapses_d_stroke():
    assert _fold_keep_tones("Đà") == _fold_keep_tones("đà") == "dà"


def test_ascii_fold_still_collapses_everything():
    # The historical _fold MUST still strip everything for the loose
    # tier to keep working on tone-less surfaces.
    assert _fold("bà") == _fold("ba") == "ba"
    assert _fold("má") == _fold("ma") == "ma"


def test_has_diacritics_classifies_correctly():
    assert _has_diacritics("bà") is True
    assert _has_diacritics("Bà") is True
    assert _has_diacritics("anh Tuấn") is True
    assert _has_diacritics("ba") is False
    assert _has_diacritics("Tuan") is False
    assert _has_diacritics("anh Tuan") is False

"""Alias / fuzzy-name resolution.

Maps a surface form like "mẹ", "anh Minh", "Minh" to the user's contacts.
Returns all plausible candidates so the caller can decide whether to confirm
or ask for disambiguation.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import Optional

from ..models.schemas import Contact, ResolvedRecipient

log = logging.getLogger("omni.context.alias")


def _rag_fallback_enabled() -> bool:
    """RAG fuzzy-alias fallback feature flag.

    Default OFF — the exact / token / prefix / label hits in
    :func:`_lookup_in_aliases` + :func:`_lookup_in_names` cover the vast
    majority of real queries, and the embedding/lexical fallbacks have
    historically returned noisy candidates on cold queries. The flag
    keeps the RAG code path available for the slide-deck demo and for
    operators who want to re-enable it after re-tuning the cutoffs.

    Set ``OMNI_RAG_ALIAS=1`` to enable.
    """
    return (os.environ.get("OMNI_RAG_ALIAS", "0") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _fold(s: str) -> str:
    n = unicodedata.normalize("NFKD", s)
    return (
        "".join(c for c in n if not unicodedata.combining(c))
        .lower()
        .replace("đ", "d")
        .strip()
    )


# Vietnamese combining tone marks. These CHANGE WORD MEANING — bà
# (grandma, huyền) is NOT ba (dad, no-tone) is NOT bá (tone-sắc).
# Any folding that strips these silently fuses unrelated kinship
# terms and made the resolver pick the wrong recipient (judge audit:
# "bà" → contact whose alias was "ba" tone-folded for dad). All other
# combining marks (circumflex on â/ê/ô, breve on ă, horn on ơ/ư) are
# stripped so "bố" and "bó" collide deliberately — they're not the
# real ambiguity axis here; the tones are.
_VN_TONE_MARKS = frozenset({
    0x0300,  # huyền
    0x0301,  # sắc
    0x0303,  # ngã
    0x0309,  # hỏi
    0x0323,  # nặng
})


def _fold_keep_tones(s: str) -> str:
    """Like ``_fold`` but preserves Vietnamese tone marks.

    Returns NFC-normalised lowercase with đ→d and base-letter
    decorations (circumflex / breve / horn) stripped, but the five
    Vietnamese tones kept. Used as the strict-tier comparison key so
    "bà" never collides with "ba"/"bố"/"bá".
    """
    n = unicodedata.normalize("NFKD", s.lower().replace("đ", "d"))
    out: list[str] = []
    for c in n:
        if unicodedata.combining(c):
            if ord(c) in _VN_TONE_MARKS:
                out.append(c)
            # else: drop circumflex / breve / horn — same as _fold
        else:
            out.append(c)
    return unicodedata.normalize("NFC", "".join(out)).strip()


def _has_diacritics(s: str) -> bool:
    """True when ``s`` carries any combining mark or đ — i.e. the user
    typed a tone-bearing form. False when input is pure ASCII (tone-less)
    and the loose ASCII-fold tier is safe to use."""
    nfd = unicodedata.normalize("NFD", s)
    ascii_form = nfd.encode("ascii", "ignore").decode("ascii")
    return s != ascii_form


# Family/relational prefixes that should be stripped to match name tokens.
_RELATIONAL_PREFIXES = (
    "anh ", "chi ", "em ", "ban ",
    "co ", "chu ", "bac ", "ong ", "ba ",
    # Additional kinship honorifics — pre-fix "chuyển dì Lan 200k" and
    # "chuyển cậu Minh 200k" returned missing_recipient because the
    # honorific stayed glued to the name and the resolver couldn't
    # token-match "di lan" / "cau minh" against any display_name.
    "di ",     # "dì Lan" (aunt — mother's side)
    "cau ",    # "cậu Minh" (uncle — mother's side)
    "thay ",   # "thầy <Name>" (teacher)
)

# Possessive / vocative tail tokens — "mẹ tôi" / "mẹ mình" /
# "chị Lan ơi" / "anh Hùng nhé" / "bạn thân của tôi". None appear in
# any contact's display name or alias list, so leaving them in
# defeats the exact-alias match ("mẹ tôi" never hits the "mẹ" alias).
# Strip from the right.
#
# "của" (possessive marker, "of") is a critical addition: pre-fix
# "bạn thân của tôi" → strip only "toi" → "ban than cua" → no alias
# match. Now "cua" is stripped too, and the chain pops down to
# "ban than" which hits the alias. Same trap for "anh Tuấn của mình".
_RELATIONAL_TAIL_TOKENS = {
    "toi", "minh", "em", "anh", "chi",
    "oi", "nhe", "nha", "nhi", "a",   # "mẹ ạ"
    "cua",                             # possessive "của"
}


def _strip_relational(folded: str) -> str:
    s = folded
    for p in _RELATIONAL_PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
    # Token-level tail stripping — handles "mẹ tôi nhé" → "mẹ". Only
    # strips when the original has >1 token so a single-token name like
    # "Minh" doesn't get eaten (Minh is in the tail-token set... actually
    # it isn't; but "Em" / "Anh" / "Chi" might be a single name token,
    # so the >1 guard keeps them safe).
    tokens = s.split()
    while len(tokens) > 1 and tokens[-1] in _RELATIONAL_TAIL_TOKENS:
        tokens.pop()
    return " ".join(tokens)


def _strip_tail_only(folded: str) -> str:
    """Like ``_strip_relational`` but skips the prefix-strip step.

    Critical for matching multi-word labels whose first token is a
    relational word — e.g. "bạn thân của tôi" needs to land as
    "ban than" (tail "cua toi" gone, prefix "ban " KEPT) to hit the
    "Bạn thân" label on Vũ Quốc Bảo. The full strip used to chop "ban"
    too and reduced the query to "than", which matched nothing.

    Compound-noun guard: if the strip would collapse the surface to a
    SINGLE token AND that token is itself a relational PREFIX word
    (chu/anh/chi/em/ban/...), the original was almost certainly a
    compound noun ("chủ nhà", "anh em", "chị em") and we should NOT
    strip. Without this guard, ``_strip_tail_only("chu nha")`` returned
    "chu" because "nha" is in the tail-token set (kept there for the
    "mẹ nha" softener) — and "chu" then matched the "Chú" label on a
    completely unrelated contact. User report: hỏi "chủ nhà" lại nhận
    suggestion Phạm Văn Đạt (chú).
    """
    tokens = folded.split()
    # Try the strip on a copy so we can revert if it produces a too-short
    # compound-noun-looking result.
    stripped = list(tokens)
    while len(stripped) > 1 and stripped[-1] in _RELATIONAL_TAIL_TOKENS:
        stripped.pop()
    if (
        len(stripped) == 1
        and len(tokens) > 1
        and (stripped[0] + " ") in _RELATIONAL_PREFIXES
    ):
        # Revert — original was a compound noun like "chủ nhà".
        return " ".join(tokens)
    tokens = stripped
    return " ".join(tokens)


# Tone-preserved counterparts to the ASCII strip-token sets. We keep
# the lists in lockstep — when ``_RELATIONAL_PREFIXES`` grows, so does
# this one — so that "anh Tuấn" strips the "anh " prefix in both tiers.
# Built by applying ``_fold_keep_tones`` to the original Vietnamese
# canonical form; the ASCII set is the result of further stripping
# tone marks. Maintain by Vietnamese form to avoid drift.
# These literals are stored in the SAME shape produced by
# ``_fold_keep_tones``: lowercase, NFC, đ→d, with base-letter
# decorations (circumflex / breve / horn) already stripped — only the
# five tone marks remain. So "tôi" appears here as "toi", "ơi" as
# "oi", "ạ" as "ạ" (only the nặng-tone survives), "cô" as "co",
# "ông" as "ong", "cậu" as "cạu", "thầy" as "thày", etc.
_RELATIONAL_PREFIXES_TONE = (
    "anh ", "chị ", "em ", "bạn ",
    "co ", "chú ", "bác ", "ong ", "bà ",
    "dì ", "cạu ", "thày ",
)

_RELATIONAL_TAIL_TOKENS_TONE = {
    "toi", "mình", "em", "anh", "chị",
    "oi", "nhé", "nha", "nhỉ", "ạ",
    "của",
}


def _strip_relational_tone(folded: str) -> str:
    """Tone-preserving counterpart to ``_strip_relational``."""
    s = folded
    for p in _RELATIONAL_PREFIXES_TONE:
        if s.startswith(p):
            s = s[len(p):]
    tokens = s.split()
    while len(tokens) > 1 and tokens[-1] in _RELATIONAL_TAIL_TOKENS_TONE:
        tokens.pop()
    return " ".join(tokens)


def _strip_tail_only_tone(folded: str) -> str:
    """Tone-preserving counterpart to ``_strip_tail_only``."""
    tokens = folded.split()
    stripped = list(tokens)
    while len(stripped) > 1 and stripped[-1] in _RELATIONAL_TAIL_TOKENS_TONE:
        stripped.pop()
    if (
        len(stripped) == 1
        and len(tokens) > 1
        and (stripped[0] + " ") in _RELATIONAL_PREFIXES_TONE
    ):
        return " ".join(tokens)
    return " ".join(stripped)


def _last_token(folded_name: str) -> str:
    parts = folded_name.split()
    return parts[-1] if parts else folded_name


_ALIAS_HEURISTIC_TOKENS = frozenset({
    # Vietnamese kinship / role idioms — treated as alias kind when the
    # LLM didn't classify. Tokens used STAND-ALONE OR with a single
    # qualifier ("bạn thân", "anh hai"); a multi-word kinship-role
    # phrase whose head token is here also routes to alias.
    "me", "ba", "bo", "ny", "vo", "chong", "sep", "boss",
    "ban", "anh", "chi", "em", "co", "chu", "bac", "ong", "ba",
})


def _looks_like_alias_kind(folded: str) -> bool:
    """Heuristic for rule-fallback when LLM didn't tag recipient_kind.

    True when the surface starts with a kinship/role token and is short
    (≤ 3 tokens). Examples: 'bạn thân', 'anh hai', 'sếp', 'mẹ'. Rejects
    'Nguyễn Văn Minh', 'Nam', 'Tuấn'.
    """
    tokens = folded.split()
    if not tokens or len(tokens) > 3:
        return False
    return tokens[0] in _ALIAS_HEURISTIC_TOKENS


def resolve_recipient(
    surface: str,
    contacts: list[Contact],
    *,
    kind: Optional[str] = None,
) -> list[ResolvedRecipient]:
    """Resolve a recipient surface to candidate contacts.

    ``kind`` (NEW) is the LLM's hint, one of "alias" | "name" | None:
      - "alias": lookup ONLY in contact_aliases (exact fold match).
      - "name":  lookup ONLY in display_name (exact + token-exact).
      - None:    try alias first, then name. Drops the embedding
                 fallback that was returning noise on cold queries
                 like "bạn thân" or "grabfood".

    The previous semantic-fallback returned arbitrary "similar" names
    (5 garbage contacts on "cho Nam") — gone. Better to return 0 and
    let the chat ask again than confidently pick the wrong person.

    Tone-safety contract
    --------------------
    Vietnamese tone marks change word meaning — "bà" (grandma) is NOT
    "ba" (dad / three / father-alias), "má" (mother) is NOT "ma"
    (ghost), "chú" (uncle) is NOT "chu" (chicken-call), and so on.
    Pre-fix the resolver folded all diacritics away and silently
    routed "bà" to a contact whose dad-alias "ba" happened to
    ASCII-collide. We now run two tiers:

      • Tier A (strict, tone-preserved): compare via
        ``_fold_keep_tones`` so "bà" only ever hits an alias whose
        own tone-preserved form is "bà".
      • Tier B (loose, ASCII): only when the surface itself is pure
        ASCII (no diacritics — user typed without tones), fall back
        to the historical ``_fold`` match. This keeps "ba", "me",
        "bo" working for keyboards without tone input.
    """
    if not surface:
        return []
    query = _fold(surface)
    query_stripped = _strip_relational(query)
    # Tail-only variant — preserves relational PREFIXES so multi-word
    # labels whose first token IS the relational word ("Bạn thân",
    # "Bạn cấp 3", "Anh Hai") still match. Full strip would chop "bạn"
    # off and leave only "than", which matches nothing.
    query_tail = _strip_tail_only(query)

    # Tone-preserved parallel forms — strict tier.
    tone_query = _fold_keep_tones(surface)
    tone_query_stripped = _strip_relational_tone(tone_query)
    tone_query_tail = _strip_tail_only_tone(tone_query)

    surface_has_tones = _has_diacritics(surface)

    matches: list[ResolvedRecipient] = []

    # When kind is explicitly "alias" (LLM-confirmed), DO NOT fall through
    # to name lookups. User said "bạn thân" → if no alias matches, return
    # []; the chat asks again rather than guessing a random name.
    if kind == "alias":
        return _lookup_in_aliases(
            query, query_stripped, query_tail,
            tone_query, tone_query_stripped, tone_query_tail,
            surface_has_tones, contacts,
        )

    if kind == "name":
        return _lookup_in_names(
            query, query_stripped,
            tone_query, tone_query_stripped,
            surface_has_tones, contacts,
        )

    # kind is None — try alias-first, then name. No semantic fallback by
    # default. The previous behaviour promoted alias-shaped surfaces ("cô
    # Lan", "anh Minh") to a hard kind="alias" via _looks_like_alias_kind
    # and blocked the name fall-through entirely. That made "cô Lan"
    # return 0 even though stripping "cô" + token-matching "Lan" would
    # have surfaced ambiguity between the two Lans in the user's book.
    # Alias-first ordering still keeps "bạn thân" / "mẹ" routed correctly;
    # the fall-through only kicks in when no alias matches, and name
    # lookup is strict exact/token (no embedding noise).
    matches = _lookup_in_aliases(
        query, query_stripped, query_tail,
        tone_query, tone_query_stripped, tone_query_tail,
        surface_has_tones, contacts,
    )
    if matches:
        return matches
    name_matches = _lookup_in_names(
        query, query_stripped,
        tone_query, tone_query_stripped,
        surface_has_tones, contacts,
    )
    if name_matches:
        return name_matches

    # RAG fuzzy fallback — gated behind ``OMNI_RAG_ALIAS=1``. Lexical
    # (token-overlap) goes first because it's cheap and deterministic;
    # the embedding match is only consulted when lexical comes back
    # empty. Both feed into ``ResolvedRecipient`` with
    # ``matched_from="history"`` so the orchestrator can surface them
    # as lower-confidence candidates that still need user confirmation.
    #
    # Tone-safety note: the RAG path uses ASCII-folded ``_lexical_match``
    # and embedding similarity, neither of which preserves tones. That
    # is acceptable here because (a) it's gated off by default, and (b)
    # any candidate it returns is surfaced as low-confidence and still
    # requires user confirmation in the orchestrator.
    if _rag_fallback_enabled():
        lex = _lexical_match(surface, contacts)
        if lex:
            log.debug(
                "RAG lexical match for %r returned %d candidate(s)",
                surface, len(lex),
            )
            return lex
        emb = _embedding_match(surface, contacts)
        if emb:
            log.debug(
                "RAG embedding match for %r returned %d candidate(s)",
                surface, len(emb),
            )
            return emb
    return []


def _lookup_in_aliases(
    query: str,
    query_stripped: str,
    query_tail: str,
    tone_query: str,
    tone_query_stripped: str,
    tone_query_tail: str,
    surface_has_tones: bool,
    contacts: list[Contact],
) -> list[ResolvedRecipient]:
    """Two-tier alias lookup. See ``resolve_recipient`` for the contract.

    Tier A (strict, tone-preserved) always runs. If the user typed a
    tone-bearing surface (``surface_has_tones`` is True) AND tier A
    returns nothing, we DO NOT fall back — returning ``[]`` is the
    safety behaviour ("bà" must not collide with "ba"). Only when the
    surface is pure ASCII do we run tier B as a fallback.

    Three query forms per tier (raw / stripped / tail) — first match
    wins per contact.
    """
    # Tier A — strict, tone-preserved.
    strict: list[ResolvedRecipient] = []
    for c in contacts:
        matched = False
        for alias in c.aliases:
            t_alias = _fold_keep_tones(alias)
            if t_alias and t_alias in (
                tone_query, tone_query_stripped, tone_query_tail,
            ):
                strict.append(
                    ResolvedRecipient(
                        contact=c, via_alias=alias, matched_from="alias",
                    )
                )
                matched = True
                break
        if matched:
            continue
        if c.label:
            t_label = _fold_keep_tones(c.label)
            if t_label and t_label in (
                tone_query, tone_query_stripped, tone_query_tail,
            ):
                strict.append(
                    ResolvedRecipient(
                        contact=c, via_alias=c.label, matched_from="alias",
                    )
                )
    if strict:
        return _dedupe(strict)

    # Tier B — loose ASCII fold. Only safe when the user's surface
    # carried no tones to begin with. Otherwise we'd reintroduce the
    # silent "bà" → "ba" collision the strict tier exists to block.
    if surface_has_tones:
        return []

    matches: list[ResolvedRecipient] = []
    for c in contacts:
        matched = False
        for alias in c.aliases:
            folded = _fold(alias)
            if folded == query or folded == query_stripped or folded == query_tail:
                matches.append(
                    ResolvedRecipient(
                        contact=c, via_alias=alias, matched_from="alias",
                    )
                )
                matched = True
                break
        if matched:
            continue
        if c.label:
            folded_label = _fold(c.label)
            if (
                folded_label == query
                or folded_label == query_stripped
                or folded_label == query_tail
            ):
                matches.append(
                    ResolvedRecipient(
                        contact=c, via_alias=c.label, matched_from="alias",
                    )
                )
    return _dedupe(matches)


def _lookup_in_names(
    query: str,
    query_stripped: str,
    tone_query: str,
    tone_query_stripped: str,
    surface_has_tones: bool,
    contacts: list[Contact],
) -> list[ResolvedRecipient]:
    """Match against display_name with two-tier tone safety.

    Strict tier (tone-preserved) runs first. ASCII tier B is gated on
    a tone-less surface, same rule as ``_lookup_in_aliases``.
    """
    # Tier A — strict tone-preserved exact then token.
    strict: list[ResolvedRecipient] = []
    for c in contacts:
        t_full = _fold_keep_tones(c.display_name)
        if t_full == tone_query or t_full == tone_query_stripped:
            strict.append(ResolvedRecipient(contact=c, matched_from="exact"))
    if strict:
        return _dedupe(strict)

    for c in contacts:
        t_tokens = _fold_keep_tones(c.display_name).split()
        if tone_query_stripped and tone_query_stripped in t_tokens:
            strict.append(ResolvedRecipient(contact=c, matched_from="name"))
        elif tone_query and tone_query in t_tokens:
            strict.append(ResolvedRecipient(contact=c, matched_from="name"))
    if strict:
        return _dedupe(strict)

    # Tier B — loose ASCII, only if the user typed no tones.
    if surface_has_tones:
        return []

    matches: list[ResolvedRecipient] = []
    for c in contacts:
        folded = _fold(c.display_name)
        if folded == query or folded == query_stripped:
            matches.append(ResolvedRecipient(contact=c, matched_from="exact"))
    if matches:
        return _dedupe(matches)

    for c in contacts:
        tokens = _fold(c.display_name).split()
        if query_stripped and query_stripped in tokens:
            matches.append(ResolvedRecipient(contact=c, matched_from="name"))
        elif query and query in tokens:
            matches.append(ResolvedRecipient(contact=c, matched_from="name"))
    return _dedupe(matches)


# ---------------------------------------------------------------------------
# Embedding-based path (best precision when available)
# ---------------------------------------------------------------------------


def _embedding_match(
    surface: str, contacts: list[Contact]
) -> list[ResolvedRecipient]:
    from ..db.connection import get_connection
    from ..nlp.embeddings import cosine, embed, unpack

    query_vec = embed(surface, task_type="RETRIEVAL_QUERY")
    if query_vec is None or not contacts:
        return []

    ids = [c.id for c in contacts]
    placeholders = ",".join("?" * len(ids))
    rows = get_connection().execute(
        f"SELECT id, embedding FROM contacts "
        f"WHERE id IN ({placeholders}) AND embedding IS NOT NULL",
        ids,
    ).fetchall()
    if not rows:
        return []

    by_id = {c.id: c for c in contacts}
    scored: list[tuple[float, Contact]] = []
    for row in rows:
        score = cosine(query_vec, unpack(row["embedding"]))
        scored.append((score, by_id[row["id"]]))
    scored.sort(key=lambda x: x[0], reverse=True)

    CUTOFF = 0.55
    above = [(s, c) for s, c in scored if s >= CUTOFF]
    if not above:
        return []
    if len(above) == 1 or (above[0][0] - above[1][0]) > 0.08:
        return [ResolvedRecipient(contact=above[0][1], matched_from="history")]
    return [ResolvedRecipient(contact=c, matched_from="history") for _, c in above]


# ---------------------------------------------------------------------------
# Lexical (token-overlap) fallback — no network, no model dependency
# ---------------------------------------------------------------------------

# Tokens that are too generic to help discrimination ("the", "for", "to" of VN).
_STOP_TOKENS = {
    "nguoi", "ban", "ay", "do", "kia", "cua", "minh", "toi", "hay", "thuong",
    "o", "tai", "voi", "nay", "the", "qua", "vao",
}


def _lexical_match(
    surface: str, contacts: list[Contact]
) -> list[ResolvedRecipient]:
    query_tokens = {
        t for t in _fold(surface).split() if t and t not in _STOP_TOKENS
    }
    if not query_tokens:
        return []

    scored: list[tuple[float, Contact]] = []
    for c in contacts:
        doc_tokens = _contact_tokens(c)
        if not doc_tokens:
            continue
        overlap = query_tokens & doc_tokens
        if not overlap:
            continue
        # Weighted Jaccard: |overlap| / |query| × (1 + 0.1 × frequent flag).
        # Recall-biased — we'd rather return a couple of plausible candidates
        # for the orchestrator to ambiguate than miss the right one entirely.
        score = len(overlap) / max(len(query_tokens), 1)
        if c.frequent:
            score *= 1.1
        scored.append((score, c))

    if not scored:
        return []
    scored.sort(key=lambda x: x[0], reverse=True)
    CUTOFF = 0.35
    above = [(s, c) for s, c in scored if s >= CUTOFF]
    if not above:
        return []
    # Tight winner → single candidate; close race → return all (the
    # orchestrator will ask for disambiguation).
    if len(above) == 1 or (above[0][0] - above[1][0]) > 0.2:
        return [ResolvedRecipient(contact=above[0][1], matched_from="history")]
    return [ResolvedRecipient(contact=c, matched_from="history") for _, c in above]


def _contact_tokens(contact: Contact) -> set[str]:
    """All searchable tokens for a contact: name + bank + label + aliases."""
    bits = [contact.display_name, contact.bank]
    if contact.label:
        bits.append(contact.label)
    bits.extend(contact.aliases)
    tokens: set[str] = set()
    for b in bits:
        for tok in _fold(b).split():
            if tok and tok not in _STOP_TOKENS:
                tokens.add(tok)
    return tokens


def _dedupe(items: list[ResolvedRecipient]) -> list[ResolvedRecipient]:
    seen: set[str] = set()
    out: list[ResolvedRecipient] = []
    for r in items:
        if r.contact.id in seen:
            continue
        seen.add(r.contact.id)
        out.append(r)
    return out


def filter_by_account_hint(
    candidates: list[ResolvedRecipient], hint: str
) -> list[ResolvedRecipient]:
    if not hint:
        return candidates
    digits = re.sub(r"\D", "", hint)
    if not digits:
        return candidates
    keep = [
        r
        for r in candidates
        if _account_matches_hint(r.contact.account_number, digits)
    ]
    return keep


def resolve_by_account_hint(
    hint: str, contacts: list[Contact]
) -> list[ResolvedRecipient]:
    digits = re.sub(r"\D", "", hint)
    if not digits:
        return []
    return [
        ResolvedRecipient(contact=c, matched_from="exact")
        for c in contacts
        if _account_matches_hint(c.account_number, digits)
    ]


def _account_matches_hint(account_number: str, digits: str) -> bool:
    if len(digits) >= 6:
        return account_number == digits
    return account_number.endswith(digits)

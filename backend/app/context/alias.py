"""Alias / fuzzy-name resolution.

Maps a surface form like "mẹ", "anh Minh", "Minh" to the user's contacts.
Returns all plausible candidates so the caller can decide whether to confirm
or ask for disambiguation.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from ..models.schemas import Contact, ResolvedRecipient


def _fold(s: str) -> str:
    n = unicodedata.normalize("NFKD", s)
    return (
        "".join(c for c in n if not unicodedata.combining(c))
        .lower()
        .replace("đ", "d")
        .strip()
    )


# Family/relational prefixes that should be stripped to match name tokens.
_RELATIONAL_PREFIXES = ("anh ", "chi ", "em ", "ban ", "co ", "chu ", "bac ", "ong ", "ba ")

# Possessive / vocative tail tokens — "mẹ tôi" / "mẹ mình" /
# "chị Lan ơi" / "anh Hùng nhé". None appear in any contact's display
# name or alias list, so leaving them in defeats the exact-alias match
# ("mẹ tôi" never hits the "mẹ" alias). Strip from the right.
_RELATIONAL_TAIL_TOKENS = {
    "toi", "minh", "em", "anh", "chi",
    "oi", "nhe", "nha", "nhi",
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
    """
    if not surface:
        return []
    query = _fold(surface)
    query_stripped = _strip_relational(query)

    # When the LLM didn't say, but the surface looks alias-shaped, treat
    # it as an alias query so the user's "bạn thân" → empty rather than
    # falling into name-token noise.
    if kind is None and _looks_like_alias_kind(query):
        kind = "alias"

    matches: list[ResolvedRecipient] = []

    # When kind is explicitly "alias", DO NOT fall through to name lookups.
    # User said "bạn thân" → if no alias matches, return []; the chat asks
    # again rather than guessing a random name.
    if kind == "alias":
        return _lookup_in_aliases(query, query_stripped, contacts)

    if kind == "name":
        return _lookup_in_names(query, query_stripped, contacts)

    # kind is None — try alias-first, then name. No semantic fallback.
    # That eliminates the noise class on "bạn thân" / "grabfood" / "cho Nam".
    matches = _lookup_in_aliases(query, query_stripped, contacts)
    if matches:
        return matches
    return _lookup_in_names(query, query_stripped, contacts)


def _lookup_in_aliases(
    query: str, query_stripped: str, contacts: list[Contact]
) -> list[ResolvedRecipient]:
    """Exact fold-match against any saved alias of any contact. Also
    accepts the stripped form so "anh Tuấn" matches alias "Tuấn".

    Also matches against ``contact.label`` (the kinship/relationship
    chip displayed in the contacts UI: "Mẹ", "Bạn thân", "Sếp"...).
    The seed data carries these on the contact row itself, not as
    separate alias rows, so "bạn thân" was previously returning [].
    """
    matches: list[ResolvedRecipient] = []
    for c in contacts:
        matched = False
        for alias in c.aliases:
            folded = _fold(alias)
            if folded == query or folded == query_stripped:
                matches.append(
                    ResolvedRecipient(
                        contact=c, via_alias=alias, matched_from="alias",
                    )
                )
                matched = True
                break
        if matched:
            continue
        # Fall through to label match — keeps the via_alias slot empty
        # because the user typed a label, not a stored alias.
        if c.label:
            folded_label = _fold(c.label)
            if folded_label == query or folded_label == query_stripped:
                matches.append(
                    ResolvedRecipient(
                        contact=c, via_alias=c.label, matched_from="alias",
                    )
                )
    return _dedupe(matches)


def _lookup_in_names(
    query: str, query_stripped: str, contacts: list[Contact]
) -> list[ResolvedRecipient]:
    """Match against display_name:
      1. Full-name fold exact ("Nguyễn Văn Minh") → matched_from="exact"
      2. Any TOKEN of display_name equals the query ("Minh" → multiple
         contacts) → matched_from="name"
    No prefix match, no embedding fallback — those returned noise.
    """
    matches: list[ResolvedRecipient] = []
    # Stage 1: full-name exact
    for c in contacts:
        folded = _fold(c.display_name)
        if folded == query or folded == query_stripped:
            matches.append(ResolvedRecipient(contact=c, matched_from="exact"))
    if matches:
        return _dedupe(matches)

    # Stage 2: token-exact (any whole token of display_name == query)
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

"""Alias / fuzzy-name resolution.

Maps a surface form like "mẹ", "anh Minh", "Minh" to the user's contacts.
Returns all plausible candidates so the caller can decide whether to confirm
or ask for disambiguation.
"""

from __future__ import annotations

import re
import unicodedata

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


def _strip_relational(folded: str) -> str:
    s = folded
    for p in _RELATIONAL_PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
    return s


def _last_token(folded_name: str) -> str:
    parts = folded_name.split()
    return parts[-1] if parts else folded_name


def resolve_recipient(
    surface: str, contacts: list[Contact]
) -> list[ResolvedRecipient]:
    if not surface:
        return []
    query = _fold(surface)
    query_stripped = _strip_relational(query)

    matches: list[ResolvedRecipient] = []

    # 1) Direct alias match (high precision)
    for c in contacts:
        for alias in c.aliases:
            if _fold(alias) == query or _fold(alias) == query_stripped:
                matches.append(
                    ResolvedRecipient(contact=c, via_alias=alias, matched_from="alias")
                )
                break
    if matches:
        return _dedupe(matches)

    # 2) Last-name token equality on display name
    #    e.g., "Minh" -> "Nguyễn Văn Minh" and "Trần Hoàng Minh"
    for c in contacts:
        folded_name = _fold(c.display_name)
        if _last_token(folded_name) == query_stripped or query_stripped in folded_name.split():
            matches.append(
                ResolvedRecipient(contact=c, matched_from="name")
            )
    if matches:
        return _dedupe(matches)

    # 3) Token prefix match on display name (e.g. "Min" → "Minh")
    #    Match must align to a word boundary so "anh" doesn't sneak into
    #    "Hạnh" via raw substring.
    for c in contacts:
        for token in _fold(c.display_name).split():
            if query_stripped and token.startswith(query_stripped):
                matches.append(ResolvedRecipient(contact=c, matched_from="name"))
                break
    if matches:
        return _dedupe(matches)

    # 4) Whole-token match within an alias (e.g. "anh" matches alias
    #    "anh tuấn" but not "hạnh" / "chị bích").
    for c in contacts:
        for alias in c.aliases:
            if not query_stripped:
                continue
            alias_tokens = _fold(alias).split()
            if query_stripped in alias_tokens:
                matches.append(
                    ResolvedRecipient(contact=c, via_alias=alias, matched_from="alias")
                )
                break
    if matches:
        return _dedupe(matches)

    # 5) Semantic-ish fallback — only when every literal pass has failed.
    #    Tries Gemini embeddings first (when the key has access); falls
    #    through to a lexical token-overlap scorer that runs in pure
    #    Python with no network. Both yield a list of candidates the
    #    orchestrator can confirm or ambiguate.
    rag = _embedding_match(surface, contacts) or _lexical_match(surface, contacts)
    return _dedupe(rag)


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
    keep = [r for r in candidates if digits in r.contact.account_number]
    return keep or candidates

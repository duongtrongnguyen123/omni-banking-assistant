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

    # 3) Substring match on full name (lower precision)
    for c in contacts:
        folded_name = _fold(c.display_name)
        if query_stripped in folded_name:
            matches.append(ResolvedRecipient(contact=c, matched_from="name"))
    if matches:
        return _dedupe(matches)

    # 4) Alias substring (e.g., "anh Minh" -> alias "minh")
    for c in contacts:
        for alias in c.aliases:
            if query_stripped and query_stripped in _fold(alias):
                matches.append(
                    ResolvedRecipient(contact=c, via_alias=alias, matched_from="alias")
                )
                break

    return _dedupe(matches)


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

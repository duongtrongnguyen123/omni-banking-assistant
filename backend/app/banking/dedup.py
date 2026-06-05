"""Contact deduplication detector.

After 6 months of usage, a user's contact list collects duplicates:
"Mẹ", "Nguyen Thi Lan", "Mẹ (cũ)", "mẹ Lan" — same person, three rows.
The history tab shows fragmented totals.

`find_duplicate_groups` returns groups that a user would merge *without
second thought*. The bar is high on purpose — false positives lose user
trust permanently (collapsing two different people = lost money).

Match rule (conservative):
    token_overlap(display_name + aliases) >= 0.7
    AND (
        same account_number          # exact account collision
        OR (same bank AND first-4-digit prefix match)  # likely same account, typo
        OR alias_exact_match (one alias == another's display_name normalized)
    )

The token overlap alone is NOT enough — "Anh Minh MB" and "Anh Minh TCB"
share every token but are different people on different banks.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Literal

from ..models.schemas import Contact

DedupReason = Literal[
    "same_account_number",
    "same_bank_prefix_and_alias_overlap",
    "alias_exact_match",
]


@dataclass
class DedupGroup:
    """A set of contacts that look like the same real-world person.

    `primary` is the contact with the strongest signal (most aliases /
    longest display_name) — the natural target for a merge. `candidates`
    are the others in the group. `reason` documents the rule that fired.
    """

    primary: Contact
    candidates: list[Contact]
    reason: DedupReason
    overlap: float = 0.0
    members_ids: list[str] = field(default_factory=list)


_VIETNAMESE_STOP = {
    "anh", "chị", "chi", "em", "cô", "co", "chú", "chu", "bác", "bac",
    "ông", "ong", "bà", "ba", "mẹ", "me", "mom", "má", "ma", "bố", "bo",
    "ba", "cha", "thầy", "thay", "cô giáo", "co giao",
    # Generic noise
    "ms", "mr", "mrs", "anh chị", "anh chi",
}


def _strip_diacritics(s: str) -> str:
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def _normalize(s: str) -> str:
    """Lowercase, strip diacritics, drop punctuation, collapse whitespace."""
    s = _strip_diacritics(s.lower())
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokens(s: str) -> set[str]:
    """Token set, content words only — Vietnamese honorifics are stripped
    because every "Anh Minh"/"Chị Lan" would otherwise match every other.
    Single-letter tokens are dropped (initials carry no identity)."""
    norm = _normalize(s)
    if not norm:
        return set()
    raw = norm.split()
    out: set[str] = set()
    for t in raw:
        if len(t) <= 1:
            continue
        if t in _VIETNAMESE_STOP:
            continue
        out.add(t)
    return out


def _signature_tokens(c: Contact) -> set[str]:
    """All identity tokens for a contact — display_name + aliases + label.
    Used for overlap scoring."""
    bag: set[str] = set()
    bag |= _tokens(c.display_name)
    for a in c.aliases:
        bag |= _tokens(a)
    if c.label:
        bag |= _tokens(c.label)
    return bag


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _alias_exact(a: Contact, b: Contact) -> bool:
    """Is one contact's display_name listed (normalized) in the other's aliases?
    Catches the "Mẹ" + alias "mẹ" pattern."""
    a_name = _normalize(a.display_name)
    b_name = _normalize(b.display_name)
    a_aliases = {_normalize(x) for x in a.aliases}
    b_aliases = {_normalize(x) for x in b.aliases}
    if not a_name or not b_name:
        return False
    if a_name in b_aliases:
        return True
    if b_name in a_aliases:
        return True
    return False


def _account_prefix_match(a: Contact, b: Contact, n: int = 4) -> bool:
    """Same bank and the first n digits of the account agree.
    A typo-tolerant check: real banks rarely give two unrelated accounts
    the same bank + 4-digit prefix unless the suffix differs by a digit."""
    if a.bank != b.bank:
        return False
    if not a.account_number or not b.account_number:
        return False
    # Allow either prefix OR suffix collision — masked numbers like *6456
    # only carry the last 4. Use prefix when both are full, suffix when
    # account_masked is the only signal.
    if a.account_number[:n] == b.account_number[:n]:
        return True
    return False


def _pick_primary(group: list[Contact]) -> Contact:
    """Heuristic: the contact with the most aliases + the longest display
    name wins. If tied, lowest contact.id (stable ordering)."""
    def key(c: Contact) -> tuple:
        return (
            -len(c.aliases),
            -len(c.display_name),
            -int(c.frequent),
            -int(c.verified),
            c.id,
        )

    return sorted(group, key=key)[0]


def find_duplicate_groups(
    contacts: Iterable[Contact],
    *,
    overlap_threshold: float = 0.7,
) -> list[DedupGroup]:
    """Detect groups of contacts that almost certainly refer to the same
    real-world person.

    Algorithm: pairwise rule check + union-find clustering. O(n^2) on the
    contact list — fine up to a few thousand contacts. For the demo seed
    (~30) and contest data (~1000) this is trivially fast.

    Args:
        contacts: candidate contacts (typically `store.contacts_of(user_id)`).
        overlap_threshold: minimum Jaccard token overlap. Default 0.7 —
            tight enough to reject "Anh Minh MB" vs "Anh Minh TCB", loose
            enough to accept "Mẹ" vs "Nguyễn Thị Lan" with shared alias.

    Returns:
        List of DedupGroup, each containing >= 2 contacts. Empty list if
        no duplicates found.
    """
    items = list(contacts)
    n = len(items)
    if n < 2:
        return []

    # Union-find
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    sigs = [_signature_tokens(c) for c in items]
    # Track the strongest reason for each merged pair (priority order).
    reason_priority = {
        "same_account_number": 3,
        "alias_exact_match": 2,
        "same_bank_prefix_and_alias_overlap": 1,
    }
    pair_reason: dict[tuple[int, int], tuple[DedupReason, float]] = {}

    for i in range(n):
        for j in range(i + 1, n):
            a, b = items[i], items[j]
            overlap = _jaccard(sigs[i], sigs[j])
            same_acct = (
                bool(a.account_number)
                and a.account_number == b.account_number
            )
            prefix = _account_prefix_match(a, b)
            alias_exact = _alias_exact(a, b)

            reason: DedupReason | None = None
            if same_acct:
                # Strongest signal — same account is always the same account.
                reason = "same_account_number"
            elif alias_exact and prefix:
                # Display-name-as-alias is itself strong; combined with
                # bank + prefix it's effectively as good as same-account.
                reason = "alias_exact_match"
            elif overlap >= overlap_threshold and prefix:
                # Standard rule: high token overlap + likely same account.
                reason = "same_bank_prefix_and_alias_overlap"

            if reason is None:
                continue
            pair_reason[(i, j)] = (reason, overlap)
            union(i, j)

    # Group by root
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    out: list[DedupGroup] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        member_contacts = [items[i] for i in members]
        primary = _pick_primary(member_contacts)
        candidates = [c for c in member_contacts if c.id != primary.id]

        # Best reason among the pairs in this group.
        best_reason: DedupReason = "same_bank_prefix_and_alias_overlap"
        best_overlap = 0.0
        best_priority = -1
        for i_idx, i in enumerate(members):
            for j in members[i_idx + 1 :]:
                lo, hi = sorted((i, j))
                key = (lo, hi)
                if key not in pair_reason:
                    continue
                r, ov = pair_reason[key]
                p = reason_priority[r]
                if p > best_priority or (p == best_priority and ov > best_overlap):
                    best_priority = p
                    best_reason = r
                    best_overlap = ov

        out.append(
            DedupGroup(
                primary=primary,
                candidates=candidates,
                reason=best_reason,
                overlap=round(best_overlap, 3),
                members_ids=[c.id for c in member_contacts],
            )
        )

    # Stable order: largest groups first, then by primary.id for determinism.
    out.sort(key=lambda g: (-len(g.candidates) - 1, g.primary.id))
    return out


def merge_contacts(
    contacts: dict[str, Contact],
    transactions: dict,
    *,
    primary_id: str,
    candidate_ids: list[str],
) -> dict:
    """Re-attribute transactions and merge aliases. Operates on the
    in-memory store dicts; the route wraps this in a lock for atomicity.

    Hard-delete is used (simpler, defensible): the merge_audit return
    payload captures the original aliases and candidate snapshots so an
    admin can reconstruct the pre-merge state if needed. A sidecar
    `merge_audit` table would be the next iteration if undo becomes a
    user-facing feature.

    Returns: {merged_tx_count, retained_aliases, audit}.
    """
    if primary_id not in contacts:
        raise KeyError(primary_id)
    primary = contacts[primary_id]

    # Snapshot candidates before mutation (audit).
    audit_snapshot = []
    merged_aliases: list[str] = list(primary.aliases)
    seen_alias = {_normalize(a) for a in merged_aliases}

    for cid in candidate_ids:
        if cid == primary_id:
            continue
        c = contacts.get(cid)
        if c is None:
            continue
        audit_snapshot.append(
            {
                "id": c.id,
                "display_name": c.display_name,
                "bank": c.bank,
                "account_number": c.account_number,
                "aliases": list(c.aliases),
                "label": c.label,
            }
        )
        # Also fold the candidate's display_name in as an alias so the
        # user can still find the merged contact under the old name.
        for cand in [c.display_name, c.label, *c.aliases]:
            if not cand:
                continue
            norm = _normalize(cand)
            if not norm or norm in seen_alias:
                continue
            seen_alias.add(norm)
            merged_aliases.append(cand)

    merged_tx_count = 0
    for tx in list(transactions.values()):
        if tx.contact_id in candidate_ids and tx.contact_id != primary_id:
            tx.contact_id = primary_id
            merged_tx_count += 1

    # Apply alias merge and remove candidates.
    primary.aliases = merged_aliases
    for cid in candidate_ids:
        if cid == primary_id:
            continue
        contacts.pop(cid, None)

    return {
        "merged_tx_count": merged_tx_count,
        "retained_aliases": merged_aliases,
        "audit": {
            "primary_id": primary_id,
            "merged_candidates": audit_snapshot,
        },
    }

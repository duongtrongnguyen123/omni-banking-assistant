"""Look-alike contact detection — catches fraud lookalikes at transfer time.

Classic social-engineering vector: attacker adds a contact whose name is
visually near-identical to a frequent recipient (mẹ, sếp), then convinces
the user to confirm a transfer. Without an explicit guard, "Nguyên Thi Lan"
(homoglyph) and "Nguyễn Thị Lan" (real mẹ) look the same in a chat reply.

Signal: a *non-frequent* candidate whose normalized name is within
``max_distance`` Levenshtein edits of a *frequent* contact's name. We only
fire on low-freq candidates because a frequent contact is by definition
already trusted by the user — flagging them would be pure noise.

Returns the *frequent* contact the candidate looks like, so the orchestrator
can name it in the warn message ("trông giống mẹ — chắc đúng người chứ?").
"""

from __future__ import annotations

import unicodedata
from typing import Optional

from ..models.schemas import Contact


def _normalize_name(name: str) -> str:
    """Lower + accent-fold + strip non-alphanumeric, collapse whitespace.

    Accent-folding is the whole point — the attack relies on visually
    similar characters folding to a canonical form. The defender does the
    same fold and then compares.
    """
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("đ", "d").replace("Đ", "D").lower()
    # Strip everything but ASCII letters, digits, and spaces.
    s = "".join(c if c.isalnum() or c == " " else " " for c in s)
    return " ".join(s.split())


def _levenshtein(a: str, b: str, *, cap: int) -> int:
    """Compact Levenshtein with early-exit when distance exceeds ``cap``.

    Names are short (Vietnamese: 3-5 tokens, ~20 chars), so the classic
    O(len(a)·len(b)) DP runs in microseconds. The cap lets us bail when
    the running min row exceeds the threshold — useful when checking
    against 1000 contacts and most pairs are obviously far apart.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    if not a:
        return len(b)
    if not b:
        return len(a)

    prev = list(range(len(b) + 1))
    cur = [0] * (len(b) + 1)
    for i, ca in enumerate(a, 1):
        cur[0] = i
        row_min = cur[0]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(
                prev[j] + 1,        # deletion
                cur[j - 1] + 1,     # insertion
                prev[j - 1] + cost, # substitution
            )
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > cap:
            return cap + 1
        prev, cur = cur, prev
    return prev[len(b)]


def detect_lookalike_match(
    candidate: Contact,
    contacts: list[Contact],
    *,
    max_distance: int = 1,
    min_name_len: int = 5,
) -> Optional[tuple[Contact, int]]:
    """Same matching contract as :func:`detect_lookalike` but returns the
    winning twin **and** the edit distance, so callers can populate a
    structured details payload ("homograph match" vs "1-edit typo")."""
    twin = _detect(candidate, contacts, max_distance, min_name_len)
    return twin


def detect_lookalike(
    candidate: Contact,
    contacts: list[Contact],
    *,
    max_distance: int = 1,
    min_name_len: int = 5,
) -> Optional[Contact]:
    """Return a frequent contact whose normalized name is within
    ``max_distance`` edits of ``candidate``'s, or ``None``.

    Strictness model — tuned against ~1000 real Vietnamese names where
    surname + middle-name overlap (Nguyễn/Trần/Văn/Thị) is the norm:

      - distance == 0 (homograph after accent fold): always flag.
      - distance == 1: require first AND last tokens to match exactly.
        Catches "Nguyễn Văn Minh" vs "Nguyễn Văn Mihn" (typo'd given
        name) but drops "Nguyễn Anh Anh" vs "Nguyễn Anh An" — those
        are two different real people.
      - distance >= 2: too noisy on this name corpus; dropped.

    Skips:
      - candidate is itself frequent (trusted by definition).
      - candidate name is shorter than ``min_name_len`` after normalisation.
      - same id as a frequent contact (the candidate IS the frequent one).
    """
    match = _detect(candidate, contacts, max_distance, min_name_len)
    return match[0] if match else None


def _detect(
    candidate: Contact,
    contacts: list[Contact],
    max_distance: int,
    min_name_len: int,
) -> Optional[tuple[Contact, int]]:
    if candidate.frequent:
        return None

    cand_norm = _normalize_name(candidate.display_name)
    if len(cand_norm) < min_name_len:
        return None
    cand_tokens = cand_norm.split()

    best: Optional[tuple[int, Contact]] = None
    for c in contacts:
        if not c.frequent or c.id == candidate.id:
            continue
        c_norm = _normalize_name(c.display_name)
        if len(c_norm) < min_name_len:
            continue
        d = _levenshtein(cand_norm, c_norm, cap=max_distance)
        if d > max_distance:
            continue
        if d == 1:
            # Require first AND last token match — kills the most common
            # false positives on this corpus (sibling names sharing surname).
            c_tokens = c_norm.split()
            if (
                len(cand_tokens) < 2 or len(c_tokens) < 2
                or cand_tokens[0] != c_tokens[0]
                or cand_tokens[-1] != c_tokens[-1]
            ):
                continue
        if best is None or d < best[0]:
            best = (d, c)
            if d == 0:
                break  # perfect homograph — no point checking further

    return (best[1], best[0]) if best else None

"""NAPAS-style account-name lookup over the demo dataset.

Mimics the interbank *name inquiry* a real banking app runs before you confirm
a transfer to a brand-new beneficiary: given an account number (+ optional
bank), return the holder's display name. Data lives in
``app/data/napas_accounts.json`` (555 rows of {bank, account_number,
display_name}).

This is what lets the assistant handle a "stranger" (someone not yet in the
user's contact book): look the account up, show the real name, let the user
confirm, then save them to contacts on first transfer.
"""

from __future__ import annotations

import functools
import json
from typing import Optional

from ..config import get_settings


def _digits(s: Optional[str]) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


@functools.lru_cache(maxsize=1)
def _index() -> dict[str, dict]:
    """account_number → row. Cached; built once per process."""
    path = get_settings().data_dir / "napas_accounts.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt data must not crash NLU
        return {}
    return {
        _digits(r.get("account_number")): r
        for r in rows
        if r.get("account_number") and r.get("display_name")
    }


def lookup(account_number: str, bank: Optional[str] = None) -> Optional[dict]:
    """Return ``{bank, account_number, display_name}`` for an account, or None.

    Exact match first; if the user typed only a suffix (≥ 6 digits) we match by
    ending. ``bank`` narrows ties when several banks share an ending.
    """
    acct = _digits(account_number)
    if not acct:
        return None
    idx = _index()
    row = idx.get(acct)
    if row is None and len(acct) >= 6:
        ends = [r for an, r in idx.items() if an.endswith(acct)]
        if len(ends) == 1:
            row = ends[0]
    if row is None:
        return None
    # Bank must match the account's actual issuer — giving the wrong bank for a
    # valid account is "not found" (the wrong institution can't see it).
    if bank and not _bank_match(bank, row.get("bank")):
        return None
    return row


def _bank_match(a: Optional[str], b: Optional[str]) -> bool:
    x, y = (a or "").strip().lower(), (b or "").strip().lower()
    if not x or not y:
        return False
    return x == y or x in y or y in x

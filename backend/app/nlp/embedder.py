"""Batch embedder — fill missing embeddings in the SQLite contacts and
transactions tables. Idempotent: rows that already have an embedding are
skipped. Safe to call on every startup and after bulk inserts.
"""

from __future__ import annotations

import logging

from ..db.connection import get_connection
from .embeddings import embed, pack

log = logging.getLogger("omni.nlp.embed")


def _contact_text(row) -> str:
    """The text we embed for a contact. Concatenate the display name,
    aliases, and label so semantic queries can hit any of those signals."""
    aliases = [
        r["alias"] for r in get_connection().execute(
            "SELECT alias FROM contact_aliases WHERE contact_id = ?",
            (row["id"],),
        ).fetchall()
    ]
    bits = [row["display_name"], row["bank"]]
    if row["label"]:
        bits.append(row["label"])
    bits.extend(aliases)
    return " · ".join(bits)


def _transaction_text(row) -> str:
    return f"{row['description']} ({row['category']})".strip()


def _fill(table: str, text_fn) -> int:
    conn = get_connection()
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE embedding IS NULL"
    ).fetchall()
    filled = 0
    for row in rows:
        text = text_fn(row)
        if not text.strip():
            continue
        vec = embed(text)
        if vec is None:
            # No API key or rate-limited; stop trying this run.
            return filled
        conn.execute(
            f"UPDATE {table} SET embedding = ? WHERE id = ?",
            (pack(vec), row["id"]),
        )
        filled += 1
    return filled


def fill_missing_embeddings() -> dict:
    contacts = _fill("contacts", _contact_text)
    transactions = _fill("transactions", _transaction_text)
    return {"contacts": contacts, "transactions": transactions}

"""Bootstrap the SQLite database from the JSON seed files on first run.

Idempotent — re-running is a no-op for any table that already has rows.
This lets developers wipe omni.db and get the seed back, while preserving
any mutations (new contacts, executed transfers) across restarts.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..config import get_settings
from ..context.alias import _fold
from .connection import get_connection


def _read_json(name: str) -> list[dict]:
    path: Path = get_settings().data_dir / name
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _table_count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


def _parse_dt(s: str | datetime) -> str:
    if isinstance(s, datetime):
        return s.isoformat()
    # Round-trip through Pydantic's datetime parser for tz-aware ISO strings
    return datetime.fromisoformat(s).isoformat()


def bootstrap_if_empty() -> None:
    conn = get_connection()
    cur = conn.cursor()

    if _table_count(conn, "users") > 0:
        return  # already bootstrapped

    cur.execute("BEGIN")
    try:
        for u in _read_json("users.json"):
            cur.execute(
                "INSERT OR IGNORE INTO users(id, display_name, phone) VALUES(?,?,?)",
                (u["id"], u["display_name"], u.get("phone", "")),
            )
            for acc in u.get("accounts", []):
                cur.execute(
                    """INSERT OR IGNORE INTO accounts
                       (id, user_id, bank, number, balance, currency, is_primary)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        acc["id"],
                        u["id"],
                        acc["bank"],
                        acc["number"],
                        acc["balance"],
                        acc.get("currency", "VND"),
                        1 if acc.get("primary") else 0,
                    ),
                )

        for c in _read_json("contacts.json"):
            cur.execute(
                """INSERT OR IGNORE INTO contacts
                   (id, owner_id, display_name, bank, account_number,
                    account_masked, label, verified, frequent)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    c["id"], c["owner_id"], c["display_name"], c["bank"],
                    c["account_number"], c["account_masked"], c.get("label"),
                    1 if c.get("verified") else 0,
                    1 if c.get("frequent") else 0,
                ),
            )
            for alias in c.get("aliases", []):
                cur.execute(
                    """INSERT OR IGNORE INTO contact_aliases
                       (contact_id, alias, alias_normalized) VALUES(?,?,?)""",
                    (c["id"], alias, _fold(alias)),
                )

        for t in _read_json("transactions.json"):
            cur.execute(
                """INSERT OR IGNORE INTO transactions
                   (id, owner_id, contact_id, amount, description, category,
                    status, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    t["id"], t["owner_id"], t.get("contact_id"),
                    t["amount"], t.get("description", ""),
                    t.get("category", "other"),
                    t.get("status", "completed"),
                    _parse_dt(t["created_at"]),
                ),
            )

        for s in _read_json("schedules.json"):
            cur.execute(
                """INSERT OR IGNORE INTO schedules
                   (id, owner_id, contact_id, source_account_id, amount,
                    description, cron, next_run, active)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    s["id"], s["owner_id"], s["contact_id"],
                    s.get("source_account_id"),
                    s["amount"], s.get("description", ""),
                    s["cron"], _parse_dt(s["next_run"]),
                    1 if s.get("active", True) else 0,
                ),
            )

        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise

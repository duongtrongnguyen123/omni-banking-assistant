"""Convert the full 521K-row contest CSV into a SQLite DB for eval.

Unlike data/transform_banking_dataset.py's *demo* subset (which capped
each contact at 80 tx for a tractable UI seed), this script loads every
outgoing transaction so the suggester eval has real volume to chew on.

Schema is mapped 1:1 to backend/app/db/schema.sql. Date shift mirrors
the demo metadata so "today" sits at the end of the data.

Writes to ``backend/app/data/omni_contest.db``. Point env vars at it:

    OMNI_DB_PATH=app/data/omni_contest.db .venv/bin/python scripts/eval_suggester.py
"""

from __future__ import annotations

import csv
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = ROOT.parent / "generated" / "transactions_enriched_6m.csv"
DB = ROOT / "app" / "data" / "omni_contest.db"
SCHEMA = ROOT / "app" / "db" / "schema.sql"
USER = "u_an"
# Source max is 2025-06-29; align to 2026-06-02 to match the demo shift.
SHIFT = timedelta(days=338)

# 1) Reset DB + apply schema
DB.unlink(missing_ok=True)
for suffix in ("-wal", "-shm"):
    (DB.parent / f"{DB.name}{suffix}").unlink(missing_ok=True)

conn = sqlite3.connect(str(DB), isolation_level=None)
conn.execute("PRAGMA journal_mode = WAL")
with SCHEMA.open() as f:
    conn.executescript(f.read())

# 2) Insert demo user
conn.execute(
    "INSERT INTO users(id, display_name, phone) VALUES (?,?,?)",
    (USER, "Nguyễn Hoàng An", "0912345678"),
)
conn.execute(
    """INSERT INTO accounts(id, user_id, bank, number, balance, currency, is_primary)
       VALUES (?,?,?,?,?,?,?)""",
    ("acc_an_main", USER, "Omni Bank", "1234567890", 24_350_000, "VND", 1),
)

# 3) Stream-process the CSV
contacts: dict[str, str] = {}  # cif → contact_id
tx_buffer: list[tuple] = []
BATCH = 10_000

print(f"Reading {SRC.name}…")
conn.execute("BEGIN")
with SRC.open(encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    n = 0
    for row in reader:
        if row["direction"] != "outgoing":
            continue
        if row["sender_id"] != USER:
            continue
        cif = row["source_cif_no"]
        if cif not in contacts:
            cid = f"c_{cif}"
            contacts[cif] = cid
            conn.execute(
                """INSERT INTO contacts(id, owner_id, display_name, bank,
                   account_number, account_masked, label, verified, frequent)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    cid, USER, row["counterparty_name"], row["counterparty_bank"],
                    row["counterparty_account_number"],
                    "*" + row["counterparty_account_number"][-3:],
                    None, 1, 0,
                ),
            )
        when = datetime.fromisoformat(row["transaction_at"]) + SHIFT
        tx_buffer.append((
            row["transaction_id"], USER, contacts[cif],
            abs(int(row["amount_vnd"])),
            row["note_normalized"], row["category"],
            row["status"], when.isoformat(),
        ))
        if len(tx_buffer) >= BATCH:
            conn.executemany(
                """INSERT INTO transactions(id, owner_id, contact_id, amount,
                   description, category, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                tx_buffer,
            )
            n += len(tx_buffer)
            tx_buffer = []
            if n % 50_000 == 0:
                print(f"  …{n:,} tx inserted")

if tx_buffer:
    conn.executemany(
        """INSERT INTO transactions(id, owner_id, contact_id, amount,
           description, category, status, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        tx_buffer,
    )
    n += len(tx_buffer)
conn.execute("COMMIT")

c_count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
print()
print(f"  contacts:     {c_count:,}")
print(f"  transactions: {n:,}")
print(f"  db:           {DB} ({DB.stat().st_size // 1024 // 1024} MB)")
print()
print(f"Run eval:")
print(f"  OMNI_DB_PATH={DB} OMNI_SKIP_EMBED_BACKFILL=1 \\")
print(f"  .venv/bin/python scripts/eval_suggester.py")

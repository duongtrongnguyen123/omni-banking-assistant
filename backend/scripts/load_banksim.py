"""Load the BankSim dataset into our SQLite schema.

BankSim (kaggle: ealaxi/banksim1) is a 594k-row synthetic-yet-realistic
payment dataset with labeled fraud (7,200 fraud cases) used widely in
fraud-detection literature. Schema:

    step, customer, age, gender, zipcodeOri, merchant, zipMerchant,
    category, amount, fraud

Mapping into our model:

  * customer  →  Omni `user`            (only the top-N most active are
                                         instantiated as full users to
                                         keep memory reasonable)
  * merchant  →  Omni `contact` per user (each merchant becomes a
                                          per-user contact row)
  * step (0..179) → synthetic `created_at`. We anchor step=0 at
                    2024-01-01 and add `step` days. The day-of-month
                    distribution this produces is real enough to test
                    the suggester's date features.
  * fraud (0/1) → kept in a SEPARATE column on transactions
                  (`is_fraud`) — out-of-band label, never read by app
                  code, only by `eval_fraud_banksim.py`.

We pick the TOP `USER_LIMIT` customers by tx count so the per-user
history is rich enough to evaluate the suggester (Hit@K is meaningless
when a user only has 2 transactions). The fraud eval uses ALL rows
(capped via `--limit`) because Isolation Forest is unsupervised across
users.

Output: `backend/app/data/omni_banksim.db` (gitignored).
"""

from __future__ import annotations

import csv
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CSV_PATH = ROOT.parent / "data" / "public" / "banksim" / "bs140513_032310.csv"
DB = ROOT / "app" / "data" / "omni_banksim.db"
SCHEMA = ROOT / "app" / "db" / "schema.sql"

# How many of the most-active customers to instantiate as Omni users.
# The suggester eval is per-user so we need enough tx per user; 50 lets
# us cover ~10k tx with rich per-user histories.
USER_LIMIT = 50
USER_MIN_TX = 50    # skip thinly populated users below this threshold

# Day-zero anchor — picked so the resulting calendar overlaps a realistic
# range of weekdays / month boundaries for the suggester's features.
EPOCH = datetime(2024, 1, 1)


def _strip_quotes(s: str) -> str:
    return s.strip().strip("'")


def main() -> None:
    if not CSV_PATH.exists():
        print(f"Missing {CSV_PATH}", file=sys.stderr)
        print(
            "Download with:\n"
            "  mkdir -p data/public/banksim && cd data/public/banksim\n"
            "  curl -L -sS -o bs140513_032310.csv \\\n"
            "    https://raw.githubusercontent.com/atavci/fraud-detection-on-"
            "banksim-data/master/Data/synthetic-data-from-a-financial-payment-"
            "system/bs140513_032310.csv",
            file=sys.stderr,
        )
        sys.exit(1)

    DB.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        (DB.parent / f"{DB.name}{suffix}").unlink(missing_ok=True)

    conn = sqlite3.connect(str(DB), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    with SCHEMA.open() as f:
        conn.executescript(f.read())
    # Add the fraud flag column without polluting the base schema.
    conn.execute("ALTER TABLE transactions ADD COLUMN is_fraud INTEGER NOT NULL DEFAULT 0")

    # ----------------------------------------------------------------- pass 1
    # Count tx per customer to pick our user pool.
    print("Pass 1: counting customers…")
    cust_count: Counter = Counter()
    with CSV_PATH.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cust_count[_strip_quotes(row["customer"])] += 1
    top_users = [c for c, n in cust_count.most_common() if n >= USER_MIN_TX][:USER_LIMIT]
    user_set = set(top_users)
    print(f"  total customers: {len(cust_count):,}")
    print(f"  selected:        {len(top_users)} with ≥{USER_MIN_TX} tx each")

    # ----------------------------------------------------------------- users
    for cust in top_users:
        uid = f"u_bs_{cust}"
        conn.execute(
            "INSERT INTO users(id, display_name, phone) VALUES (?,?,?)",
            (uid, f"BankSim {cust}", f"BS-{cust}"),
        )
        conn.execute(
            "INSERT INTO accounts(id, user_id, bank, number, balance, currency, is_primary) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"acc_bs_{cust}", uid, "BankSim Bank", cust, 10_000_000, "EUR", 1),
        )

    # ----------------------------------------------------------------- pass 2
    # Stream transactions. Build per-user contacts on first encounter.
    print("Pass 2: loading transactions…")
    contacts_for: dict[str, dict[str, str]] = {u: {} for u in user_set}
    seen_total = 0
    selected_total = 0
    fraud_total = 0
    BATCH = 5_000
    buf: list[tuple] = []
    conn.execute("BEGIN")
    with CSV_PATH.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            seen_total += 1
            cust = _strip_quotes(row["customer"])
            if cust not in user_set:
                continue
            uid = f"u_bs_{cust}"
            merchant = _strip_quotes(row["merchant"])
            cat = row["category"].strip().strip("'") or "other"

            cid_map = contacts_for[cust]
            cid = cid_map.get(merchant)
            if cid is None:
                cid = f"c_bs_{cust}_{len(cid_map)}"
                cid_map[merchant] = cid
                conn.execute(
                    "INSERT INTO contacts(id, owner_id, display_name, bank, "
                    "account_number, account_masked, label, verified, frequent) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (cid, uid, merchant, "BankSim", merchant,
                     "*" + merchant[-4:], cat, 1, 0),
                )

            step = int(row["step"])
            when = EPOCH + timedelta(days=step)
            # Spread by hour so per-day tx aren't identical timestamps —
            # tx[seen_total % 24] gives a deterministic spread.
            when = when.replace(hour=(seen_total * 13) % 24,
                                minute=(seen_total * 7) % 60)
            amount = int(round(float(row["amount"]) * 100))
            fraud = 1 if row["fraud"].strip() == "1" else 0
            fraud_total += fraud

            buf.append((
                f"tx_bs_{seen_total}", uid, cid, amount,
                f"{merchant} {cat}", cat, "completed",
                when.isoformat(), fraud,
            ))
            selected_total += 1
            if len(buf) >= BATCH:
                conn.executemany(
                    "INSERT INTO transactions(id, owner_id, contact_id, amount, "
                    "description, category, status, created_at, is_fraud) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    buf,
                )
                buf.clear()
    if buf:
        conn.executemany(
            "INSERT INTO transactions(id, owner_id, contact_id, amount, "
            "description, category, status, created_at, is_fraud) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            buf,
        )
    conn.execute("COMMIT")

    n_contacts = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    n_tx = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    n_fraud = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE is_fraud=1"
    ).fetchone()[0]
    print()
    print(f"  CSV rows scanned:   {seen_total:,}")
    print(f"  users:              {len(top_users)}")
    print(f"  contacts:           {n_contacts:,}")
    print(f"  transactions kept:  {n_tx:,}")
    print(f"  fraud rows kept:    {n_fraud:,} "
          f"({100*n_fraud/max(n_tx,1):.2f}%)")
    print(f"  db: {DB} ({DB.stat().st_size // 1024 // 1024} MB)")


if __name__ == "__main__":
    main()

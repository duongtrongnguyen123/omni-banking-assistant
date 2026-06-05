"""Load the Czech PKDD'99 financial dataset into a SQLite DB.

The dataset (mirror: github.com/dnoeth/1999_Czech_financial_dataset_Teradata)
contains 1.05M real anonymised transactions from a Czech bank, 1993-1998
(GitHub mirror shifts dates +20y so the data spans 2013-2018). For us
it's gold because the bank's own ``fin_order`` table is *ground truth
for recurring payments* — that's exactly what `app/banking/recurring.py`
tries to mine.

What this script does
---------------------
1. Reads `data/public/czech_pkdd99/*.tsv` (download via
   `data/public/README.md` if missing — script aborts with instructions).
2. Picks the 5 most-active accounts (most permanent orders + most
   outgoing tx) as "demo users" so the eval set fits memory.
3. Maps every outgoing transaction (`trans_type IN ('D','P')`) for those
   users to our `transactions` schema. We derive `contact_id` from:
     * `other_account_id` when present (real remittance recipient), OR
     * a synthetic "cash/<category>" pseudo-contact for cash withdrawals
       (the recurring detector keys on (contact_id, description) so this
       lets HH/IN/LO-but-paid-in-cash patterns still show up).
4. Mirrors `fin_order` (per-account permanent orders) into a separate
   `permanent_orders` table — never used by app code, just compared
   against by `eval_recurring_czech.py`.

Output: `backend/app/data/omni_czech.db` (gitignored).
"""

from __future__ import annotations

import csv
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA = ROOT.parent / "data" / "public" / "czech_pkdd99"
DB = ROOT / "app" / "data" / "omni_czech.db"
SCHEMA = ROOT / "app" / "db" / "schema.sql"
USER_COUNT = 5

REQUIRED_FILES = [
    "fin_account.tsv",
    "fin_client.tsv",
    "fin_disp.tsv",
    "fin_order.tsv",
    "fin_trans.tsv",
]


def _check_files() -> None:
    missing = [f for f in REQUIRED_FILES if not (DATA / f).exists()]
    if missing:
        print(f"Missing files in {DATA}: {missing}", file=sys.stderr)
        print(
            "Download with:\n"
            "  mkdir -p data/public/czech_pkdd99 && cd data/public/czech_pkdd99\n"
            "  BASE=https://raw.githubusercontent.com/dnoeth/"
            "1999_Czech_financial_dataset_Teradata/master\n"
            "  for f in fin_account.tsv fin_client.tsv fin_disp.tsv "
            "fin_order.tsv fin_trans.tsv; do\n"
            "      curl -L -sS -o \"$f\" \"$BASE/$f\"; done",
            file=sys.stderr,
        )
        sys.exit(1)


def _read_tsv(path: Path):
    with path.open(encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f, delimiter="\t"):
            yield row


def main() -> None:
    _check_files()

    DB.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        (DB.parent / f"{DB.name}{suffix}").unlink(missing_ok=True)

    conn = sqlite3.connect(str(DB), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    with SCHEMA.open() as f:
        conn.executescript(f.read())
    # Ground-truth recurring orders table — read-only reference for eval.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS permanent_orders (
            order_id     INTEGER PRIMARY KEY,
            owner_id     TEXT NOT NULL,
            account_id   INTEGER NOT NULL,
            bank_to      TEXT,
            account_to   INTEGER,
            amount       INTEGER NOT NULL,   -- VND-style integer, *100 of original
            category     TEXT,
            contact_id   TEXT NOT NULL       -- matches contacts.id we synthesised
        );
        CREATE INDEX IF NOT EXISTS ix_porders_owner ON permanent_orders(owner_id);
        """
    )

    # ----------------------------------------------------------------- pick users
    # Score each account: tx count + permanent-order count
    print("Scoring accounts to pick demo users…")
    order_count_by_acc: Counter = Counter()
    for row in _read_tsv(DATA / "fin_order.tsv"):
        # order_id, account_id, bank_to, account_to, amount, category
        order_count_by_acc[int(row[1])] += 1

    tx_count_by_acc: Counter = Counter()
    for row in _read_tsv(DATA / "fin_trans.tsv"):
        # trans_id, account_id, trans_date, amount, balance, type, op, cat, bank, other_acc
        if len(row) < 10:
            continue
        if row[5] in ("D", "P"):  # debit / cash withdrawal
            tx_count_by_acc[int(row[1])] += 1

    accounts_scored = [
        (acc, tx_count_by_acc.get(acc, 0) + 50 * order_count_by_acc.get(acc, 0))
        for acc in order_count_by_acc
    ]
    accounts_scored.sort(key=lambda kv: -kv[1])
    chosen_accounts = [a for a, _ in accounts_scored[:USER_COUNT]]
    print(f"  picked accounts: {chosen_accounts}")

    # Map each account_id → omni user_id
    user_for: dict[int, str] = {acc: f"u_cz_{acc}" for acc in chosen_accounts}

    # ----------------------------------------------------------------- users
    # Read client/disp to get the OWNER client of each chosen account.
    client_birth: dict[int, str] = {}
    for row in _read_tsv(DATA / "fin_client.tsv"):
        if len(row) >= 3:
            client_birth[int(row[0])] = row[1]
    owner_client: dict[int, int] = {}
    for row in _read_tsv(DATA / "fin_disp.tsv"):
        # disp_id, client_id, account_id, disp_type
        if len(row) < 4:
            continue
        acc = int(row[2])
        if row[3] == "O" and acc in user_for:
            owner_client[acc] = int(row[1])

    for acc in chosen_accounts:
        uid = user_for[acc]
        conn.execute(
            "INSERT INTO users(id, display_name, phone) VALUES (?,?,?)",
            (uid, f"Czech demo account #{acc}", f"+420{acc:08d}"),
        )
        conn.execute(
            "INSERT INTO accounts(id, user_id, bank, number, balance, currency, is_primary) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"acc_cz_{acc}", uid, "CZ Demo Bank", str(acc), 1_000_000, "CZK", 1),
        )

    # ----------------------------------------------------------------- contacts + orders
    # We synthesise one contact per (owner, normalised contact key):
    #   * remittance ROB    →  contact key = f"acc_{account_to}_{bank_to}"
    #   * cash WIC/CCW      →  contact key = f"cash_{category or 'misc'}"
    #   * collection COB    →  contact key = f"col_{other_account_id}"
    # Permanent orders use ("acc_{account_to}_{bank_to}", category).

    contacts_for_owner: dict[str, dict[str, str]] = defaultdict(dict)

    def get_or_create_contact(owner: str, key: str, display: str, bank: str) -> str:
        existing = contacts_for_owner[owner].get(key)
        if existing:
            return existing
        cid = f"c_{owner[2:]}_{len(contacts_for_owner[owner])}"
        contacts_for_owner[owner][key] = cid
        conn.execute(
            "INSERT INTO contacts(id, owner_id, display_name, bank, "
            "account_number, account_masked, label, verified, frequent) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, owner, display, bank, key, "*" + key[-4:], None, 1, 0),
        )
        return cid

    # --- permanent_orders: writes BOTH the omni ground-truth row AND ensures
    # the contact exists (so the eval can match by contact_id directly).
    print("Loading fin_order.tsv (permanent orders, ground truth)…")
    n_orders = 0
    for row in _read_tsv(DATA / "fin_order.tsv"):
        if len(row) < 6:
            continue
        order_id, acc_s, bank_to, account_to, amount, category = row[:6]
        acc = int(acc_s)
        if acc not in user_for:
            continue
        owner = user_for[acc]
        key = f"acc_{account_to.strip()}_{bank_to.strip()}"
        display = f"Other-bank acc {account_to.strip()} ({bank_to.strip()})"
        cid = get_or_create_contact(owner, key, display, bank_to.strip() or "Other")
        cat = category.strip()
        conn.execute(
            "INSERT INTO permanent_orders(order_id, owner_id, account_id, "
            "bank_to, account_to, amount, category, contact_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (int(order_id), owner, acc, bank_to.strip(),
             int(account_to), int(float(amount) * 100), cat, cid),
        )
        n_orders += 1
    print(f"  permanent_orders: {n_orders}")

    # ----------------------------------------------------------------- transactions
    print("Loading fin_trans.tsv (transactions)…")
    BATCH = 5_000
    buf: list[tuple] = []
    conn.execute("BEGIN")
    n_tx = 0
    skipped_dir = 0
    seen_user = Counter()
    for i, row in enumerate(_read_tsv(DATA / "fin_trans.tsv")):
        if len(row) < 10:
            continue
        trans_id, acc_s, dt_s, amount_s, balance_s, t_type, op, cat, bank, other_acc = row[:10]
        acc = int(acc_s)
        if acc not in user_for:
            continue
        # We only keep outgoing tx — debit ("D") and cash withdrawal ("P").
        if t_type not in ("D", "P"):
            skipped_dir += 1
            continue

        owner = user_for[acc]
        bank = bank.strip()
        other_acc = other_acc.strip()
        cat = cat.strip() or "other"
        op = op.strip()

        if op == "ROB" and other_acc:
            key = f"acc_{other_acc}_{bank}"
            display = f"Other-bank acc {other_acc} ({bank or '??'})"
            contact_bank = bank or "Other"
        elif op == "COB" and other_acc:
            key = f"col_{other_acc}_{bank}"
            display = f"Collection from {other_acc} ({bank or '??'})"
            contact_bank = bank or "Other"
        else:
            # Cash withdrawal or untagged debit. Bucket by category so the
            # detector can still recognise "monthly HH cash" patterns.
            bucket = cat if cat != "other" else "cash"
            key = f"cash_{bucket}"
            display = f"Cash withdrawal ({bucket})"
            contact_bank = "Cash"

        cid = get_or_create_contact(owner, key, display, contact_bank)

        # Use the operation/category as description so the recurring
        # detector groups consistent payments together. Without a
        # description it would fall under our noise filter.
        desc = f"{op or 'OUT'} {cat}".strip()
        try:
            created = datetime.fromisoformat(dt_s).isoformat()
        except ValueError:
            continue
        # Czech trans amounts are signed (debit = negative). Omni convention
        # stores outgoing amount as a POSITIVE int and infers direction from
        # the schema's `direction` (we only keep `D`/`P` here, so direction
        # is implicitly outgoing). Multiply by 100 to make whole-int VND-like.
        amount_int = abs(int(round(float(amount_s) * 100)))

        buf.append((
            f"tx_cz_{trans_id}", owner, cid, amount_int, desc, cat,
            "completed", created,
        ))
        seen_user[owner] += 1
        if len(buf) >= BATCH:
            conn.executemany(
                "INSERT INTO transactions(id, owner_id, contact_id, amount, "
                "description, category, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                buf,
            )
            n_tx += len(buf)
            buf.clear()
            if n_tx % 50_000 == 0:
                print(f"  …{n_tx:,}")
    if buf:
        conn.executemany(
            "INSERT INTO transactions(id, owner_id, contact_id, amount, "
            "description, category, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            buf,
        )
        n_tx += len(buf)
    conn.execute("COMMIT")

    print()
    print(f"  users:             {len(chosen_accounts)}")
    print(f"  contacts:          {sum(len(v) for v in contacts_for_owner.values()):,}")
    print(f"  outgoing tx:       {n_tx:,}")
    print(f"  permanent_orders:  {n_orders:,}")
    print(f"  per-user tx:")
    for owner, n in seen_user.most_common():
        print(f"    {owner}: {n:,}")
    print(f"  db: {DB} ({DB.stat().st_size // 1024 // 1024} MB)")
    print()
    print("Run eval:")
    print(f"  OMNI_DB_PATH={DB} OMNI_SKIP_EMBED_BACKFILL=1 \\")
    print("  .venv/bin/python scripts/eval_recurring_czech.py")


if __name__ == "__main__":
    main()

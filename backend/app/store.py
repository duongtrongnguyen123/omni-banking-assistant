"""SQLite-backed Store.

Preserves the exact interface of the previous in-memory Store so the
orchestrator + banking + routes layers don't need to change. The DB file
lives at ``backend/app/data/omni.db`` and is bootstrapped from the JSON
seed on first run (see ``db/bootstrap.py``). Mutations now survive
restarts — adds, transfers, schedules, contact saves all persist.

Performance: indexed lookups on ``owner_id`` for contacts/transactions
and on the normalised alias for fuzzy match make this scale to ~100k
transactions / 10k contacts per user without anything special.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Optional

from .db.bootstrap import bootstrap_if_empty
from .db.connection import get_connection
from .models.schemas import Account, Contact, Schedule, Transaction, User


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Bootstrap from JSON seeds on first run; idempotent.
        bootstrap_if_empty()

    # ---- users ---------------------------------------------------------

    def get_user(self, user_id: str) -> User:
        user = self.get_user_or_none(user_id)
        if user is None:
            raise KeyError(user_id)
        return user

    def get_user_or_none(self, user_id: str) -> Optional[User]:
        conn = get_connection()
        row = conn.execute(
            "SELECT id, display_name, phone FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        accounts = self._accounts_of(user_id)
        return User(
            id=row["id"],
            display_name=row["display_name"],
            phone=row["phone"] or "",
            accounts=accounts,
        )

    def _accounts_of(self, user_id: str) -> list[Account]:
        rows = get_connection().execute(
            """SELECT id, bank, number, balance, currency, is_primary
               FROM accounts WHERE user_id = ? ORDER BY is_primary DESC, id""",
            (user_id,),
        ).fetchall()
        return [
            Account(
                id=r["id"], bank=r["bank"], number=r["number"],
                balance=r["balance"], currency=r["currency"],
                primary=bool(r["is_primary"]),
            )
            for r in rows
        ]

    def primary_account(self, user_id: str) -> Optional[Account]:
        accs = self._accounts_of(user_id)
        if not accs:
            return None
        for a in accs:
            if a.primary:
                return a
        return accs[0]

    def account_by_id(self, user_id: str, account_id: str) -> Account:
        for acc in self._accounts_of(user_id):
            if acc.id == account_id:
                return acc
        raise KeyError(account_id)

    def update_balance(self, user_id: str, account_id: str, delta: int) -> int:
        conn = get_connection()
        with self._lock:
            cur = conn.execute(
                """UPDATE accounts SET balance = balance + ?
                   WHERE id = ? AND user_id = ?
                   RETURNING balance""",
                (delta, account_id, user_id),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(account_id)
            return row["balance"]

    # ---- contacts ------------------------------------------------------

    def get_contact(self, contact_id: str) -> Optional[Contact]:
        row = get_connection().execute(
            """SELECT * FROM contacts WHERE id = ?""",
            (contact_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_contact(row)

    def contacts_of(self, user_id: str) -> list[Contact]:
        rows = get_connection().execute(
            "SELECT * FROM contacts WHERE owner_id = ? ORDER BY frequent DESC, display_name",
            (user_id,),
        ).fetchall()
        return [self._row_to_contact(r) for r in rows]

    def _row_to_contact(self, row) -> Contact:
        aliases = [
            r["alias"] for r in get_connection().execute(
                "SELECT alias FROM contact_aliases WHERE contact_id = ?",
                (row["id"],),
            ).fetchall()
        ]
        return Contact(
            id=row["id"], owner_id=row["owner_id"],
            display_name=row["display_name"], bank=row["bank"],
            account_number=row["account_number"],
            account_masked=row["account_masked"],
            aliases=aliases, label=row["label"],
            verified=bool(row["verified"]),
            frequent=bool(row["frequent"]),
        )

    def add_contact(self, contact: Contact) -> Contact:
        from .context.alias import _fold

        conn = get_connection()
        with self._lock:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """INSERT INTO contacts
                       (id, owner_id, display_name, bank, account_number,
                        account_masked, label, verified, frequent)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        contact.id, contact.owner_id, contact.display_name,
                        contact.bank, contact.account_number,
                        contact.account_masked, contact.label,
                        1 if contact.verified else 0,
                        1 if contact.frequent else 0,
                    ),
                )
                for alias in contact.aliases:
                    conn.execute(
                        """INSERT OR IGNORE INTO contact_aliases
                           (contact_id, alias, alias_normalized) VALUES(?,?,?)""",
                        (contact.id, alias, _fold(alias)),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return contact

    def add_alias(self, contact_id: str, alias: str) -> bool:
        """Persist ``alias`` for ``contact_id`` if it isn't already stored.

        Returns True when a new row was inserted, False when the alias
        already existed (idempotent — safe to call on every confirm).
        """
        from .context.alias import _fold

        normalized = _fold(alias)
        if not normalized:
            return False
        conn = get_connection()
        with self._lock:
            cur = conn.execute(
                """INSERT OR IGNORE INTO contact_aliases
                   (contact_id, alias, alias_normalized) VALUES (?,?,?)""",
                (contact_id, alias.strip(), normalized),
            )
            return cur.rowcount > 0

    def find_contact_by_account(
        self, user_id: str, account_number: str
    ) -> Optional[Contact]:
        row = get_connection().execute(
            """SELECT * FROM contacts WHERE owner_id = ? AND account_number = ?
               LIMIT 1""",
            (user_id, account_number),
        ).fetchone()
        return self._row_to_contact(row) if row else None

    # ---- transactions --------------------------------------------------

    def transactions_of(self, user_id: str) -> list[Transaction]:
        rows = get_connection().execute(
            """SELECT id, owner_id, contact_id, amount, description, category,
                      status, created_at
               FROM transactions WHERE owner_id = ?
               ORDER BY created_at DESC""",
            (user_id,),
        ).fetchall()
        return [
            Transaction(
                id=r["id"], owner_id=r["owner_id"],
                contact_id=r["contact_id"] or "",
                amount=r["amount"], description=r["description"],
                category=r["category"], status=r["status"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    def add_transaction(self, tx: Transaction) -> Transaction:
        conn = get_connection()
        with self._lock:
            conn.execute(
                """INSERT INTO transactions
                   (id, owner_id, contact_id, amount, description, category,
                    status, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    tx.id, tx.owner_id, tx.contact_id, tx.amount,
                    tx.description, tx.category, tx.status,
                    tx.created_at.isoformat(),
                ),
            )
        return tx

    # ---- schedules -----------------------------------------------------

    def add_schedule(self, sched: Schedule) -> Schedule:
        conn = get_connection()
        with self._lock:
            conn.execute(
                """INSERT INTO schedules
                   (id, owner_id, contact_id, source_account_id, amount,
                    description, cron, next_run, active)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    sched.id, sched.owner_id, sched.contact_id,
                    sched.source_account_id, sched.amount, sched.description,
                    sched.cron, sched.next_run.isoformat(),
                    1 if sched.active else 0,
                ),
            )
        return sched

    def schedules_of(self, user_id: str) -> list[Schedule]:
        rows = get_connection().execute(
            """SELECT id, owner_id, contact_id, source_account_id, amount,
                      description, cron, next_run, active
               FROM schedules WHERE owner_id = ? ORDER BY next_run""",
            (user_id,),
        ).fetchall()
        return [
            Schedule(
                id=r["id"], owner_id=r["owner_id"],
                contact_id=r["contact_id"],
                source_account_id=r["source_account_id"],
                amount=r["amount"], description=r["description"],
                cron=r["cron"],
                next_run=datetime.fromisoformat(r["next_run"]),
                active=bool(r["active"]),
            )
            for r in rows
        ]


_store: Optional[Store] = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def now() -> datetime:
    return datetime.now().astimezone()

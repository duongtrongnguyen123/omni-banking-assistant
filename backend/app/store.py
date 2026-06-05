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
from .models.schemas import (
    Account,
    Budget,
    Contact,
    SavingsGoal,
    Schedule,
    Transaction,
    User,
)


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
        # OPT-2 (bench): single-shot fetch with aliases via a correlated
        # subquery + GROUP_CONCAT. Saves the second query (or rather, two
        # queries on a per-tx hot path inside ``_contact_summary``).
        row = get_connection().execute(
            """
            SELECT c.id, c.owner_id, c.display_name, c.bank, c.account_number,
                   c.account_masked, c.label, c.verified, c.frequent,
                   (SELECT GROUP_CONCAT(alias, char(31))
                    FROM contact_aliases a WHERE a.contact_id = c.id) AS aliases
            FROM contacts c WHERE c.id = ?
            """,
            (contact_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_contact_with_aliases(row)

    def contacts_of(self, user_id: str) -> list[Contact]:
        # OPT-1 (bench): N+1 → single query.  Previous version emitted one
        # ``SELECT alias FROM contact_aliases WHERE contact_id = ?`` per
        # contact — on the 1000-row contest dataset that's 1000 extra
        # round-trips (~120ms total).  We now fold all aliases into the
        # row with a LEFT JOIN + GROUP_CONCAT, then split client-side.
        # The character-31 (US, "unit separator") delimiter is chosen so
        # it can't collide with anything in a Vietnamese name.
        rows = get_connection().execute(
            """
            SELECT c.id, c.owner_id, c.display_name, c.bank, c.account_number,
                   c.account_masked, c.label, c.verified, c.frequent,
                   GROUP_CONCAT(a.alias, char(31)) AS aliases
            FROM contacts c
            LEFT JOIN contact_aliases a ON a.contact_id = c.id
            WHERE c.owner_id = ?
            GROUP BY c.id
            ORDER BY c.frequent DESC, c.display_name
            """,
            (user_id,),
        ).fetchall()
        return [self._row_to_contact_with_aliases(r) for r in rows]

    @staticmethod
    def _row_to_contact_with_aliases(row) -> Contact:
        raw = row["aliases"]
        aliases = raw.split("\x1f") if raw else []
        return Contact(
            id=row["id"], owner_id=row["owner_id"],
            display_name=row["display_name"], bank=row["bank"],
            account_number=row["account_number"],
            account_masked=row["account_masked"],
            aliases=aliases, label=row["label"],
            verified=bool(row["verified"]),
            frequent=bool(row["frequent"]),
        )

    # Kept for backwards-compat: ``add_contact`` and ``find_contact_by_account``
    # below still call this. Wraps the new helper by issuing the alias query
    # separately for callers that already hold a "thin" contacts row.
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

    def find_contact_by_account(
        self, user_id: str, account_number: str
    ) -> Optional[Contact]:
        row = get_connection().execute(
            """SELECT * FROM contacts WHERE owner_id = ? AND account_number = ?
               LIMIT 1""",
            (user_id, account_number),
        ).fetchone()
        return self._row_to_contact(row) if row else None

    def contacts_by_ids(self, contact_ids: list[str]) -> dict[str, Contact]:
        """OPT-2 (bench): batch fetch contacts + aliases in a single query.

        Used by ``banking.service.get_history`` to resolve recipient names
        for every transaction in the result window without issuing one
        ``SELECT`` per row (the old ``_contact_summary`` shape took N
        queries; on a 30-row history page with the per-row alias subquery
        that was 60 round-trips).
        """
        if not contact_ids:
            return {}
        placeholders = ",".join("?" * len(contact_ids))
        rows = get_connection().execute(
            f"""
            SELECT c.id, c.owner_id, c.display_name, c.bank, c.account_number,
                   c.account_masked, c.label, c.verified, c.frequent,
                   (SELECT GROUP_CONCAT(alias, char(31))
                    FROM contact_aliases a WHERE a.contact_id = c.id) AS aliases
            FROM contacts c WHERE c.id IN ({placeholders})
            """,
            contact_ids,
        ).fetchall()
        return {r["id"]: self._row_to_contact_with_aliases(r) for r in rows}

    # ---- transactions --------------------------------------------------

    def transactions_of(
        self,
        user_id: str,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        contact_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[Transaction]:
        """OPT-3 (bench): optional filters pushed into SQL.

        The old signature unconditionally returned every transaction the
        user has ever made and materialised each row into a Pydantic model
        — for the contest dataset (520k rows) that's ~2GB allocation per
        call. Every chat handler, the suggester, the recurring detector,
        and the insights endpoint hit this path on every request, so the
        15-30s baseline transfer latency was almost entirely Pydantic
        construction.

        Callers that genuinely need the full history (recurring miner,
        insights subscriptions) still get it by passing no filters.
        Callers that only need a slice (history endpoint, suggester
        reason-strings, balance / smalltalk path) now opt in to a tight
        SQL range and skip the materialisation cost entirely.
        """
        clauses = ["owner_id = ?"]
        params: list = [user_id]
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("created_at < ?")
            params.append(until.isoformat())
        if contact_id is not None:
            clauses.append("contact_id = ?")
            params.append(contact_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = " AND ".join(clauses)

        sql = (
            "SELECT id, owner_id, contact_id, amount, description, category, "
            "status, created_at FROM transactions "
            f"WHERE {where} ORDER BY created_at DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        rows = get_connection().execute(sql, params).fetchall()
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

    def transaction_count(self, user_id: str) -> int:
        """OPT-3 (bench): cheap counterpart to ``transactions_of`` for the
        suggester's training trigger. Avoids paying the full materialisation
        cost just to read ``len(txs)``."""
        row = get_connection().execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE owner_id = ?",
            (user_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def transactions_raw(
        self,
        user_id: str,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        contact_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[tuple]:
        """OPT-3 (bench): bulk fetch of (id, contact_id, amount, description,
        status, created_at_iso) tuples for callers that don't need Pydantic
        ``Transaction`` objects.

        At contest scale, the per-row Pydantic construction inside
        ``transactions_of`` dominates the cost — building a model for each
        of 520k rows takes ~5s of pure Python.  The recurring detector
        only reads ``contact_id``, ``amount``, ``description``,
        ``status``, ``created_at``, so we hand it a tuple list directly.
        """
        clauses = ["owner_id = ?"]
        params: list = [user_id]
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("created_at < ?")
            params.append(until.isoformat())
        if contact_id is not None:
            clauses.append("contact_id = ?")
            params.append(contact_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = " AND ".join(clauses)
        rows = get_connection().execute(
            f"SELECT id, contact_id, amount, description, status, created_at "
            f"FROM transactions WHERE {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [(r["id"], r["contact_id"] or "", r["amount"],
                 r["description"], r["status"], r["created_at"])
                for r in rows]

    def completed_amount_mean(self, user_id: str) -> Optional[float]:
        """OPT-3 (bench): global mean for the cold-contact anomaly
        fallback in ``safety.rules.evaluate``.  Computing this in SQL is
        ~100× faster than materialising every transaction and reading
        ``.amount`` in Python, which is all the caller was doing."""
        row = get_connection().execute(
            "SELECT AVG(amount) AS mu FROM transactions "
            "WHERE owner_id = ? AND status = 'completed' AND amount > 0",
            (user_id,),
        ).fetchone()
        return float(row["mu"]) if row and row["mu"] is not None else None

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


    # ---- budgets -------------------------------------------------------

    def budgets_of(self, user_id: str) -> list[Budget]:
        rows = get_connection().execute(
            """SELECT id, user_id, category, monthly_limit_vnd, created_at
               FROM budgets WHERE user_id = ? ORDER BY created_at""",
            (user_id,),
        ).fetchall()
        return [
            Budget(
                id=r["id"], user_id=r["user_id"], category=r["category"],
                monthly_limit_vnd=r["monthly_limit_vnd"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    def get_budget_by_category(
        self, user_id: str, category: str
    ) -> Optional[Budget]:
        row = get_connection().execute(
            """SELECT id, user_id, category, monthly_limit_vnd, created_at
               FROM budgets WHERE user_id = ? AND category = ?""",
            (user_id, category),
        ).fetchone()
        if row is None:
            return None
        return Budget(
            id=row["id"], user_id=row["user_id"], category=row["category"],
            monthly_limit_vnd=row["monthly_limit_vnd"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def add_budget(self, budget: Budget) -> Budget:
        """Upsert by (user_id, category). The UNIQUE index on the table
        means re-adding a budget for the same category overwrites the
        limit — this matches the chat flow "đặt lại ngân sách ăn uống
        thành 4 triệu" which should not error."""
        conn = get_connection()
        with self._lock:
            conn.execute(
                """INSERT INTO budgets
                   (id, user_id, category, monthly_limit_vnd, created_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(user_id, category) DO UPDATE SET
                       monthly_limit_vnd = excluded.monthly_limit_vnd""",
                (
                    budget.id, budget.user_id, budget.category,
                    budget.monthly_limit_vnd, budget.created_at.isoformat(),
                ),
            )
        return self.get_budget_by_category(budget.user_id, budget.category) or budget

    def update_budget(
        self, budget_id: str, monthly_limit_vnd: int
    ) -> Optional[Budget]:
        conn = get_connection()
        with self._lock:
            cur = conn.execute(
                """UPDATE budgets SET monthly_limit_vnd = ?
                   WHERE id = ? RETURNING user_id, category""",
                (monthly_limit_vnd, budget_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return self.get_budget_by_category(row["user_id"], row["category"])

    def delete_budget(self, budget_id: str) -> bool:
        conn = get_connection()
        with self._lock:
            cur = conn.execute(
                "DELETE FROM budgets WHERE id = ?",
                (budget_id,),
            )
            return cur.rowcount > 0

    # ---- savings goals -------------------------------------------------

    def goals_of(self, user_id: str) -> list[SavingsGoal]:
        rows = get_connection().execute(
            """SELECT id, user_id, name, target_vnd, current_vnd,
                      deadline, created_at
               FROM savings_goals WHERE user_id = ? ORDER BY created_at""",
            (user_id,),
        ).fetchall()
        return [
            SavingsGoal(
                id=r["id"], user_id=r["user_id"], name=r["name"],
                target_vnd=r["target_vnd"], current_vnd=r["current_vnd"],
                deadline=r["deadline"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    def get_goal(self, goal_id: str) -> Optional[SavingsGoal]:
        row = get_connection().execute(
            """SELECT id, user_id, name, target_vnd, current_vnd,
                      deadline, created_at
               FROM savings_goals WHERE id = ?""",
            (goal_id,),
        ).fetchone()
        if row is None:
            return None
        return SavingsGoal(
            id=row["id"], user_id=row["user_id"], name=row["name"],
            target_vnd=row["target_vnd"], current_vnd=row["current_vnd"],
            deadline=row["deadline"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def add_goal(self, goal: SavingsGoal) -> SavingsGoal:
        conn = get_connection()
        with self._lock:
            conn.execute(
                """INSERT INTO savings_goals
                   (id, user_id, name, target_vnd, current_vnd,
                    deadline, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    goal.id, goal.user_id, goal.name, goal.target_vnd,
                    goal.current_vnd, goal.deadline,
                    goal.created_at.isoformat(),
                ),
            )
        return goal

    def contribute_to_goal(
        self, goal_id: str, amount: int
    ) -> SavingsGoal:
        """Add ``amount`` to ``current_vnd``. Rejects contributions that
        would push past ``target_vnd`` — caller is expected to clamp
        first if they want a partial top-up.

        Raises ``ValueError`` with a Vietnamese message if the
        contribution is rejected, ``KeyError`` if the goal id is
        unknown."""
        if amount <= 0:
            raise ValueError("Số tiền góp phải lớn hơn 0.")
        conn = get_connection()
        with self._lock:
            conn.execute("BEGIN")
            try:
                row = conn.execute(
                    """SELECT target_vnd, current_vnd
                       FROM savings_goals WHERE id = ?""",
                    (goal_id,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    raise KeyError(goal_id)
                new_total = row["current_vnd"] + amount
                if new_total > row["target_vnd"]:
                    conn.execute("ROLLBACK")
                    raise ValueError(
                        "Góp thêm sẽ vượt quá mục tiêu đã đặt."
                    )
                conn.execute(
                    """UPDATE savings_goals SET current_vnd = ?
                       WHERE id = ?""",
                    (new_total, goal_id),
                )
                conn.execute("COMMIT")
            except Exception:
                # Belt-and-braces rollback for unexpected sqlite errors;
                # known branches above already ROLLBACK explicitly.
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return self.get_goal(goal_id)  # type: ignore[return-value]


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

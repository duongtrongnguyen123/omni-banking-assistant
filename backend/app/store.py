"""In-memory data store backed by JSON seed files.

Standing in for PostgreSQL/Redis/Pinecone in the slide architecture. The API
surface is intentionally narrow so it can be swapped for real stores later.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import get_settings
from .models.schemas import AuditEvent, Contact, Schedule, Transaction, User


def _load(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        data_dir = get_settings().data_dir
        self.users: dict[str, User] = {
            u["id"]: User(**u) for u in _load(data_dir / "users.json")
        }
        self.contacts: dict[str, Contact] = {
            c["id"]: Contact(**c) for c in _load(data_dir / "contacts.json")
        }
        self.transactions: dict[str, Transaction] = {
            t["id"]: Transaction(**t) for t in _load(data_dir / "transactions.json")
        }
        self.schedules: dict[str, Schedule] = {
            s["id"]: Schedule(**s) for s in _load(data_dir / "schedules.json")
        }
        self.audit_events: list[AuditEvent] = []

    def get_user(self, user_id: str) -> User:
        return self.users[user_id]

    def get_user_or_none(self, user_id: str) -> Optional[User]:
        return self.users.get(user_id)

    def contacts_of(self, user_id: str) -> list[Contact]:
        return [c for c in self.contacts.values() if c.owner_id == user_id]

    def _load_transactions(self, user_id: str) -> list[Transaction]:
        """Đọc lịch sử từ nguồn đã chọn (in-memory hoặc Postgres RDS)."""
        if get_settings().data_backend == "postgres":
            from .db import postgres

            txs = postgres.fetch_transactions(user_id)
            if txs:  # fail-open: nếu RDS rỗng/sập thì rơi về in-memory
                return txs
        txs = [t for t in self.transactions.values() if t.owner_id == user_id]
        return sorted(txs, key=lambda t: t.created_at, reverse=True)

    def transactions_of(self, user_id: str) -> list[Transaction]:
        settings = get_settings()
        if not settings.cache_enabled:
            return self._load_transactions(user_id)

        # Cache-aside: thử Redis trước, miss thì đọc nguồn chính rồi ghi cache.
        from .redis_client import get_cache, set_cache, user_history_key

        key = user_history_key(user_id)
        cached = get_cache(key)
        if cached is not None:
            return [Transaction(**t) for t in cached]

        txs = self._load_transactions(user_id)
        set_cache(
            key,
            [t.model_dump(mode="json") for t in txs],
            settings.cache_ttl_seconds,
        )
        return txs

    def add_transaction(self, tx: Transaction) -> Transaction:
        with self._lock:
            self.transactions[tx.id] = tx
            self._invalidate(tx.owner_id)
            return tx

    @staticmethod
    def _invalidate(user_id: str) -> None:
        """Xoá mọi cache của user sau khi dữ liệu thay đổi (history/summary/balance)."""
        if get_settings().cache_enabled:
            from .redis_client import invalidate_user

            invalidate_user(user_id)

    def update_balance(self, user_id: str, account_id: str, delta: int) -> int:
        with self._lock:
            user = self.users[user_id]
            for acc in user.accounts:
                if acc.id == account_id:
                    acc.balance += delta
                    self._invalidate(user_id)
                    return acc.balance
            raise KeyError(account_id)

    def primary_account(self, user_id: str):
        """Return the user's primary account, or None if the user is unknown
        or has no accounts on file. Callers must handle None gracefully —
        crashing here would 500 the API on any unrecognised user_id."""
        user = self.users.get(user_id)
        if user is None or not user.accounts:
            return None
        for acc in user.accounts:
            if acc.primary:
                return acc
        return user.accounts[0]

    def account_by_id(self, user_id: str, account_id: str):
        for acc in self.users[user_id].accounts:
            if acc.id == account_id:
                return acc
        raise KeyError(account_id)

    def add_schedule(self, sched: Schedule) -> Schedule:
        with self._lock:
            self.schedules[sched.id] = sched
            return sched

    def schedules_of(self, user_id: str) -> list[Schedule]:
        return [s for s in self.schedules.values() if s.owner_id == user_id]

    def add_contact(self, contact: Contact) -> Contact:
        with self._lock:
            self.contacts[contact.id] = contact
            return contact

    def find_contact_by_account(
        self, user_id: str, account_number: str
    ) -> Optional[Contact]:
        for c in self.contacts.values():
            if c.owner_id == user_id and c.account_number == account_number:
                return c
        return None

    def add_audit_event(self, event: AuditEvent) -> AuditEvent:
        with self._lock:
            self.audit_events.append(event)
            return event

    def audit_of(self, user_id: str, limit: int = 100) -> list[AuditEvent]:
        items = [e for e in self.audit_events if e.user_id == user_id]
        return list(reversed(items[-limit:]))


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def now() -> datetime:
    return datetime.now().astimezone()

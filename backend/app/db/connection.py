"""Thread-safe SQLite connection with WAL + lazy schema init.

We deliberately stick to stdlib sqlite3 (no SQLAlchemy / ORM) because the
data model is small, the queries are hand-written and tuned, and adding an
ORM would burn budget without paying off for this MVP.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

from ..config import get_settings

_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def db_path() -> Path:
    """Resolve the on-disk SQLite file. Defaults to backend/app/data/omni.db.

    Override with `OMNI_DB_PATH` env (handy for tests / a separate read-only
    copy of the dataset)."""
    import os

    override = os.environ.get("OMNI_DB_PATH")
    if override:
        return Path(override).expanduser()
    return get_settings().data_dir / "omni.db"


def get_connection() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            path = db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            _CONN = sqlite3.connect(
                str(path),
                check_same_thread=False,
                isolation_level=None,  # autocommit; explicit BEGIN/COMMIT in transactions
            )
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA foreign_keys = ON")
            _init_schema(_CONN)
        return _CONN


def _init_schema(conn: sqlite3.Connection) -> None:
    with SCHEMA_PATH.open("r", encoding="utf-8") as f:
        conn.executescript(f.read())


def reset_connection() -> None:
    """Close the cached connection — used by tests that need a clean DB."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None

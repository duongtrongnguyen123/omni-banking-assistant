"""Thread-safe SQLite connection with WAL + lazy schema init.

We deliberately stick to stdlib sqlite3 (no SQLAlchemy / ORM) because the
data model is small, the queries are hand-written and tuned, and adding an
ORM would burn budget without paying off for this MVP.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from ..config import get_settings

log = logging.getLogger("omni.db.connection")

_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# Module-level cache of the last self-heal verdict so health probes
# don't re-PRAGMA on every request — integrity_check walks the entire
# DB which is too expensive for a readiness probe path. Set once by
# ``_self_heal_sqlite()`` during startup; remains stable until the
# process restarts.
_INTEGRITY_STATUS: str = "unknown"
_INTEGRITY_DETAILS: dict = {}


def integrity_status() -> str:
    """Return the cached SQLite integrity status: ``ok|repaired|broken|unknown``.

    ``unknown`` is the pre-startup default — once :func:`_self_heal_sqlite`
    has run the value is one of the other three. The health probe in
    :mod:`app.routes.health` reads this and surfaces it under
    ``checks.sqlite_integrity``.
    """
    return _INTEGRITY_STATUS


def integrity_details() -> dict:
    """Return a dict snapshot of integrity-check diagnostics.

    Shape: ``{"errors_before": [...], "errors_after": [...]}``. Used by
    /health/ready when the status is anything other than ``ok``.
    """
    return dict(_INTEGRITY_DETAILS)


def _run_integrity_check(conn: sqlite3.Connection) -> list[str]:
    """Run ``PRAGMA integrity_check`` and return the row list.

    SQLite returns a single row ``("ok",)`` when the DB is healthy, or
    one row per discovered problem otherwise. We normalise to a list of
    strings so callers can ``== ["ok"]`` or iterate the errors.

    Never raises — on any sqlite-side failure we treat the DB as
    broken and surface a synthetic error string. The whole point of
    the self-heal is to degrade gracefully.
    """
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except Exception as exc:  # noqa: BLE001
        return [f"pragma_failed: {exc!r}"]
    out: list[str] = []
    for row in rows:
        try:
            value = row[0]
        except (IndexError, TypeError):
            value = str(row)
        if value is None:
            continue
        out.append(str(value))
    return out


def _self_heal_sqlite(conn: Optional[sqlite3.Connection] = None) -> str:
    """Run an integrity check + auto-repair pass on the runtime SQLite file.

    Behaviour:

    * ``PRAGMA integrity_check`` returns ``["ok"]`` → record ``ok``.
    * Otherwise log a warning with the errors, run ``REINDEX``, then
      re-check. If the re-check is clean → record ``repaired``. If it
      still reports errors → log an error and record ``broken``.

    The function NEVER raises — a broken DB must not crash the
    process. Operators can repair from a ``.bak`` if needed; the
    cached status is surfaced via /health/ready so the failure is
    visible without crashing.

    Increments the ``omni_sqlite_repair_total`` counter exactly once
    per call, labelled with the resulting status.
    """
    global _INTEGRITY_STATUS, _INTEGRITY_DETAILS

    if conn is None:
        try:
            conn = get_connection()
        except Exception as exc:  # noqa: BLE001
            log.error("self-heal: could not open DB for integrity check: %r", exc)
            _INTEGRITY_STATUS = "broken"
            _INTEGRITY_DETAILS = {
                "errors_before": [f"connect_failed: {exc!r}"],
                "errors_after": [],
            }
            _record_repair_metric("broken")
            return "broken"

    # We never close ``conn`` ourselves — it's the process-wide
    # cached handle from :func:`get_connection`. Closing it would
    # invalidate the connection for every other caller.
    errors_before = _run_integrity_check(conn)
    if errors_before == ["ok"]:
        _INTEGRITY_STATUS = "ok"
        _INTEGRITY_DETAILS = {"errors_before": [], "errors_after": []}
        _record_repair_metric("ok")
        return "ok"

    log.warning(
        "self-heal: SQLite integrity_check reported %d issue(s): %s",
        len(errors_before),
        errors_before[:5],
    )
    try:
        conn.execute("REINDEX")
    except Exception as exc:  # noqa: BLE001
        log.error("self-heal: REINDEX failed: %r", exc)

    errors_after = _run_integrity_check(conn)
    if errors_after == ["ok"]:
        log.warning(
            "self-heal: SQLite indexes rebuilt successfully (%d issue(s) cleared)",
            len(errors_before),
        )
        _INTEGRITY_STATUS = "repaired"
        _INTEGRITY_DETAILS = {"errors_before": errors_before, "errors_after": []}
        _record_repair_metric("repaired")
        return "repaired"

    log.error(
        "self-heal: SQLite still reports %d issue(s) after REINDEX: %s — operator action required",
        len(errors_after),
        errors_after[:5],
    )
    _INTEGRITY_STATUS = "broken"
    _INTEGRITY_DETAILS = {"errors_before": errors_before, "errors_after": errors_after}
    _record_repair_metric("broken")
    return "broken"


def _record_repair_metric(result: str) -> None:
    """Increment the Prometheus repair counter without crashing on import cycles.

    Lazy-imported because ``app.services.metrics`` may not yet be
    imported when we self-heal during the earliest moments of
    lifespan startup, and we don't want a stray ImportError on a
    background path to silently swallow the result.
    """
    try:
        from ..services.metrics import sqlite_repair_total

        sqlite_repair_total.inc(result=result)
    except Exception as exc:  # noqa: BLE001
        log.debug("self-heal: metric increment skipped: %r", exc)


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
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(chat_messages)").fetchall()
    }
    if "response_json" not in cols:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN response_json TEXT")
    user_cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "kyc_level" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN kyc_level TEXT NOT NULL DEFAULT 'normal'")
    tx_cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(transactions)").fetchall()
    }
    tx_migrations = {
        "auth_methods": "ALTER TABLE transactions ADD COLUMN auth_methods TEXT NOT NULL DEFAULT ''",
        "kyc_level": "ALTER TABLE transactions ADD COLUMN kyc_level TEXT",
        "daily_limit_vnd": "ALTER TABLE transactions ADD COLUMN daily_limit_vnd INTEGER",
        "daily_total_before_vnd": "ALTER TABLE transactions ADD COLUMN daily_total_before_vnd INTEGER",
        "retention_until": "ALTER TABLE transactions ADD COLUMN retention_until TEXT",
    }
    for col, sql in tx_migrations.items():
        if col not in tx_cols:
            conn.execute(sql)


def reset_connection() -> None:
    """Close the cached connection — used by tests that need a clean DB."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None

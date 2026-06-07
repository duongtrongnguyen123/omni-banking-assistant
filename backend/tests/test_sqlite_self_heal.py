"""Self-heal regression tests for the SQLite integrity-check + REINDEX
auto-repair pass wired through :func:`app.db.connection._self_heal_sqlite`.

Motivation: during a live demo, ``ix_chat_messages_session`` corrupted
and ``chat_log.get_session`` silently fell through, minting a fresh
session id per turn and wiping the in-flight TransactionDraft. The
self-heal pass walks ``PRAGMA integrity_check`` at startup and rebuilds
indexes with ``REINDEX`` so the operator (and the judges) never see
that fail mode again.

Three scenarios:

1. **Happy path** — a clean DB stays ``ok`` and the metric / status
   reflects it.
2. **Corrupted index, repairable** — we manually wreck a non-key index
   via the ``writable_schema`` PRAGMA and confirm the self-heal cleans
   it.
3. **Unrepairable corruption** — we truncate the DB file mid-page so
   even REINDEX can't fix it; the function must record ``broken`` and
   not raise.

We use a temp file per test (``OMNI_DB_PATH``) and reset the cached
connection between cases so the state cache in
``app.db.connection`` doesn't leak from one test to the next.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_chat_log_schema(path: Path) -> None:
    """Create the full production schema + a few chat sessions / messages.

    We load ``backend/app/db/schema.sql`` directly so the table & index
    definitions match what production runs — anything less and the
    later ``get_connection()`` call would discover schema drift and run
    its own migrations (which would themselves trip the integrity check
    we're trying to test).
    """
    from app.db.connection import SCHEMA_PATH

    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        with SCHEMA_PATH.open("r", encoding="utf-8") as f:
            conn.executescript(f.read())
        # Seed a couple of sessions + messages so the index has something
        # to lose. Three sessions x three messages keeps the file small
        # enough that any later corruption maps to the index, not data
        # pages.
        for i in range(3):
            sid = f"s_{i}"
            conn.execute(
                "INSERT INTO chat_sessions (id, user_id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, "u_test", f"Session {i}", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
            for j in range(3):
                conn.execute(
                    "INSERT INTO chat_messages "
                    "(id, session_id, user_id, role, content, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        f"m_{i}_{j}",
                        sid,
                        "u_test",
                        "user" if j % 2 == 0 else "assistant",
                        f"hello {j}",
                        f"2026-01-01T00:00:{j:02d}Z",
                    ),
                )
    finally:
        conn.close()


def _force_corrupt_chat_index(path: Path) -> None:
    """Corrupt ``ix_chat_messages_session`` by raw-byte tampering on the
    SQLite file.

    Strategy: SQLite stores each index as a b-tree rooted at a specific
    page. We look up the rootpage from ``sqlite_master`` (read-only —
    the ``writable_schema`` pragma only relaxes UPDATE on the schema,
    not all platforms allow it without compile-time flag), then close
    the connection, open the file in ``r+b`` mode, and zero out the
    payload region of that page.

    A wiped index page leaves the table data intact (REINDEX can
    rebuild the index from the rows), but ``PRAGMA integrity_check``
    immediately reports the inconsistency — usually as
    ``wrong # of entries in index ix_chat_messages_session``, the
    exact error string from the live-demo incident.

    We make sure the SQLite VFS isn't holding write locks before we
    edit: ``PRAGMA wal_checkpoint(FULL)`` flushes the WAL back into
    the main DB and ``conn.close()`` releases the file. The DB has
    been opened in WAL mode by the production schema bootstrap, so
    this checkpoint is the cleanest way to guarantee subsequent
    integrity_check sees our corruption.
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        # Force a clean fsync so the index page is materialised in
        # the main DB file (not just the WAL).
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        row = conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = 'ix_chat_messages_session'"
        ).fetchone()
        if row is None or row[0] is None:
            raise RuntimeError("ix_chat_messages_session not present in seeded DB")
        rootpage = int(row[0])
        page_size_row = conn.execute("PRAGMA page_size").fetchone()
        page_size = int(page_size_row[0])
    finally:
        conn.close()

    # SQLite numbers pages starting at 1; offset = (rootpage - 1) * page_size.
    offset = (rootpage - 1) * page_size

    # Read the page header to learn the current cell count, then
    # decrement it. SQLite's integrity_check walks the table rows and
    # cross-checks each row appears in the index — dropping the
    # advertised cell count to 0 leaves the underlying b-tree cells
    # still on disk but unreachable through the page header, so the
    # check reports "wrong # of entries in index <name>" exactly like
    # the live-demo incident. REINDEX rebuilds the index from the
    # table rows, so the file becomes consistent again afterwards.
    with open(path, "r+b") as fh:
        fh.seek(offset)
        header = bytearray(fh.read(12))
        # Force cell count to 0 (bytes 3-4 big-endian uint16). Leave
        # every other header byte intact so SQLite still sees a
        # parseable index page — the inconsistency is purely "I
        # promised N rows, I have 0".
        header[3] = 0x00
        header[4] = 0x00
        fh.seek(offset)
        fh.write(bytes(header))
        fh.flush()
        os.fsync(fh.fileno())


def _reset_db_module(tmp_db: Path):
    """Reload ``app.db.connection`` so the cached ``_CONN`` + status
    don't leak across tests.

    Setting ``OMNI_DB_PATH`` before reload is enough — ``db_path()``
    reads it lazily. We also reset the metrics counter we increment so
    the assertion in the happy-path test is self-contained.
    """
    os.environ["OMNI_DB_PATH"] = str(tmp_db)
    from app.db import connection as _conn_mod

    _conn_mod.reset_connection()
    _conn_mod._INTEGRITY_STATUS = "unknown"
    _conn_mod._INTEGRITY_DETAILS = {}
    return importlib.reload(_conn_mod)


# ---------------------------------------------------------------------------
# Scenario 1 — happy path
# ---------------------------------------------------------------------------


def test_self_heal_happy_path_reports_ok(tmp_path: Path) -> None:
    """Clean DB: integrity_check returns ok → status = ``ok``, no repair.

    Also asserts the lifecycle readiness check reads the cached status
    so /health/ready surfaces it.
    """
    db_file = tmp_path / "clean.db"
    _seed_chat_log_schema(db_file)

    conn_mod = _reset_db_module(db_file)

    result = conn_mod._self_heal_sqlite()

    assert result == "ok"
    assert conn_mod.integrity_status() == "ok"
    assert conn_mod.integrity_details() == {"errors_before": [], "errors_after": []}

    # The lifecycle helper must return the same cached value.
    from app.services import lifecycle as _lc

    assert _lc._check_sqlite_integrity() == "ok"


# ---------------------------------------------------------------------------
# Scenario 2 — corrupted index, repairable by REINDEX
# ---------------------------------------------------------------------------


def test_self_heal_repairs_corrupted_index(tmp_path: Path) -> None:
    """Corrupted index → ``PRAGMA integrity_check`` reports a failure
    BEFORE self-heal; after :func:`_self_heal_sqlite` ran, the same
    pragma reports ``ok`` and the cached status is ``repaired``.
    """
    db_file = tmp_path / "corrupted.db"
    _seed_chat_log_schema(db_file)
    _force_corrupt_chat_index(db_file)

    # Confirm corruption is visible from a fresh handle. A heavily
    # corrupted page may make ``PRAGMA integrity_check`` itself raise
    # ``DatabaseError: database disk image is malformed`` — that still
    # counts as "not ok" for our purposes (the self-heal must handle
    # both shapes).
    pre_conn = sqlite3.connect(str(db_file))
    try:
        try:
            pre_errors = [
                row[0] for row in pre_conn.execute("PRAGMA integrity_check").fetchall()
            ]
        except sqlite3.DatabaseError as exc:
            pre_errors = [f"raised: {exc!r}"]
    finally:
        pre_conn.close()
    assert pre_errors != ["ok"], (
        f"expected at least one integrity error before repair, got {pre_errors!r}"
    )

    conn_mod = _reset_db_module(db_file)
    result = conn_mod._self_heal_sqlite()

    assert result == "repaired"
    assert conn_mod.integrity_status() == "repaired"
    details = conn_mod.integrity_details()
    assert details["errors_before"]  # at least one entry
    assert details["errors_after"] == []

    # And a fresh handle agrees the file is now healthy.
    post_conn = sqlite3.connect(str(db_file))
    post_errors = [row[0] for row in post_conn.execute("PRAGMA integrity_check").fetchall()]
    post_conn.close()
    assert post_errors == ["ok"], f"expected ok after repair, got {post_errors!r}"


# ---------------------------------------------------------------------------
# Scenario 3 — unrepairable corruption
# ---------------------------------------------------------------------------


def test_self_heal_unrepairable_db_marked_broken_without_raising(tmp_path: Path) -> None:
    """Truncated DB file → REINDEX cannot fix it.

    The self-heal must:
      * log an ERROR (not asserted directly here — log capture would
        couple the test to caplog mechanics; we assert the visible
        contract instead),
      * return the string ``broken``,
      * NOT raise — degraded DB is fine, crashing on startup is not.
    """
    db_file = tmp_path / "broken.db"
    _seed_chat_log_schema(db_file)

    # Truncate the file mid-page so the b-tree pages are inconsistent.
    # SQLite default page size is 4 KiB; lopping off the tail leaves a
    # valid header but a partial leaf page, which ``PRAGMA
    # integrity_check`` reports as bad and ``REINDEX`` cannot recover
    # from (it needs to read every row of every table first).
    size = db_file.stat().st_size
    # Leave just the header + first page intact, drop everything else.
    keep = min(size, 4096)
    with open(db_file, "r+b") as f:
        f.truncate(keep)

    conn_mod = _reset_db_module(db_file)

    # Must not raise — this is the central safety contract of the
    # self-heal. We capture the return string and inspect the cached
    # state explicitly.
    try:
        result = conn_mod._self_heal_sqlite()
    except Exception as exc:  # pragma: no cover — the test fails loudly
        pytest.fail(f"_self_heal_sqlite raised on broken DB: {exc!r}")

    assert result == "broken", (
        f"expected 'broken' for unrepairable DB, got {result!r}"
    )
    assert conn_mod.integrity_status() == "broken"


# ---------------------------------------------------------------------------
# Metric integration — increments once per repair attempt
# ---------------------------------------------------------------------------


def test_self_heal_increments_metric_with_result_label(tmp_path: Path) -> None:
    """Each ``_self_heal_sqlite`` call bumps
    ``omni_sqlite_repair_total{result=<status>}`` exactly once.

    We snapshot before/after and check the delta — the registry is
    process-wide so other tests may have already incremented it.
    """
    db_file = tmp_path / "clean_metric.db"
    _seed_chat_log_schema(db_file)
    conn_mod = _reset_db_module(db_file)

    from app.services.metrics import sqlite_repair_total

    before = sqlite_repair_total.labels(result="ok").value
    conn_mod._self_heal_sqlite()
    after = sqlite_repair_total.labels(result="ok").value

    assert after - before == 1.0, (
        f"expected exactly one ok increment, delta={after - before}"
    )


# ---------------------------------------------------------------------------
# Readiness probe wiring — /health/ready surfaces the cached status
# ---------------------------------------------------------------------------


def test_readiness_snapshot_includes_sqlite_integrity(tmp_path: Path) -> None:
    """`readiness_snapshot()` must include ``sqlite_integrity`` and only
    fail (ready=False) when the cached status is ``broken``.
    """
    db_file = tmp_path / "ready.db"
    _seed_chat_log_schema(db_file)
    conn_mod = _reset_db_module(db_file)
    conn_mod._self_heal_sqlite()

    # Allow the suggester / embedder checks to pass independently —
    # we're testing the new key, not the full readiness gate.
    os.environ["OMNI_HEALTH_ALLOW_UNTRAINED"] = "1"
    os.environ["OMNI_SKIP_EMBED_BACKFILL"] = "1"

    from app.services import lifecycle as _lc

    snap = _lc.readiness_snapshot()
    assert "sqlite_integrity" in snap["checks"]
    assert snap["checks"]["sqlite_integrity"] == "ok"

    # Simulate a broken DB after the fact and confirm readiness flips.
    conn_mod._INTEGRITY_STATUS = "broken"
    snap2 = _lc.readiness_snapshot()
    assert snap2["checks"]["sqlite_integrity"] == "broken"
    assert snap2["ready"] is False

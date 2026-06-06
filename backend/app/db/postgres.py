"""Đường đọc Postgres omni (AWS RDS) — opt-in qua DATA_BACKEND=postgres.

Mặc định app chạy store in-memory (nhanh, đủ cho demo). Khi bật DATA_BACKEND=postgres,
``store.transactions_of`` sẽ lấy dữ liệu THẬT từ RDS (591k giao dịch) qua module này —
đây chính là đường đọc nặng mà lớp cache Redis tăng tốc.

Dùng connection pool (ThreadedConnectionPool) để FastAPI đa luồng không phải mở
kết nối mới mỗi request. Mọi lỗi/thiếu cấu hình đều fail-open (trả rỗng) thay vì
làm chết app.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from ..config import get_settings
from ..models.schemas import Transaction

logger = logging.getLogger("omni.postgres")

try:
    import psycopg2
    from psycopg2.pool import ThreadedConnectionPool

    _PG_IMPORTED = True
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore
    ThreadedConnectionPool = None  # type: ignore
    _PG_IMPORTED = False


_pool: "Optional[ThreadedConnectionPool]" = None
_pool_lock = threading.Lock()
_pool_failed = False


def _get_pool():
    """Khởi tạo lazy connection pool tới RDS, hoặc ``None`` nếu không khả dụng."""
    global _pool, _pool_failed
    if _pool is not None:
        return _pool
    if _pool_failed or not _PG_IMPORTED:
        return None

    with _pool_lock:
        if _pool is not None:
            return _pool
        url = get_settings().database_url
        if not url:
            logger.warning("DATABASE_URL trống — không thể đọc Postgres, fallback rỗng.")
            _pool_failed = True
            return None
        try:
            _pool = ThreadedConnectionPool(minconn=1, maxconn=8, dsn=url)
            logger.info("Đã mở connection pool tới Postgres omni.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Không mở được pool Postgres (%s) — fallback rỗng.", exc)
            _pool_failed = True
            _pool = None
    return _pool


def is_available() -> bool:
    return _get_pool() is not None


def fetch_transactions(user_id: str) -> list[Transaction]:
    """Quét toàn bộ lịch sử giao dịch của user từ RDS, đã sort mới->cũ.

    Trả [] nếu Postgres không khả dụng (fail-open).
    """
    pool = _get_pool()
    if pool is None:
        return []

    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, owner_id, contact_id, amount, description,
                       category, status, created_at
                FROM transactions
                WHERE owner_id = %s
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            rows = cur.fetchall()
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lỗi đọc transactions từ Postgres: %s", exc)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        return []
    finally:
        if conn is not None:
            pool.putconn(conn)

    return [_row_to_tx(r) for r in rows]


_VALID_STATUS = {"pending", "completed", "cancelled", "needs_confirm"}


def _row_to_tx(row) -> Transaction:
    """Map một dòng SQL sang model Transaction, chuẩn hoá NULL/Decimal."""
    rid, owner_id, contact_id, amount, description, category, status, created_at = row
    return Transaction(
        id=rid,
        owner_id=owner_id,
        contact_id=contact_id or "",  # RDS cho phép NULL; model yêu cầu str
        amount=int(amount),  # NUMERIC -> int
        description=description or "",
        category=category or "other",
        status=status if status in _VALID_STATUS else "completed",
        created_at=created_at,
    )

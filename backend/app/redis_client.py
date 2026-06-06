"""Lớp cache Redis cho Omni — cache-aside, fail-open, có đo lường.

Thiết kế theo 3 nguyên tắc:

1. **Fail-open**: Redis chỉ là lớp tăng tốc *tùy chọn*. Thiếu thư viện, URL trống,
   hay Redis sập → mọi hàm lặng lẽ trả ``None``/no-op để app đọc thẳng nguồn chính
   (Postgres RDS hoặc store in-memory), KHÔNG bao giờ làm chết app.
2. **Namespaced keys**: mọi key nằm dưới tiền tố ``omni:`` để xoá hàng loạt theo
   pattern an toàn (vd invalidate toàn bộ cache của một user).
3. **Observable**: đếm hit/miss/set/error để soi hiệu quả cache qua /health/cache.

Quy ước key (xem ``cache_key``):
    omni:user:<uid>:history            -> JSON danh sách giao dịch thô (store-level)
    omni:user:<uid>:summary:<c>:<p>    -> JSON kết quả get_history đã tổng hợp
    omni:user:<uid>:balance            -> JSON số dư
Xoá toàn bộ cache của user: pattern ``omni:user:<uid>:*``.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Iterable, Optional

from .config import get_settings

logger = logging.getLogger("omni.redis")

NAMESPACE = "omni"

# Import mềm: nếu chưa `pip install redis`, cache tự tắt thay vì làm sập app.
try:
    import redis as _redis  # type: ignore

    _REDIS_IMPORTED = True
except ImportError:  # pragma: no cover - phụ thuộc môi trường
    _redis = None  # type: ignore
    _REDIS_IMPORTED = False


_client: "Optional[_redis.Redis]" = None  # type: ignore
_init_done = False

# Đo lường hiệu quả cache (an toàn đa luồng).
_metrics_lock = threading.Lock()
_metrics = {"hits": 0, "misses": 0, "sets": 0, "deletes": 0, "errors": 0}


def _bump(name: str, n: int = 1) -> None:
    with _metrics_lock:
        _metrics[name] += n


def _get_client():  # -> Optional[redis.Redis]
    """Trả về client Redis dùng chung (lazy), hoặc ``None`` nếu không khả dụng.

    Chỉ thử kết nối một lần cho mỗi tiến trình; kết quả được nhớ lại để các lần
    gọi sau không phải chịu thêm độ trễ kết nối khi Redis đang sập.
    """
    global _client, _init_done

    if _init_done:
        return _client

    _init_done = True

    if not _REDIS_IMPORTED:
        logger.info("Thư viện 'redis' chưa được cài — cache bị vô hiệu hoá.")
        return None

    url = get_settings().redis_url
    if not url:
        return None

    try:
        client = _redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=1.0,  # không treo app nếu Redis không phản hồi
            socket_timeout=1.0,
            health_check_interval=30,
        )
        client.ping()
        _client = client
        logger.info("Đã kết nối Redis tại %s", _safe_url(url))
    except Exception as exc:  # noqa: BLE001 - mọi lỗi đều fail-open
        logger.warning("Không kết nối được Redis (%s) — chạy không cache.", exc)
        _client = None

    return _client


def _safe_url(url: str) -> str:
    """Che mật khẩu trong URL khi log."""
    if "@" in url:
        scheme, _, tail = url.partition("//")
        return f"{scheme}//***@{tail.split('@', 1)[-1]}"
    return url


def reset_for_tests() -> None:
    """Buộc khởi tạo lại kết nối (dùng trong test khi đổi REDIS_URL)."""
    global _client, _init_done
    _client = None
    _init_done = False
    with _metrics_lock:
        for k in _metrics:
            _metrics[k] = 0


# --- Key builders ----------------------------------------------------------


def cache_key(*parts: Any) -> str:
    """Ghép key có namespace: cache_key('user', 'u_an', 'balance') -> omni:user:u_an:balance."""
    return ":".join([NAMESPACE, *(str(p) for p in parts)])


def user_history_key(user_id: str) -> str:
    return cache_key("user", user_id, "history")


def user_balance_key(user_id: str) -> str:
    return cache_key("user", user_id, "balance")


def user_summary_key(user_id: str, contact_id: Optional[str], period: str) -> str:
    return cache_key("user", user_id, "summary", contact_id or "all", period)


def user_pattern(user_id: str) -> str:
    return cache_key("user", user_id, "*")


# --- Thao tác cache --------------------------------------------------------


def is_available() -> bool:
    """True nếu Redis đang sẵn sàng phục vụ."""
    return _get_client() is not None


def get_cache(key: str) -> Optional[Any]:
    """Đọc + giải mã JSON từ cache. Trả ``None`` khi miss/lỗi (và đếm hit/miss)."""
    client = _get_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception as exc:  # noqa: BLE001
        _bump("errors")
        logger.debug("Lỗi đọc cache key=%s: %s", key, exc)
        return None
    if raw is None:
        _bump("misses")
        return None
    try:
        value = json.loads(raw)
        _bump("hits")
        return value
    except (json.JSONDecodeError, TypeError):
        delete_cache(key)  # cache hỏng -> dọn và coi như miss
        _bump("misses")
        return None


def set_cache(key: str, value: Any, ttl_seconds: int) -> bool:
    """Ghi giá trị (JSON-serializable) kèm TTL. Trả True nếu thành công."""
    client = _get_client()
    if client is None:
        return False
    try:
        payload = json.dumps(value, ensure_ascii=False, default=str)
        client.set(key, payload, ex=ttl_seconds)
        _bump("sets")
        return True
    except Exception as exc:  # noqa: BLE001
        _bump("errors")
        logger.debug("Lỗi ghi cache key=%s: %s", key, exc)
        return False


def delete_cache(*keys: str) -> int:
    """Xoá một/nhiều key cụ thể. Trả số key đã xoá."""
    client = _get_client()
    if client is None or not keys:
        return 0
    try:
        n = int(client.delete(*keys))
        _bump("deletes", n)
        return n
    except Exception as exc:  # noqa: BLE001
        _bump("errors")
        logger.debug("Lỗi xoá cache keys=%s: %s", keys, exc)
        return 0


def delete_pattern(pattern: str, batch: int = 256) -> int:
    """Xoá mọi key khớp pattern bằng SCAN (an toàn cho production, không block).

    Dùng để invalidate hàng loạt, vd ``omni:user:u_an:*``.
    """
    client = _get_client()
    if client is None:
        return 0
    deleted = 0
    try:
        cursor = 0
        while True:
            cursor, keys = client.scan(cursor=cursor, match=pattern, count=batch)
            if keys:
                deleted += int(client.delete(*keys))
            if cursor == 0:
                break
        _bump("deletes", deleted)
        return deleted
    except Exception as exc:  # noqa: BLE001
        _bump("errors")
        logger.debug("Lỗi xoá pattern=%s: %s", pattern, exc)
        return deleted


def invalidate_user(user_id: str) -> int:
    """Xoá toàn bộ cache liên quan tới một user (history/summary/balance)."""
    return delete_pattern(user_pattern(user_id))


# --- Quan sát --------------------------------------------------------------


def stats() -> dict:
    """Số liệu cache để soi qua /health/cache."""
    with _metrics_lock:
        m = dict(_metrics)
    total = m["hits"] + m["misses"]
    m["hit_rate"] = round(m["hits"] / total, 4) if total else None
    m["available"] = is_available()
    client = _get_client()
    if client is not None:
        try:
            m["keys"] = int(client.dbsize())
        except Exception:  # noqa: BLE001
            pass
    return m

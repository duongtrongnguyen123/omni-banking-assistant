"""Push-style notification event bus.

Powers the ``<ToastStack />`` in the phone frame. Banking actions
(transfer success, schedule fired, anomaly warnings, low balance, etc.)
publish events here; the ``/ws/events`` WebSocket subscribes per-user
and streams them down.

Design notes:

* **Per-user queue, not a fan-out broadcast.** Each ``user_id`` gets
  their own ``asyncio.Queue``. This means we don't accidentally leak
  user A's "Đã chuyển 5tr cho mẹ" toast to user B's phone — a strict
  win for privacy in a banking product. The trade-off is that we keep
  one queue alive per user even when nobody is connected, but the
  backlog cap (``_MAX_BACKLOG``) bounds memory.
* **Backlog replay on connect.** When a WebSocket opens, we drain the
  queue immediately so events that fired while the tab was closed
  still surface as toasts. After that we move into the live tail.
* **Fail-open everywhere.** ``publish()`` swallows queue-full
  conditions; ``subscribe()`` yields forever and lets the WS handler
  decide when to break. The chat path must keep working even if the
  notification system breaks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from typing import AsyncIterator, Literal, Optional

from pydantic import BaseModel, Field

log = logging.getLogger("omni.events")

EventKind = Literal[
    "transfer_success",
    "transfer_failed",
    "schedule_fired",
    "recurring_detected",
    "balance_low",
    "anomaly_warning",
]

EventSeverity = Literal["success", "info", "warn", "error"]

# Per-user backlog cap. Roughly 1KB per event × 64 ≈ 64KB worst case.
# Picks the oldest off the head when full so the *newest* toast wins.
_MAX_BACKLOG = 64


class Event(BaseModel):
    """A push notification toast payload.

    ``actionable_text`` is the string that gets pre-filled into the
    chat input when the user clicks the toast — e.g. ``"Lặp lại giao
    dịch vừa rồi"`` for a successful transfer.
    """

    kind: EventKind
    title: str
    body: str = ""
    severity: EventSeverity = "info"
    ts: float = Field(default_factory=lambda: time.time())
    actionable_text: Optional[str] = None


class EventBus:
    """In-process per-user pub/sub.

    Thread-safety: we only ever touch ``_queues`` from the asyncio
    event loop (publish is sync but calls ``put_nowait`` which is
    loop-safe to call from the same thread). The store callers are
    synchronous request handlers — they share the same loop because
    FastAPI runs them in the loop's threadpool but the queue itself
    only matters when consumed by the WS coroutine.
    """

    def __init__(self) -> None:
        # ``defaultdict`` so that publishing for a not-yet-subscribed
        # user still queues the event. The subscriber will drain it on
        # connect.
        self._queues: dict[str, asyncio.Queue[Event]] = defaultdict(
            lambda: asyncio.Queue(maxsize=_MAX_BACKLOG)
        )

    def _queue_for(self, user_id: str) -> asyncio.Queue[Event]:
        # Hitting defaultdict via __getitem__ creates the queue. We do
        # this in a helper to keep the call sites readable.
        return self._queues[user_id]

    def publish(self, user_id: str, event: Event) -> None:
        """Drop an event onto the user's queue.

        Non-blocking. If the queue is full we discard the oldest entry
        rather than the newest — toasts are about right-now state,
        stale ones aren't worth surfacing.
        """
        q = self._queue_for(user_id)
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()
            try:
                q.put_nowait(event)
            except Exception:  # pragma: no cover — extremely unlikely
                log.warning("dropped event for %s: queue churn", user_id)

    async def subscribe(self, user_id: str) -> AsyncIterator[Event]:
        """Async generator that yields events for a user.

        Drains backlog first, then awaits the next push. Cancellation
        bubbles up naturally when the caller's task is cancelled (e.g.
        WS disconnect).
        """
        q = self._queue_for(user_id)
        while True:
            event = await q.get()
            yield event

    # Test / introspection hooks ------------------------------------

    def pending_count(self, user_id: str) -> int:
        return self._queues[user_id].qsize() if user_id in self._queues else 0

    def reset(self) -> None:
        """Wipe all queues — only used by tests."""
        self._queues.clear()


_bus: Optional[EventBus] = None


def get_bus() -> EventBus:
    """Lazy singleton so tests can monkeypatch ``_bus`` if needed."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def publish(user_id: str, event: Event) -> None:
    """Convenience: ``events.publish(user_id, Event(...))``.

    Safe to call from synchronous request handlers — the bus uses
    ``put_nowait`` under the hood.
    """
    try:
        get_bus().publish(user_id, event)
    except Exception as e:  # pragma: no cover — fail-open
        log.warning("event publish failed: %s", e)
    # Metrics: count every published toast by kind. Wrapped so a metrics
    # registration bug can't break the per-user event bus.
    try:
        from . import metrics as _m

        _m.toast_published_total.inc(kind=event.kind)
    except Exception:
        pass


# Convenience constructors ------------------------------------------
# These exist so callers don't have to memorize severity defaults for
# each kind. ``publish_*`` keeps the publish sites tiny and readable.


def publish_transfer_success(
    user_id: str, *, recipient_name: str, amount_vnd: int
) -> None:
    publish(
        user_id,
        Event(
            kind="transfer_success",
            severity="success",
            title="Chuyển khoản thành công",
            body=f"Đã chuyển {amount_vnd:,}đ cho {recipient_name}.".replace(",", "."),
            actionable_text="Lặp lại giao dịch vừa rồi",
        ),
    )


def publish_transfer_failed(user_id: str, *, reason: str) -> None:
    publish(
        user_id,
        Event(
            kind="transfer_failed",
            severity="error",
            title="Giao dịch thất bại",
            body=reason,
        ),
    )


def publish_schedule_created(
    user_id: str, *, recipient_name: str, amount_vnd: int, cron: str
) -> None:
    publish(
        user_id,
        Event(
            kind="schedule_fired",  # reused kind: "lịch đã chạy / đã đặt"
            severity="info",
            title="Đã đặt lịch chuyển khoản",
            body=(
                f"Sẽ chuyển {amount_vnd:,}đ cho {recipient_name} theo lịch ({cron})."
            ).replace(",", "."),
        ),
    )


def publish_schedule_fired(
    user_id: str, *, recipient_name: str, amount_vnd: int
) -> None:
    publish(
        user_id,
        Event(
            kind="schedule_fired",
            severity="info",
            title="Lịch định kỳ đã thực hiện",
            body=(
                f"Đã chuyển {amount_vnd:,}đ cho {recipient_name} theo lịch định kỳ."
            ).replace(",", "."),
            actionable_text="Xem các lịch định kỳ",
        ),
    )


def publish_balance_low(user_id: str, *, balance_vnd: int) -> None:
    publish(
        user_id,
        Event(
            kind="balance_low",
            severity="warn",
            title="Số dư thấp",
            body=(
                f"Số dư tài khoản chính chỉ còn {balance_vnd:,}đ. "
                "Bạn cân nhắc nạp thêm nhé."
            ).replace(",", "."),
            actionable_text="Số dư của tôi",
        ),
    )


def publish_anomaly_warning(user_id: str, *, message: str) -> None:
    publish(
        user_id,
        Event(
            kind="anomaly_warning",
            severity="warn",
            title="Giao dịch bất thường",
            body=message,
        ),
    )


def publish_recurring_detected(
    user_id: str, *, count: int
) -> None:
    publish(
        user_id,
        Event(
            kind="recurring_detected",
            severity="info",
            title="Phát hiện khoản định kỳ",
            body=f"Mình nhận thấy bạn có {count} khoản chi định kỳ hàng tháng.",
            actionable_text="Mình có khoản nào trả định kỳ không?",
        ),
    )

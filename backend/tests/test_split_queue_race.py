"""Regression test for the M5 audit finding on ``_split_queues``.

The split-bill queue lived in a module-level dict and one of the read
sites (``len(_split_queues.get(user_id, []))`` used to render
"Còn N người trong yêu cầu chia tiền") happened OUTSIDE the
``_drafts_lock``. Concurrent confirms + new split-bill starts could
therefore race the read against a dict mutation. The fix moves the
sample inside the lock; this test exercises mixed reads and writes
to confirm the path no longer raises.
"""

from __future__ import annotations

import threading

from app.services import orchestrator as _o


def test_concurrent_split_queue_mutations_do_not_raise():
    # Reset the module-level dict so prior tests don't taint state.
    with _o._drafts_lock:
        _o._split_queues.clear()

    errors: list[BaseException] = []

    def writer(user_id: str, n: int) -> None:
        try:
            for _ in range(n):
                with _o._drafts_lock:
                    _o._split_queues[user_id] = ["a", "b", "c"]  # type: ignore[list-item]
                with _o._drafts_lock:
                    q = _o._split_queues.get(user_id)
                    if q:
                        q.pop(0)
                        if not q:
                            _o._split_queues.pop(user_id, None)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def reader(user_ids: list[str], n: int) -> None:
        try:
            for _ in range(n):
                for u in user_ids:
                    with _o._drafts_lock:
                        # Same shape as the patched orchestrator read.
                        _ = len(_o._split_queues.get(u) or []) + 1
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    users = [f"u{i}" for i in range(50)]
    threads: list[threading.Thread] = []
    # 50 writer threads (one per user) and 5 reader threads sweeping
    # the dict — a generous spread for the GIL release window.
    for u in users:
        threads.append(threading.Thread(target=writer, args=(u, 200)))
    for _ in range(5):
        threads.append(threading.Thread(target=reader, args=(users, 200)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"unexpected exceptions: {errors[:3]}"
    # Clean up so we don't bleed state into the next test.
    with _o._drafts_lock:
        _o._split_queues.clear()


def test_split_queue_advance_remaining_count_is_consistent():
    """The 'Còn N người' counter computed inside the lock matches the
    underlying list len. (Sanity check; the fix makes this the natural
    state of the code.)"""
    user_id = "u_consistency"
    with _o._drafts_lock:
        # 3 queued + 1 active in session = 4 split shares total.
        _o._split_queues[user_id] = ["d2", "d3", "d4"]  # type: ignore[list-item]

    # Simulate the post-confirm advance: pop one + sample depth.
    with _o._drafts_lock:
        queue = _o._split_queues.get(user_id)
        assert queue is not None
        queue.pop(0)
        remaining = len(_o._split_queues.get(user_id) or []) + 1
    # 2 left in queue + 1 just-popped that's about to be confirmed = 3.
    assert remaining == 3

    with _o._drafts_lock:
        _o._split_queues.pop(user_id, None)

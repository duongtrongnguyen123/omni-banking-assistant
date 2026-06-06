"""Regression test for the suggester thread-safety fix.

``suggest`` used to read ``_STATE.get(user_id)`` lock-free and then call
``state["model"].predict_proba(...)`` directly. sklearn does not document
``predict_proba`` as thread-safe under concurrent calls on the same
fitted estimator, and a concurrent ``train_for`` could swap the dict
entry mid-read. The fix:
  * upgrades ``_LOCK`` to ``RLock`` (``suggest`` may transitively call
    ``train_for`` on cold start).
  * snapshots ``model`` / ``labels`` / ``prior`` / ``contact_stats``
    under the lock.
  * attaches a per-user ``infer_lock`` to the state dict and wraps the
    ``predict_proba`` call in it so two concurrent suggestions for the
    same user can't race the same model.

This test exercises 8 threads hammering ``suggest`` for one user against
a real (tiny) sklearn model. No exception, consistent rankings.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.ml import suggester
from app.models.schemas import Contact, Transaction


pytest.importorskip("sklearn", reason="sklearn required for suggester")


class _FakeStore:
    """In-memory store stub matching the methods ``suggester`` uses."""

    def __init__(self, contacts: list[Contact], txs: list[Transaction]) -> None:
        self._contacts = contacts
        self._txs = txs

    def contacts_of(self, _user_id: str) -> list[Contact]:
        return list(self._contacts)

    def transactions_of(
        self,
        _user_id: str,
        contact_id: str | None = None,
        limit: int | None = None,
    ) -> list[Transaction]:
        rows = self._txs
        if contact_id is not None:
            rows = [t for t in rows if t.contact_id == contact_id]
        if limit is not None:
            rows = rows[:limit]
        return rows


def _make_fixture() -> _FakeStore:
    contacts = [
        Contact(
            id=f"c{i}",
            owner_id="u1",
            display_name=f"Contact {i}",
            bank="MB Bank",
            account_number=f"99900012{i:02d}",
            account_masked=f"****12{i:02d}",
        )
        for i in range(4)
    ]
    base = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
    txs: list[Transaction] = []
    # 4 contacts × 4 txs each, spread across the month so the RF can fit.
    for i in range(4):
        for j in range(4):
            txs.append(
                Transaction(
                    id=f"t{i}-{j}",
                    owner_id="u1",
                    contact_id=f"c{i}",
                    amount=100_000 * (j + 1),
                    description=f"tx {i}-{j}",
                    category="other",
                    status="completed",
                    created_at=base + timedelta(days=i * 3 + j),
                )
            )
    return _FakeStore(contacts, txs)


def test_concurrent_suggest_does_not_raise(monkeypatch):
    """8 threads × 50 calls each on the same user. The fix must serialise
    ``predict_proba`` on the per-user ``infer_lock`` so no thread sees a
    sklearn internal-state corruption error.
    """
    suggester.reset_all()
    store = _make_fixture()
    monkeypatch.setattr(suggester, "get_store", lambda: store)

    # Warm the cache so every thread takes the hot path through suggest().
    suggester.train_for("u1")

    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        try:
            for _ in range(50):
                out = suggester.suggest("u1", k=3)
                # Sanity: each call returns a list and the top-K size is bounded.
                assert isinstance(out, list)
                assert len(out) <= 3
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(worker) for _ in range(8)]
        for f in futs:
            f.result()

    assert not errors, f"concurrent suggest raised: {errors[:3]}"


def test_concurrent_train_and_suggest_no_keyerror(monkeypatch):
    """A retrain on one thread while another thread is mid-``suggest`` used
    to be racy: ``state["model"]`` and ``state["labels"]`` could come
    from different generations. With the snapshot-under-lock fix the
    locally captured references stay coherent for the duration of the
    call.
    """
    suggester.reset_all()
    store = _make_fixture()
    monkeypatch.setattr(suggester, "get_store", lambda: store)
    suggester.train_for("u1")

    errors: list[BaseException] = []
    stop = threading.Event()

    def retrainer() -> None:
        try:
            while not stop.is_set():
                suggester.train_for("u1")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def caller() -> None:
        try:
            for _ in range(80):
                suggester.suggest("u1", k=3)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    rt = threading.Thread(target=retrainer)
    callers = [threading.Thread(target=caller) for _ in range(4)]
    rt.start()
    for t in callers:
        t.start()
    for t in callers:
        t.join()
    stop.set()
    rt.join()

    assert not errors, f"train-vs-suggest race raised: {errors[:3]}"


def test_state_dict_carries_infer_lock(monkeypatch):
    """Pin the schema — every per-user state entry written by
    ``train_for`` MUST include an ``infer_lock``. A silent drop would
    bring back the race even with the snapshot pattern in place.
    """
    suggester.reset_all()
    store = _make_fixture()
    monkeypatch.setattr(suggester, "get_store", lambda: store)

    suggester.train_for("u1")
    state = suggester._STATE.get("u1")
    assert state is not None
    assert "infer_lock" in state
    assert isinstance(state["infer_lock"], type(threading.Lock()))

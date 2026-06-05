"""Tests for the push-notification event bus."""

from __future__ import annotations

import asyncio

import pytest

from app.services import events


@pytest.fixture(autouse=True)
def _reset_bus():
    """Fresh bus per test so ordering assertions stay deterministic."""
    events.get_bus().reset()
    yield
    events.get_bus().reset()


def test_publish_then_subscribe_preserves_order():
    """Three events should arrive in the same order they were published."""

    async def _run():
        events.publish_transfer_success(
            "u_test", recipient_name="Mẹ", amount_vnd=5_000_000
        )
        events.publish_anomaly_warning(
            "u_test", message="Số tiền cao gấp 4 lần thường ngày."
        )
        events.publish_balance_low("u_test", balance_vnd=80_000)

        gen = events.get_bus().subscribe("u_test")
        received: list[events.Event] = []
        for _ in range(3):
            # ``asyncio.wait_for`` guards us if the queue is somehow
            # empty — the test would hang otherwise.
            ev = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
            received.append(ev)
        return received

    received = asyncio.run(_run())

    assert [e.kind for e in received] == [
        "transfer_success",
        "anomaly_warning",
        "balance_low",
    ]
    assert [e.severity for e in received] == ["success", "warn", "warn"]
    assert "Mẹ" in received[0].body
    assert received[0].actionable_text == "Lặp lại giao dịch vừa rồi"


def test_per_user_isolation():
    """User A's events must not leak into user B's stream."""

    async def _run():
        events.publish_transfer_success(
            "u_alice", recipient_name="Bob", amount_vnd=1_000_000
        )
        events.publish_anomaly_warning("u_bob", message="bất thường")

        gen_alice = events.get_bus().subscribe("u_alice")
        gen_bob = events.get_bus().subscribe("u_bob")

        a = await asyncio.wait_for(gen_alice.__anext__(), timeout=1.0)
        b = await asyncio.wait_for(gen_bob.__anext__(), timeout=1.0)
        return a, b

    a, b = asyncio.run(_run())
    assert a.kind == "transfer_success"
    assert b.kind == "anomaly_warning"


def test_backlog_drains_on_subscribe():
    """Events published before subscription are not lost."""

    async def _run():
        for amt in (1_000_000, 2_000_000, 3_000_000):
            events.publish_transfer_success(
                "u_lurker", recipient_name="X", amount_vnd=amt
            )
        # Subscribe *after* publishing — the queue should still have all 3.
        assert events.get_bus().pending_count("u_lurker") == 3
        gen = events.get_bus().subscribe("u_lurker")
        drained = []
        for _ in range(3):
            drained.append(await asyncio.wait_for(gen.__anext__(), timeout=1.0))
        return drained

    drained = asyncio.run(_run())
    assert len(drained) == 3
    # Order preserved: oldest first.
    amounts = [int(e.body.replace(".", "").split("đ")[0].split()[-1]) for e in drained]
    assert amounts == [1_000_000, 2_000_000, 3_000_000]

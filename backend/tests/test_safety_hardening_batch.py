"""Regression suite for the safety-hardening batch (fraud model + orchestrator).

Covers four audit findings landed together:

1. ``FRAUD_RISK_THRESHOLD`` is overridable via ``OMNI_FRAUD_RISK_THRESHOLD``
   and falls back to the documented default for bad inputs.
2. ``_calibrate`` does not saturate to 1.0 when the user's training
   history is constant-amount (flat ``p95 - p50``).
3. ``_models`` / ``_last_retrain_attempt`` survive concurrent
   ``score_draft`` + ``train_user`` calls without exceptions or torn reads.
4. The orchestrator clears a non-OTP zombie draft when an intent handler
   raises, so the next turn isn't trapped in the continuation branch.

Each block is independent — fraud tests don't touch the orchestrator and
vice versa — but they share this file so the audit-batch can be skipped
or reviewed as a unit.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Bug 1 — env override + bad-input fallback
# ---------------------------------------------------------------------------


def test_fraud_threshold_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNI_FRAUD_RISK_THRESHOLD", raising=False)
    from app.safety import fraud_model

    assert fraud_model._recompute_threshold() == pytest.approx(0.5)


def test_fraud_threshold_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_FRAUD_RISK_THRESHOLD", "0.72")
    from app.safety import fraud_model

    assert fraud_model._recompute_threshold() == pytest.approx(0.72)


def test_fraud_threshold_bad_input_keeps_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OMNI_FRAUD_RISK_THRESHOLD", "not-a-number")
    from app.safety import fraud_model

    with caplog.at_level(logging.WARNING, logger=fraud_model.__name__):
        threshold = fraud_model._recompute_threshold()

    assert threshold == pytest.approx(0.5)
    assert any(
        "OMNI_FRAUD_RISK_THRESHOLD" in rec.getMessage() for rec in caplog.records
    ), "Expected a warning about the bad env value"


def test_fraud_threshold_out_of_range_keeps_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OMNI_FRAUD_RISK_THRESHOLD", "1.5")
    from app.safety import fraud_model

    with caplog.at_level(logging.WARNING, logger=fraud_model.__name__):
        assert fraud_model._recompute_threshold() == pytest.approx(0.5)
    assert any("outside" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# Bug 2 — flat-history calibration must not saturate
# ---------------------------------------------------------------------------


def test_calibrate_at_p95_lands_at_centre_for_flat_history() -> None:
    """For a flat-history user, ``p50 == p95``. The calibrated score at
    raw == p95 must still be 0.5 (i.e. the spread floor doesn't shift the
    sigmoid centre), so an unchanged transaction reads as "median normal"
    rather than alarming.
    """
    from app.safety.fraud_model import _calibrate

    assert _calibrate(0.05, 0.05, 0.05) == pytest.approx(0.5, abs=1e-9)


def test_calibrate_flat_history_outlier_below_saturation() -> None:
    """Direct unit-level check of the spread floor.

    Pre-fix: ``spread = max(p95 - p50, 1e-6)`` — for a flat-history user
    with ``p50 == p95 == 0.05`` and a 2x-of-p95 raw outlier, ``z`` would
    be ``0.05 / 1e-6 = 50000``, and the calibrated score would saturate
    to ``1.0`` to machine precision.

    Post-fix: ``max(p95 - p50, abs(p95) * 0.05, 1e-3)`` keeps ``z`` finite
    enough that mid-range outliers don't get pinned to the alarm tail,
    leaving the rule engine room to distinguish "slightly above normal"
    from "catastrophic".
    """
    from app.safety.fraud_model import _calibrate

    # Collapsed-quantile case: ``p50 == p95``. Pre-fix spread = 1e-6 so a
    # tiny raw bump (1e-3 above p95) blew the sigmoid out to 1.0. Post-fix
    # the relative + absolute floor keeps the bump in mid-range.
    p50 = p95 = 0.05
    nearby_raw = p95 + 1e-3  # smallest meaningful raw bump above the median
    score = _calibrate(nearby_raw, p50, p95)

    assert score < 0.99, (
        f"Calibrated score saturated to 1.0 on flat history: got {score!r}. "
        "Expected the relative-floor guard to keep small raw bumps inside "
        "the dynamic range (sigmoid mid-band)."
    )
    # And differentiability is preserved: a bigger raw bump ranks strictly
    # higher than a smaller one — the spread floor doesn't flatten the
    # curve.
    score_bigger = _calibrate(p95 + 1e-2, p50, p95)
    assert score_bigger > score, (
        "Calibration must rank larger raw deltas strictly higher even "
        f"under the flat-history floor: got near={score!r}, bigger={score_bigger!r}."
    )


def test_calibrate_normal_spread_unchanged() -> None:
    """When there's real spread we shouldn't perturb the existing curve."""
    from app.safety.fraud_model import _calibrate

    p50, p95 = 0.10, 0.40  # well above the relative floor (0.40 * 0.05 = 0.02)
    # Old formula: spread = 0.30, z = (raw - 0.40) / 0.30
    # The relative floor (0.02) and absolute floor (1e-3) lose; result
    # should match the historical numeric output to ~1e-9.
    expected = 1.0 / (1.0 + math.exp(-3.0 * (0.55 - 0.40) / 0.30))
    assert _calibrate(0.55, p50, p95) == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# Bug 3 — thread-safe model cache
# ---------------------------------------------------------------------------


def _make_synthetic_history(
    user_id: str, n: int = 80
) -> tuple[str, list[Any]]:
    """Build ``n`` completed outgoing tx — varied enough for sklearn to fit
    a model. Returns ``(user_id, txs)``.
    """
    from app.models.schemas import Transaction

    base = datetime.now(timezone.utc) - timedelta(days=120)
    txs = []
    for i in range(n):
        amt = 100_000 + (i % 7) * 25_000
        when = base + timedelta(days=i, hours=(i * 3) % 24)
        txs.append(
            Transaction(
                id=f"tx_{user_id}_{i}",
                owner_id=user_id,
                contact_id=f"c_{i % 5}",
                amount=amt,
                description="seed",
                category="other",
                status="completed",
                created_at=when,
            )
        )
    return user_id, txs


def test_models_dict_survives_concurrent_score_and_retrain() -> None:
    """8 reader threads call ``score_draft`` while one writer thread keeps
    swapping the user's model via ``train_user``. No exception, no None
    deref — even when the cache entry is being replaced mid-flight.
    """
    from app.safety import fraud_model

    if not fraud_model.is_enabled():
        pytest.skip("sklearn unavailable — thread-safety test irrelevant")

    user_id, txs = _make_synthetic_history("u_thread_test", n=80)
    fraud_model.clear_models()
    fitted = fraud_model.train_user(user_id, txs)
    assert fitted is not None, "synthetic history should fit a model"

    stop = threading.Event()
    errors: list[BaseException] = []
    scores: list[float | None] = []
    score_lock = threading.Lock()

    def reader() -> None:
        try:
            for _ in range(50):
                if stop.is_set():
                    return
                s = fraud_model.score_draft(
                    user_id=user_id,
                    amount=250_000,
                    when=datetime.now(timezone.utc),
                    contact_id="c_2",
                    category="other",
                )
                with score_lock:
                    scores.append(s)
        except BaseException as exc:  # noqa: BLE001 — we want to capture anything
            errors.append(exc)

    def writer() -> None:
        try:
            # Re-train repeatedly so the dict entry is swapped under the readers.
            for _ in range(20):
                if stop.is_set():
                    return
                fraud_model.train_user(user_id, txs)
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        with ThreadPoolExecutor(max_workers=9) as pool:
            futs = [pool.submit(reader) for _ in range(8)]
            futs.append(pool.submit(writer))
            for f in futs:
                f.result(timeout=30)
    finally:
        stop.set()

    assert not errors, f"Concurrent score/retrain raised: {errors!r}"
    # All readers should have produced at least one finite score.
    assert any(s is not None and math.isfinite(s) for s in scores), (
        "Expected at least one valid score; got: "
        + repr(scores[:5])
    )
    # And none of them returned NaN / inf — the snapshot semantics must
    # protect ``predict_proba`` from a half-updated model.
    for s in scores:
        if s is None:
            continue
        assert math.isfinite(s), f"non-finite score under contention: {s!r}"


# ---------------------------------------------------------------------------
# Bug 4 — orchestrator zombie-draft rollback
# ---------------------------------------------------------------------------


def test_orchestrator_clears_zombie_draft_when_handler_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler that stashes a draft via ``set_draft`` and then raises
    must not leave the draft on the session — otherwise the next turn
    enters the draft-continuation branch and the user can't cancel.
    OTP-awaiting drafts are exempt (transient errors must not drop them).
    """
    from app.context.session import session_for
    from app.models.schemas import TransactionDraft
    from app.services import orchestrator

    user_id = "u_zombie_test"
    session = session_for(user_id)
    session.clear_draft()

    def _exploding_handler(uid: str, *_args: Any, **_kwargs: Any) -> None:
        # Mimic a real handler: stash an in-progress draft, *then* blow up.
        session_for(uid).set_draft(
            TransactionDraft(id="dr_zombie", awaiting_otp=False)
        )
        raise RuntimeError("synthetic handler failure")

    monkeypatch.setattr(orchestrator, "_handle_balance", _exploding_handler)

    with pytest.raises(RuntimeError, match="synthetic"):
        orchestrator.handle_message(user_id, "số dư bao nhiêu?")

    assert session_for(user_id).current_draft is None, (
        "Expected the zombie draft to be cleared after the handler raised."
    )


def test_orchestrator_preserves_otp_draft_when_handler_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OTP-awaiting drafts MUST survive a transient handler error — the
    user already cleared the confirm gate and existing semantics (cancel/
    retry in routes/chat.py) depend on the draft staying put.
    """
    from app.context.session import session_for
    from app.models.schemas import TransactionDraft
    from app.services import orchestrator

    user_id = "u_zombie_otp_test"
    session = session_for(user_id)
    session.clear_draft()

    def _exploding_handler(uid: str, *_args: Any, **_kwargs: Any) -> None:
        session_for(uid).set_draft(
            TransactionDraft(id="dr_otp", awaiting_otp=True)
        )
        raise RuntimeError("synthetic handler failure")

    monkeypatch.setattr(orchestrator, "_handle_balance", _exploding_handler)

    with pytest.raises(RuntimeError):
        orchestrator.handle_message(user_id, "số dư bao nhiêu?")

    surviving = session_for(user_id).current_draft
    assert surviving is not None and surviving.awaiting_otp, (
        "OTP-awaiting drafts must survive a transient handler error."
    )
    # Clean up so a later test doesn't trip the draft-continuation branch.
    session_for(user_id).clear_draft()

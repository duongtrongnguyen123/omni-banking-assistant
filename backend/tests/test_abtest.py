"""Unit tests for the A/B router and Thompson-sampling bandit.

These tests do NOT exercise the suggester end-to-end — they stand the
``abtest`` + ``bandit`` modules up in isolation so a CI run is fast and
deterministic.
"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolate_state():
    """Ensure each test starts with a clean registry and a tmp persist path."""
    # Force the framework on (conftest doesn't touch it but a dev shell
    # might have OMNI_DISABLE_ABTEST=1 set).
    prev_disable = os.environ.pop("OMNI_DISABLE_ABTEST", None)
    tmp = tempfile.NamedTemporaryFile(prefix="bandit_test_", suffix=".json",
                                      delete=False)
    tmp.close()
    os.environ["OMNI_BANDIT_STATE_PATH"] = tmp.name

    from app.ml import abtest
    abtest._STATE.arms.clear()  # noqa: SLF001
    abtest._STATE.order.clear()  # noqa: SLF001

    yield

    try:
        os.unlink(tmp.name)
    except OSError:
        pass
    os.environ.pop("OMNI_BANDIT_STATE_PATH", None)
    if prev_disable is not None:
        os.environ["OMNI_DISABLE_ABTEST"] = prev_disable


def test_register_and_list_arms():
    from app.ml import abtest

    abtest.register_arm("a", (1.0, 0.0, 0.0))
    abtest.register_arm("b", (0.0, 1.0, 0.0))
    assert abtest.arm_names() == ["a", "b"]
    assert abtest.get_arm_weights("a") == (1.0, 0.0, 0.0)


def test_register_arm_validates_weights():
    from app.ml import abtest
    with pytest.raises(ValueError):
        abtest.register_arm("bad", (1.0, 0.0))  # type: ignore[arg-type]


def test_pick_arm_is_deterministic():
    from app.ml import abtest

    abtest.register_defaults()
    a1 = abtest.pick_arm("u_user_x")
    a2 = abtest.pick_arm("u_user_x")
    assert a1 == a2
    # All four arms are valid
    assert a1 in abtest.arm_names()


def test_disabled_mode_returns_default():
    os.environ["OMNI_DISABLE_ABTEST"] = "1"
    from app.ml import abtest

    abtest.register_defaults()
    assert abtest.pick_arm("u_x") == "default"
    abtest.record_outcome("u_x", "tree_freq", True)
    # Counters should remain zero — record was suppressed.
    assert abtest.report()["tree_freq"]["trials"] == 0


def test_record_outcome_updates_counters():
    from app.ml import abtest

    abtest.register_defaults()
    abtest.record_outcome("u", "tree_freq", True)
    abtest.record_outcome("u", "tree_freq", False)
    abtest.record_outcome("u", "tree_freq", True)
    r = abtest.report()["tree_freq"]
    assert r["trials"] == 3
    assert r["hits"] == 2
    assert r["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)
    lo, hi = r["ci"]
    assert 0.0 <= lo <= r["hit_rate"] <= hi <= 1.0


def test_wilson_ci_boundaries():
    """Zero-trials edge case and ratio-of-1 / ratio-of-0 should not crash."""
    from app.ml.abtest import _wilson_ci

    lo, hi = _wilson_ci(0, 0)
    assert (lo, hi) == (0.0, 1.0)

    lo, hi = _wilson_ci(0, 10)
    assert lo == 0.0 and hi < 0.5

    lo, hi = _wilson_ci(10, 10)
    assert hi == 1.0 and lo > 0.5


def test_bandit_gated_by_min_trials():
    """Below the threshold the router falls back to deterministic hash."""
    from app.ml import abtest, bandit

    abtest.register_defaults()
    # Force the gate high so the bandit never activates.
    prev = bandit.MIN_TRIALS_PER_ARM
    bandit.MIN_TRIALS_PER_ARM = 1000
    try:
        # Pick should be the deterministic hash bucket; consistent across calls.
        a1 = abtest.pick_arm("u_user_y")
        a2 = abtest.pick_arm("u_user_y")
        assert a1 == a2
        # Even after recording some outcomes (still below gate) we stay sticky.
        for _ in range(10):
            abtest.record_outcome("u_user_y", a1, True)
        assert abtest.pick_arm("u_user_y") == a1
    finally:
        bandit.MIN_TRIALS_PER_ARM = prev


def test_bandit_activates_after_threshold():
    """After every arm has ≥ threshold trials Thompson picks the best arm
    most of the time. We don't assert the exact arm — Thompson is
    stochastic — but we assert that the call no longer returns the
    deterministic-hash bucket every time."""
    from app.ml import abtest, bandit

    abtest.register_defaults()
    prev = bandit.MIN_TRIALS_PER_ARM
    bandit.MIN_TRIALS_PER_ARM = 5
    bandit.seed(1234)
    try:
        # Seed all arms past the gate. Make "tree_freq" overwhelmingly best.
        for _ in range(20):
            abtest.record_outcome("seed", "tree_freq", True)
            abtest.record_outcome("seed", "rule_heavy", False)
            abtest.record_outcome("seed", "balanced", False)
            abtest.record_outcome("seed", "tree_only", False)
        # Draw many samples — tree_freq should dominate.
        picks = [abtest.pick_arm("u_user_z") for _ in range(200)]
        from collections import Counter
        c = Counter(picks)
        assert c.most_common(1)[0][0] == "tree_freq"
    finally:
        bandit.MIN_TRIALS_PER_ARM = prev


def test_reset_clears_counters_and_state():
    from app.ml import abtest, bandit

    abtest.register_defaults()
    for _ in range(5):
        abtest.record_outcome("u", "tree_freq", True)
    assert abtest.report()["tree_freq"]["trials"] == 5
    # File should exist after the save_state hook fires.
    state_path = bandit._state_path()
    assert state_path.is_file()

    abtest.reset()
    assert abtest.report()["tree_freq"]["trials"] == 0
    assert not state_path.is_file()


def test_bandit_state_round_trip():
    """Persist + reload yields the same counters."""
    from app.ml import abtest, bandit

    abtest.register_defaults()
    abtest.record_outcome("u", "tree_freq", True)
    abtest.record_outcome("u", "tree_freq", False)
    abtest.record_outcome("u", "rule_heavy", True)
    bandit.save_state()

    # Wipe in-memory counters and reload.
    for a in abtest._STATE.arms.values():  # noqa: SLF001
        a.trials = 0
        a.hits = 0
    bandit.load_state()

    assert abtest.report()["tree_freq"]["trials"] == 2
    assert abtest.report()["tree_freq"]["hits"] == 1
    assert abtest.report()["rule_heavy"]["trials"] == 1

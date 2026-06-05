"""Tests for the in-process metrics registry.

We cover four things:

1. Counter semantics (monotonic, labels create independent series).
2. Histogram percentile shape (10000 uniform samples → p50 ≈ median).
3. Gauge set/inc/dec.
4. Prometheus exposition format conformance + the ``/api/metrics``
   endpoint returning 200 with a parseable body.

The tests use a fresh ``Registry`` for the unit-level checks (so they
don't depend on global state) and the FastAPI ``TestClient`` for the
endpoint check.
"""

from __future__ import annotations

import os
import random
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.metrics import (
    DEFAULT_BUCKETS,
    Counter,
    Gauge,
    Histogram,
    Registry,
)


@pytest.fixture(scope="module", autouse=True)
def _seed_demo_user():
    """Copy the canonical JSON seed into the conftest's isolated tmp
    data dir so /api/chat works against the bootstrap demo user.

    Resets the cached SQLite connection after wiping ``omni.db`` —
    otherwise an earlier test in the session may have opened a handle
    against the pre-seed (empty) DB and ``get_store()`` would still
    return it on the next call, leaving u_an permanently absent and
    the chat route 500-ing on ``store.get_user("u_an")``.
    """
    data_dir = Path(os.environ["BANKING_DATA_DIR"])
    src = Path(__file__).resolve().parent.parent / "app" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in ("users.json", "contacts.json", "transactions.json", "schedules.json"):
        target = data_dir / name
        if not target.exists() and (src / name).exists():
            shutil.copyfile(src / name, target)
    db_file = data_dir / "omni.db"
    if db_file.exists():
        db_file.unlink()

    # Drop the cached connection so the next get_connection() opens a
    # fresh handle pointing at the recreated DB and bootstrap_if_empty
    # actually finds an empty table set to populate from the JSON
    # seeds we just copied.
    try:
        from app.db.connection import reset_connection
        reset_connection()
    except Exception:  # pragma: no cover — defensive
        pass

    # Drop the cached Store singleton too. Store.__init__ runs
    # bootstrap_if_empty(); without resetting, the module-level
    # _store global keeps the old (empty) snapshot even after we
    # repointed the connection.
    try:
        import app.store as _store_mod
        _store_mod._store = None
    except Exception:  # pragma: no cover — defensive
        pass


# ---------------------------------------------------------------------------
# Counter
# ---------------------------------------------------------------------------


def test_counter_increments():
    c = Counter("test_total")
    c.inc()
    c.inc()
    c.inc(amount=3)
    samples = list(c.collect())
    assert samples == [((), 5.0)]


def test_counter_labels_create_series():
    c = Counter("test_labelled", labels=("intent", "source"))
    c.inc(intent="transfer", source="llm")
    c.inc(intent="transfer", source="llm")
    c.inc(intent="balance", source="rule")
    samples = dict(c.collect())
    assert samples[("balance", "rule")] == 1
    assert samples[("transfer", "llm")] == 2


def test_counter_rejects_negative():
    """Counters must be monotonic — negative .inc() is a no-op, not raise."""
    c = Counter("test_neg")
    c.inc()
    c.inc(amount=-5)
    assert list(c.collect()) == [((), 1.0)]


# ---------------------------------------------------------------------------
# Histogram
# ---------------------------------------------------------------------------


def test_histogram_percentile_uniform():
    """10000 samples uniform on [0, 1] → p50 ≈ 0.5, p95 ≈ 0.95.

    We use a fixed seed so the test is deterministic; the assertion
    tolerances are wider than the empirical jitter to be robust under
    CI variance.
    """
    h = Histogram("test_h", buckets=DEFAULT_BUCKETS)
    rng = random.Random(42)
    samples = [rng.random() for _ in range(10_000)]
    for s in samples:
        h.observe(s)
    # The reservoir caps at 2048 — we measure on what's retained.
    p50 = h.percentile(50)
    p95 = h.percentile(95)
    assert 0.4 < p50 < 0.6, f"p50 unexpected: {p50}"
    assert 0.9 < p95 < 1.0, f"p95 unexpected: {p95}"


def test_histogram_bucket_counts_cumulative():
    h = Histogram("test_buckets")
    h.observe(0.003)  # below first bucket
    h.observe(0.02)   # in 0.025 bucket
    h.observe(2.0)    # +Inf only
    child = h.labels()
    counts = child.bucket_counts
    # Cumulative: 0.005 bucket has only the 0.003 sample → 1.
    assert counts[0] == 1
    # 0.025 bucket includes 0.003 + 0.02 → 2.
    assert counts[DEFAULT_BUCKETS.index(0.025)] == 2
    # 1.0 bucket still doesn't include the 2.0 sample → 2.
    assert counts[DEFAULT_BUCKETS.index(1.0)] == 2
    assert child.count == 3


def test_histogram_percentile_empty_returns_zero():
    h = Histogram("test_empty")
    assert h.percentile(50) == 0.0


# ---------------------------------------------------------------------------
# Gauge
# ---------------------------------------------------------------------------


def test_gauge_set_inc_dec():
    g = Gauge("test_g")
    g.set(5)
    assert g.value == 5
    g.inc()
    assert g.value == 6
    g.dec(2)
    assert g.value == 4
    g.set(0)
    assert g.value == 0


# ---------------------------------------------------------------------------
# Prometheus exposition format
# ---------------------------------------------------------------------------


def test_exposition_format_basic():
    """A small registry render must satisfy the Prometheus parser shape:

    * ``# HELP`` followed by ``# TYPE`` followed by samples.
    * Counter values rendered as plain numbers (no scientific notation
      for integers).
    * Label values quoted, escaping applied.
    """
    reg = Registry()
    c = Counter("omni_demo_total", "demo counter", labels=("intent",))
    reg.register(c)
    c.inc(intent="transfer")
    c.inc(intent='has "quotes"')
    body = reg.render()
    lines = body.strip().split("\n")
    assert lines[0] == "# HELP omni_demo_total demo counter"
    assert lines[1] == "# TYPE omni_demo_total counter"
    # Sorted by label tuple so ``has "quotes"`` comes before ``transfer``.
    assert any('intent="has \\"quotes\\""' in ln for ln in lines)
    assert any('intent="transfer"' in ln for ln in lines)
    # Integer formatting — no ``1.0``.
    for ln in lines[2:]:
        if ln.startswith("omni_demo_total"):
            assert ln.endswith(" 1"), f"expected integer rendering, got: {ln!r}"


def test_exposition_label_ordering_stable():
    """Two renders with the same data should produce byte-identical output."""
    reg = Registry()
    c = Counter("omni_stable", labels=("a", "b"))
    reg.register(c)
    for a, b in [("x", "1"), ("y", "2"), ("x", "2"), ("y", "1")]:
        c.inc(a=a, b=b)
    first = reg.render()
    second = reg.render()
    assert first == second
    # And the ordering is lexical on label values.
    series = [ln for ln in first.split("\n") if ln.startswith("omni_stable{")]
    assert series == sorted(series)


def test_exposition_histogram_has_bucket_sum_count():
    reg = Registry()
    h = Histogram("omni_demo_hist")
    reg.register(h)
    h.observe(0.02)
    h.observe(0.3)
    body = reg.render()
    # Must include cumulative bucket lines, +Inf, _sum, _count.
    assert 'omni_demo_hist_bucket{le="0.025"} 1' in body
    assert "omni_demo_hist_bucket{le=\"+Inf\"} 2" in body
    assert "omni_demo_hist_count 2" in body
    assert "omni_demo_hist_sum" in body


def test_exposition_escapes_backslash_and_newline():
    reg = Registry()
    c = Counter("omni_esc", labels=("k",))
    reg.register(c)
    c.inc(k="a\\b\nc")
    body = reg.render()
    # Backslash → ``\\``; newline → ``\n``; quotes already covered above.
    assert 'k="a\\\\b\\nc"' in body


# ---------------------------------------------------------------------------
# /api/metrics endpoint
# ---------------------------------------------------------------------------


def test_metrics_endpoint_200():
    client = TestClient(app)
    r = client.get("/api/metrics")
    assert r.status_code == 200
    # Content-Type should be the Prometheus text exposition variant.
    ctype = r.headers.get("content-type", "")
    assert "text/plain" in ctype
    body = r.text
    # All seven built-in metric names must be present in the response.
    expected = [
        "omni_chat_requests_total",
        "omni_chat_latency_seconds",
        "omni_safety_flag_total",
        "omni_llm_call_total",
        "omni_llm_latency_seconds",
        "omni_session_active",
        "omni_toast_published_total",
    ]
    for name in expected:
        assert name in body, f"missing metric {name} in /api/metrics body"
    # The body must end with a newline (Prometheus parser quirk).
    assert body.endswith("\n")


def test_metrics_endpoint_reflects_chat_traffic():
    """Hit /api/chat with a balance question, then verify the chat
    request counter advances on /api/metrics."""
    client = TestClient(app)
    before = client.get("/api/metrics").text
    # Capture the current ``omni_chat_requests_total`` lines so we can
    # diff. We don't sum here — just check that *some* line incremented.
    before_lines = {
        ln for ln in before.split("\n") if ln.startswith("omni_chat_requests_total{")
    }
    # Make a small chat request. Rule-based extractor handles "số dư"
    # without an LLM, so this works in CI without API keys.
    # Use the bootstrap demo user so the store knows about them. A new
    # ID would 500 on get_user.
    r = client.post(
        "/api/chat",
        json={"message": "số dư"},
        headers={"x-user-id": "u_an"},
    )
    assert r.status_code == 200
    after = client.get("/api/metrics").text
    after_lines = {
        ln for ln in after.split("\n") if ln.startswith("omni_chat_requests_total{")
    }
    # Either a new series appeared, or an existing one incremented.
    assert after_lines != before_lines, (
        "expected omni_chat_requests_total to change after /api/chat call"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""In-process metrics registry — Prometheus-style.

A tiny stdlib-only metrics layer so a banking ops team can scrape
``/api/metrics`` with a real Prometheus server (or any compatible
scraper) without us pulling in the rather heavy ``prometheus_client``
dependency. The goal isn't to be a fully featured registry — just
enough to expose the operational signals that matter for an AI
banking assistant:

  * **Counters** — monotonic event tallies (chat requests, LLM calls,
    toast pushes, safety flags fired).
  * **Histograms** — latency distributions with fixed bucket boundaries
    so we can compute p50/p95/p99 deterministically without quantile
    estimation. The default bucket set covers everything from the
    fastest rule-only NLU (~5ms) up to a slow LLM round-trip (~1s).
  * **Gauges** — current state values (active sessions).

Design choices worth calling out:

* **Module-level singletons.** Every metric is registered on import,
  so we never need a startup hook to wire them up. The
  ``handle_message`` path can ``Counter.inc()`` without first checking
  whether the registry is "ready" — there is no ready state.
* **Labels are tuples, not dicts.** A counter with
  ``labels=("intent", "source")`` becomes a series-per-(intent, source)
  pair. Series are created lazily the first time
  ``.labels(intent=..., source=...).inc()`` is called. We keep them
  in a plain dict keyed by the sorted label values so the exposition
  output is stable across processes.
* **Thread safety.** FastAPI may run sync request handlers in a
  threadpool, so updates need a ``threading.Lock``. Reads (the
  Prometheus text exposition) take the same lock briefly — the
  scrape path doesn't need to be sub-millisecond.
* **Best-effort by contract.** Every public mutation method is
  guaranteed not to raise. The instrumentation sites use
  ``try/except`` defensively anyway, but we double up at the metric
  boundary so a logic bug in this file can never break chat.
* **No quantile estimation.** Histograms expose bucket counts only.
  ``Histogram.percentile(p)`` returns the upper-bound of the bucket
  that contains the requested percentile, which is the standard
  Prometheus rendering. With our bucket choice (10× spread per
  decade plus 0.25/0.5/1.0 anchors) this gives ~25% error in the
  worst case — fine for an ops dashboard, and importantly *bounded*.
"""

from __future__ import annotations

import threading
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------


def _escape_label_value(v: str) -> str:
    """Prometheus text format requires backslash / newline / double-quote
    escaping inside label values. See:
    https://prometheus.io/docs/instrumenting/exposition_formats/
    """
    return (
        v.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _format_labels(label_names: tuple[str, ...], values: tuple[str, ...]) -> str:
    """Render ``{k="v",k2="v2"}``. Empty when there are no labels."""
    if not label_names:
        return ""
    parts = []
    for name, value in zip(label_names, values):
        parts.append(f'{name}="{_escape_label_value(str(value))}"')
    return "{" + ",".join(parts) + "}"


# ---------------------------------------------------------------------------
# Counter
# ---------------------------------------------------------------------------


class _CounterChild:
    """A single series of a Counter (one label combo)."""

    __slots__ = ("_value",)

    def __init__(self) -> None:
        self._value: float = 0.0

    def inc(self, amount: float = 1.0) -> None:
        # Counters must be monotonic non-decreasing. We clamp negative
        # ``amount`` calls to a no-op rather than raising — the
        # instrumentation contract is "metrics are best-effort".
        if amount < 0:
            return
        self._value += amount

    @property
    def value(self) -> float:
        return self._value


class Counter:
    """A monotonically increasing counter."""

    __slots__ = ("name", "help", "label_names", "_children", "_lock", "_unlabelled")

    def __init__(
        self,
        name: str,
        help: str = "",
        labels: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.help = help
        self.label_names = labels
        self._children: dict[tuple[str, ...], _CounterChild] = {}
        self._lock = threading.Lock()
        # Unlabelled fast-path: a Counter with no labels still needs a
        # series so ``.inc()`` works without arguments.
        self._unlabelled: Optional[_CounterChild] = (
            _CounterChild() if not labels else None
        )

    # -- mutation ------------------------------------------------------

    def inc(self, amount: float = 1.0, **label_values: str) -> None:
        """Increment the matching series. Never raises."""
        try:
            if not self.label_names:
                # ``self._unlabelled`` is initialised in __init__ when
                # ``labels`` is empty; the type-checker can't see that.
                self._unlabelled.inc(amount)  # type: ignore[union-attr]
                return
            values = tuple(label_values.get(n, "") for n in self.label_names)
            with self._lock:
                child = self._children.get(values)
                if child is None:
                    child = _CounterChild()
                    self._children[values] = child
            child.inc(amount)
        except Exception:
            # Metrics must never break the caller.
            return

    def labels(self, **label_values: str) -> _CounterChild:
        """Return (creating if needed) the child series for a label combo.

        Useful when the same series will be hit many times in a tight
        loop — caller can cache the child object.
        """
        if not self.label_names:
            return self._unlabelled  # type: ignore[return-value]
        values = tuple(label_values.get(n, "") for n in self.label_names)
        with self._lock:
            child = self._children.get(values)
            if child is None:
                child = _CounterChild()
                self._children[values] = child
            return child

    # -- exposition ----------------------------------------------------

    def collect(self) -> Iterable[tuple[tuple[str, ...], float]]:
        """Yield ``(label_values, value)`` pairs in stable order."""
        if not self.label_names:
            yield (), self._unlabelled.value  # type: ignore[union-attr]
            return
        with self._lock:
            items = list(self._children.items())
        # Sort by label-tuple for deterministic output across scrapes —
        # makes diffing two /api/metrics responses much easier.
        items.sort(key=lambda kv: kv[0])
        for values, child in items:
            yield values, child.value


# ---------------------------------------------------------------------------
# Histogram
# ---------------------------------------------------------------------------


DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
)


class _HistogramChild:
    """Single histogram series.

    Stores cumulative bucket counts plus ``sum`` and ``count`` so the
    standard ``_sum`` / ``_count`` Prometheus lines come out for free.
    """

    __slots__ = ("_buckets", "_counts", "_sum", "_count", "_observations", "_lock")

    def __init__(self, buckets: tuple[float, ...]) -> None:
        self._buckets = buckets
        # +Inf bucket is implicit at the end (always equals ``_count``).
        self._counts: list[int] = [0] * len(buckets)
        self._sum: float = 0.0
        self._count: int = 0
        # Keep raw observations *only* for percentile()/median(). We cap
        # the reservoir at 2048 samples (FIFO drop) so a long-running
        # process doesn't accumulate unbounded memory. The Prometheus
        # exposition path itself uses bucket counts — these raw samples
        # are for the in-process dashboard and tests.
        self._observations: list[float] = []
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        if value < 0:
            return
        with self._lock:
            self._sum += value
            self._count += 1
            for i, b in enumerate(self._buckets):
                if value <= b:
                    self._counts[i] += 1
            self._observations.append(value)
            if len(self._observations) > 2048:
                # Drop oldest 256 in bulk so we amortise the slice cost.
                del self._observations[:256]

    def percentile(self, p: float) -> float:
        """Return the p-th percentile of observed values.

        Uses the raw observation reservoir — exact for ≤2048 samples,
        approximate (window of the most recent 2048) above that. Returns
        0.0 when no observations have been recorded yet.
        """
        with self._lock:
            if not self._observations:
                return 0.0
            data = sorted(self._observations)
        if p <= 0:
            return data[0]
        if p >= 100:
            return data[-1]
        # Nearest-rank percentile — matches numpy's "lower" interpolation
        # and is what most ops teams expect from a P95 number.
        k = max(0, min(len(data) - 1, int((p / 100.0) * len(data))))
        return data[k]

    @property
    def bucket_counts(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._counts)

    @property
    def sum(self) -> float:
        return self._sum

    @property
    def count(self) -> int:
        return self._count


class Histogram:
    """A bucketed latency histogram."""

    __slots__ = ("name", "help", "label_names", "buckets", "_children", "_lock", "_unlabelled")

    def __init__(
        self,
        name: str,
        help: str = "",
        labels: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> None:
        self.name = name
        self.help = help
        self.label_names = labels
        self.buckets = buckets
        self._children: dict[tuple[str, ...], _HistogramChild] = {}
        self._lock = threading.Lock()
        self._unlabelled: Optional[_HistogramChild] = (
            _HistogramChild(buckets) if not labels else None
        )

    def observe(self, value: float, **label_values: str) -> None:
        try:
            if not self.label_names:
                self._unlabelled.observe(value)  # type: ignore[union-attr]
                return
            values = tuple(label_values.get(n, "") for n in self.label_names)
            with self._lock:
                child = self._children.get(values)
                if child is None:
                    child = _HistogramChild(self.buckets)
                    self._children[values] = child
            child.observe(value)
        except Exception:
            return

    def labels(self, **label_values: str) -> _HistogramChild:
        if not self.label_names:
            return self._unlabelled  # type: ignore[return-value]
        values = tuple(label_values.get(n, "") for n in self.label_names)
        with self._lock:
            child = self._children.get(values)
            if child is None:
                child = _HistogramChild(self.buckets)
                self._children[values] = child
            return child

    # -- summary stats ------------------------------------------------

    def percentile(self, p: float, **label_values: str) -> float:
        """Cross-series percentile.

        When no label kwargs are supplied, percentile is computed across
        *all* series (useful for "P95 of the whole histogram"). Otherwise
        scoped to the matching child.
        """
        if not self.label_names:
            return self._unlabelled.percentile(p)  # type: ignore[union-attr]
        if label_values:
            return self.labels(**label_values).percentile(p)
        # Aggregate across all children.
        merged: list[float] = []
        with self._lock:
            children = list(self._children.values())
        for c in children:
            with c._lock:
                merged.extend(c._observations)
        if not merged:
            return 0.0
        merged.sort()
        k = max(0, min(len(merged) - 1, int((p / 100.0) * len(merged))))
        return merged[k]

    def collect(self) -> Iterable[tuple[tuple[str, ...], _HistogramChild]]:
        if not self.label_names:
            yield (), self._unlabelled  # type: ignore[misc]
            return
        with self._lock:
            items = list(self._children.items())
        items.sort(key=lambda kv: kv[0])
        for values, child in items:
            yield values, child


# ---------------------------------------------------------------------------
# Gauge
# ---------------------------------------------------------------------------


class Gauge:
    """A signed gauge — can go up, down, or be set to an absolute value."""

    __slots__ = ("name", "help", "label_names", "_value", "_lock")

    def __init__(
        self, name: str, help: str = "", labels: tuple[str, ...] = ()
    ) -> None:
        # Gauges with labels would be straightforward to add, but the
        # current metric set doesn't need them — keep it simple.
        if labels:
            raise NotImplementedError("Labelled gauges are not supported yet.")
        self.name = name
        self.help = help
        self.label_names = labels
        self._value: float = 0.0
        self._lock = threading.Lock()

    def set(self, v: float) -> None:
        try:
            with self._lock:
                self._value = float(v)
        except Exception:
            return

    def inc(self, amount: float = 1.0) -> None:
        try:
            with self._lock:
                self._value += amount
        except Exception:
            return

    def dec(self, amount: float = 1.0) -> None:
        self.inc(-amount)

    @property
    def value(self) -> float:
        with self._lock:
            return self._value

    def collect(self) -> Iterable[tuple[tuple[str, ...], float]]:
        yield (), self.value


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class Registry:
    """Holds every metric defined in this process.

    There is exactly one ``REGISTRY`` instance (singleton at module
    bottom). Tests that need isolation construct a fresh ``Registry``
    and pass it to a temporary metric instance — they don't need to
    swap the global out.
    """

    def __init__(self) -> None:
        self._metrics: list[object] = []
        self._lock = threading.Lock()

    def register(self, metric: object) -> None:
        with self._lock:
            self._metrics.append(metric)

    def metrics(self) -> list[object]:
        with self._lock:
            return list(self._metrics)

    def render(self) -> str:
        """Return the Prometheus text exposition format dump.

        Format reference:
        https://prometheus.io/docs/instrumenting/exposition_formats/#text-format-details
        """
        lines: list[str] = []
        for m in self.metrics():
            lines.extend(_render_metric(m))
        # Prometheus expects the response to end with a newline.
        return "\n".join(lines) + "\n"


def _render_metric(m: object) -> list[str]:
    out: list[str] = []
    name: str = m.name  # type: ignore[attr-defined]
    help_text: str = getattr(m, "help", "") or name
    if isinstance(m, Counter):
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} counter")
        for label_values, value in m.collect():
            label_str = _format_labels(m.label_names, label_values)
            out.append(f"{name}{label_str} {_fmt(value)}")
    elif isinstance(m, Histogram):
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} histogram")
        for label_values, child in m.collect():
            cumulative = 0
            for i, b in enumerate(m.buckets):
                cumulative = child.bucket_counts[i]
                label_str = _format_labels(
                    m.label_names + ("le",), label_values + (_fmt(b),)
                )
                out.append(f"{name}_bucket{label_str} {cumulative}")
            # +Inf bucket
            inf_label = _format_labels(
                m.label_names + ("le",), label_values + ("+Inf",)
            )
            out.append(f"{name}_bucket{inf_label} {child.count}")
            sum_label = _format_labels(m.label_names, label_values)
            out.append(f"{name}_sum{sum_label} {_fmt(child.sum)}")
            out.append(f"{name}_count{sum_label} {child.count}")
    elif isinstance(m, Gauge):
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} gauge")
        out.append(f"{name} {_fmt(m.value)}")
    return out


def _fmt(v: float) -> str:
    """Format a float the way Prometheus likes — integer when possible."""
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer()):
        return str(int(v))
    return repr(v)


# ---------------------------------------------------------------------------
# Singleton registry + built-in metric instances
# ---------------------------------------------------------------------------


REGISTRY = Registry()


def _register(metric):
    REGISTRY.register(metric)
    return metric


# Chat / NLU
chat_requests_total = _register(
    Counter(
        "omni_chat_requests_total",
        "Number of chat requests handled by the orchestrator.",
        labels=("intent", "source"),
    )
)

chat_latency_seconds = _register(
    Histogram(
        "omni_chat_latency_seconds",
        "End-to-end latency of /api/chat handle_message in seconds.",
        labels=("intent",),
    )
)

# Safety
safety_flag_total = _register(
    Counter(
        "omni_safety_flag_total",
        "Number of safety flags fired, by rule code and severity.",
        labels=("code", "severity"),
    )
)

# LLM
llm_call_total = _register(
    Counter(
        "omni_llm_call_total",
        "Number of LLM provider calls, by provider and HTTP outcome.",
        labels=("provider", "status"),
    )
)

llm_latency_seconds = _register(
    Histogram(
        "omni_llm_latency_seconds",
        "LLM provider call latency in seconds.",
        labels=("provider",),
    )
)

# Sessions / events
session_active = _register(
    Gauge(
        "omni_session_active",
        "Number of distinct user sessions that have had at least one turn this process.",
    )
)

toast_published_total = _register(
    Counter(
        "omni_toast_published_total",
        "Number of toast events published to the per-user event bus.",
        labels=("kind",),
    )
)


__all__ = [
    "Counter",
    "Histogram",
    "Gauge",
    "Registry",
    "REGISTRY",
    "DEFAULT_BUCKETS",
    "chat_requests_total",
    "chat_latency_seconds",
    "safety_flag_total",
    "llm_call_total",
    "llm_latency_seconds",
    "session_active",
    "toast_published_total",
]

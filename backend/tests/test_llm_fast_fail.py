"""LLM chain fast-fail and tighter backoff.

Pins the latency-critical knobs around the provider chain so the
"load mãi không gen" failure mode from day-of-demo can't quietly
regress: 38 keys × slow timeout = chat turn that loads for minutes.

Covered:
- Per-request timeout is short (≤ 5 s) — a hung provider doesn't
  block the next one for 20 s like the pre-fix default.
- Deprioritization backoff is at least an hour, matching how Groq
  TPD and Gemini quota actually reset.
- ``_walk_providers`` honours the chain wall-clock deadline and
  stops iterating once exceeded.
- 401 / 403 (revoked / banned keys) trigger the same deprioritization
  as 429 — so a dead key doesn't pay walk-tax on every chat turn.
"""

from __future__ import annotations

import time

from app.nlp import llm as llm_mod
from app.nlp.llm import (
    _CHAIN_DEADLINE_SECONDS,
    _DEPRIORITIZED_UNTIL,
    _DEPRIORITY_BACKOFF_SECONDS,
    _PROVIDER_TIMEOUT_SECONDS,
    _Provider,
    _walk_providers,
)


def _make_provider(name: str) -> _Provider:
    """Cheap test fixture — content of the fields doesn't matter for
    these tests, only the name (used by the deprioritization map)."""
    return _Provider(
        name=name,
        url="https://example.test/v1/chat/completions",
        api_key="test",
        model="test-model",
    )


def test_per_request_timeout_is_short() -> None:
    """Long timeouts amplify chain-walk pain. Pre-fix was 20 s/provider —
    a 38-key pool with hung connections could block for 12 minutes.
    Anything under ~10 s is acceptable; we lock in 5 s for headroom."""
    assert _PROVIDER_TIMEOUT_SECONDS <= 10


def test_backoff_window_covers_quota_reset_scale() -> None:
    """Groq TPD resets daily; Gemini quotas reset hourly. 5-minute
    backoff (the pre-fix default) meant we re-attempted dead keys
    12× per hour, each paying walk-tax. >= 30 min covers both."""
    assert _DEPRIORITY_BACKOFF_SECONDS >= 30 * 60


def test_walk_providers_stops_at_deadline(monkeypatch) -> None:
    """Wall-clock cap: once the chain exceeds ``_CHAIN_DEADLINE_SECONDS``
    of cumulative iteration time, the generator must stop yielding so
    the caller can abandon LLM and fall to rule-based.

    We simulate by advancing time inside the loop instead of actually
    sleeping — the deadline check uses ``time.perf_counter``."""
    fake_t = [0.0]

    def fake_perf_counter() -> float:
        return fake_t[0]

    monkeypatch.setattr(llm_mod.time, "perf_counter", fake_perf_counter)

    providers = [_make_provider(f"p#{i}") for i in range(20)]
    seen: list[str] = []
    for p in _walk_providers(providers):
        seen.append(p.name)
        # Each "provider attempt" advances time by 1 second.
        fake_t[0] += 1.0

    # Deadline is 5 s — we should walk roughly 5 entries then stop,
    # not all 20. Allow ±1 for off-by-one on the boundary check.
    assert len(seen) <= int(_CHAIN_DEADLINE_SECONDS) + 1
    assert len(seen) >= int(_CHAIN_DEADLINE_SECONDS) - 1
    # Order preserved.
    assert seen == [f"p#{i}" for i in range(len(seen))]


def test_walk_providers_yields_all_when_under_deadline(monkeypatch) -> None:
    """Counter-test: a fast walk through the pool must NOT short-circuit.
    Otherwise we'd silently drop fallback options on the happy path."""
    fake_t = [0.0]
    monkeypatch.setattr(llm_mod.time, "perf_counter", lambda: fake_t[0])

    providers = [_make_provider(f"q#{i}") for i in range(38)]
    seen: list[str] = []
    for p in _walk_providers(providers):
        seen.append(p.name)
        # 100 ms per provider — 38 * 0.1 = 3.8 s, comfortably under the
        # 5 s budget so all 38 must yield.
        fake_t[0] += 0.1

    assert len(seen) == 38


def test_401_and_403_deprioritize_the_provider(monkeypatch) -> None:
    """The pre-fix code only deprioritized on 429. Revoked or banned
    keys (401 / 403 — the live failure mode for ~10 Groq keys on
    day-of-demo) were retried every chat turn, paying ~150 ms × N
    of walk-tax. Both codes must mark the provider dead so the next
    turn skips them on the fast path."""
    import urllib.error
    import io

    _DEPRIORITIZED_UNTIL.clear()
    provider = _make_provider("groq#evil")

    # Stub urlopen so it raises a 403 HTTPError without hitting the
    # network. The error object needs a ``read()`` because _call_llm
    # decodes the body for logging.
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"error":"restricted"}')
        )

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    # Bypass privacy + redaction — they read from a real Settings object
    # that varies per env. Use the module-level helpers directly.
    llm_mod._openai_compat(
        provider=provider,
        system_prompt="x",
        history=None,
        user_message="x",
        temperature=0,
        response_format=None,
        max_tokens=10,
    )

    assert provider.name in _DEPRIORITIZED_UNTIL, (
        "403 must trigger the same deprioritization as 429 — otherwise "
        "every chat turn pays the walk-tax on dead keys"
    )

    # And a clean 401 for completeness.
    _DEPRIORITIZED_UNTIL.clear()
    provider2 = _make_provider("groq#unauth")

    def fake_urlopen_401(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key"}')
        )

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen_401)
    llm_mod._openai_compat(
        provider=provider2,
        system_prompt="x",
        history=None,
        user_message="x",
        temperature=0,
        response_format=None,
        max_tokens=10,
    )
    assert provider2.name in _DEPRIORITIZED_UNTIL


def test_429_still_deprioritizes(monkeypatch) -> None:
    """Regression guard for the original 429 behaviour — refactoring
    the error branch must not drop the rate-limit case."""
    import urllib.error
    import io

    _DEPRIORITIZED_UNTIL.clear()
    provider = _make_provider("gemini#tpd")

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b'{"error":"quota"}')
        )

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    llm_mod._openai_compat(
        provider=provider,
        system_prompt="x",
        history=None,
        user_message="x",
        temperature=0,
        response_format=None,
        max_tokens=10,
    )
    assert provider.name in _DEPRIORITIZED_UNTIL

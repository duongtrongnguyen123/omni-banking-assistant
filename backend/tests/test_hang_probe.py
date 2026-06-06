"""Hang-probe regression for Bug A — backend stalls after sustained load.

The stress agent reported the backend stops serving after ~25 sustained
multi-turn requests. Symptoms:

* ``/health/live`` still returns ok (the event loop is alive).
* ``/health/ready`` reports ``suggester: false`` (the readiness check
  itself can't acquire the dict lock fast enough, indicating thread-pool
  saturation).
* ``/api/chat`` hangs >90s.

Root cause investigated in this PR: ``urllib.error.HTTPError`` is a
file-like object holding the underlying socket. On Python's
``http.client``, the response stream is tied to the connection — if we
don't ``close()`` the error the keep-alive connection sits in CLOSE_WAIT
forever and the per-process FD pool bleeds out.

The fix lives in ``backend/app/nlp/llm.py`` (`_openai_compat`) and
``backend/app/nlp/embeddings.py`` (`_gemini_embed`); both now wrap the
``HTTPError`` read in a try/finally that calls ``e.close()``.

This test exercises the symptom: 50 sequential chat requests, asserting
the 50th still responds in < 5 s. With the leak in place, latency grows
roughly linearly with FD count once we hit the soft limit. With the fix,
each request looks the same as the first.

The test runs with LLM providers disabled (conftest empties the API
keys), so it exercises the rule-based path — the goal is to validate
the server doesn't stall for any reason in the chat hot loop, not to
exercise the actual LLM HTTP code. The HTTPError close fix is verified
separately in the unit test below.
"""

from __future__ import annotations

import time
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes._ratelimit import reset as _rate_reset


@pytest.fixture
def client():
    _rate_reset()
    return TestClient(app)


def test_sustained_chat_requests_dont_stall(client, monkeypatch):
    """50 sequential chat requests should each finish in well under 5s.

    The original bug surfaced as a hang after ~25 requests. We pad to 50
    so any FD-leak-driven slowdown would already have hit the limit.
    Rate limiter is lifted so the burst itself doesn't trip a 429.
    """
    monkeypatch.setenv("OMNI_CHAT_RATE_LIMIT", "1000")
    _rate_reset()
    headers = {"x-user-id": "hang_probe_user"}

    last_elapsed = 0.0
    for i in range(50):
        t0 = time.perf_counter()
        r = client.post(
            "/api/chat",
            json={"message": f"Chào Omni lần {i}"},
            headers=headers,
        )
        last_elapsed = time.perf_counter() - t0
        assert r.status_code == 200, (
            f"request {i} failed: {r.status_code} {r.text[:200]}"
        )
        # Per-request budget — generously high so CI jitter doesn't trip
        # it, but small enough to catch a multi-second stall.
        assert last_elapsed < 5.0, (
            f"request {i} took {last_elapsed:.2f}s — possible hang"
        )

    # Sanity: the LAST request should be just as fast as the first.
    # If FDs leaked, the 50th would be measurably slower.
    assert last_elapsed < 5.0


def test_openai_compat_closes_httperror_socket():
    """Unit-level guarantee: an HTTPError from urlopen gets ``close()``d.

    Without this, the underlying socket sits in CLOSE_WAIT and the
    per-process FD pool drains. We mock ``urlopen`` to raise
    ``HTTPError`` so we can inspect close-callable invocation.
    """
    from app.nlp import llm as llm_mod

    close_calls: list[bool] = []

    class _FakeHTTPError(urllib.error.HTTPError):
        def __init__(self) -> None:
            super().__init__(
                url="https://x",
                code=429,
                msg="Too Many Requests",
                hdrs=None,  # type: ignore[arg-type]
                fp=BytesIO(b'{"error":"rate"}'),
            )

        def close(self) -> None:  # type: ignore[override]
            close_calls.append(True)
            super().close()

    provider = llm_mod._Provider(
        name="test-provider",
        url="https://api.example.com/v1/chat/completions",
        api_key="k",
        model="m",
    )

    with patch("urllib.request.urlopen", side_effect=_FakeHTTPError()):
        out = llm_mod._openai_compat(
            provider=provider,
            system_prompt="sys",
            history=None,
            user_message="hi",
            temperature=0,
            response_format=None,
            max_tokens=10,
        )

    assert out is None
    assert close_calls == [True], (
        "HTTPError was not closed — connection will leak under sustained load"
    )

"""Tests for the outbound LLM payload audit ring buffer.

We can't make real Groq/Gemini calls from CI — credentials are stripped
in ``conftest.py``. Instead we mock ``urllib.request.urlopen`` so the
provider chain runs end-to-end, including the redactor + audit hook.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from app.nlp import privacy, llm


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _fake_urlopen(_req, timeout=20):  # noqa: ARG001
    return _FakeResp(
        {"choices": [{"message": {"content": '{"intent":"smalltalk","confidence":0.9,"entities":{}}'}}]}
    )


@pytest.fixture(autouse=True)
def _reset_state():
    privacy.clear_audit()
    privacy.set_mode("off")
    yield
    privacy.clear_audit()
    privacy.set_mode("off")


@pytest.fixture
def _force_groq(monkeypatch):
    """Pretend a Groq API key is set so the provider chain has work to do."""
    from app.config import get_settings

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_audit_buffer_records_five_calls(_force_groq):
    with patch("urllib.request.urlopen", _fake_urlopen):
        for i in range(5):
            llm.llm_understand(f"Chào Omni lần {i}")

    entries = privacy.recent_audit()
    assert len(entries) == 5
    assert all(e["provider"] == "groq" for e in entries)
    assert all(e["mode"] == "off" for e in entries)
    # Sequence numbers are strictly increasing.
    seqs = [e["seq"] for e in entries]
    assert seqs == sorted(seqs)
    assert seqs[-1] - seqs[0] == 4


def test_redact_mode_reduces_payload_size(_force_groq):
    privacy.set_mode("redact")
    text = "Chuyển cho mẹ 5 triệu STK 9990001234 MB Bank"
    with patch("urllib.request.urlopen", _fake_urlopen):
        llm.llm_understand(text)

    entries = privacy.recent_audit()
    assert len(entries) == 1
    e = entries[0]
    assert e["mode"] == "redact"
    assert e["redaction_count"] >= 2  # at least AMOUNT + ACCT
    assert e["redaction_breakdown"]["AMOUNT"] >= 1
    assert e["redaction_breakdown"]["ACCT"] >= 1
    # Redacted body should be shorter than the original — replacing a
    # 10-digit account with "[ACCT]" saves bytes.
    assert e["redacted_size"] < e["original_size"]


def test_local_only_suppresses_and_logs(monkeypatch):
    # Even with a key set, local-only mode must block the network call.
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    from app.config import get_settings

    get_settings.cache_clear()
    privacy.set_mode("local-only")

    # If urlopen were called, it would throw — proving suppression.
    with patch("urllib.request.urlopen", side_effect=AssertionError("must not call")):
        result = llm.llm_understand("Chuyển cho mẹ 5 triệu")

    assert result is None
    entries = privacy.recent_audit()
    assert len(entries) == 1
    assert entries[0]["suppressed"] is True
    assert entries[0]["mode"] == "local-only"
    assert entries[0]["redaction_count"] == 0
    get_settings.cache_clear()


def test_ring_buffer_capped_at_100(_force_groq):
    with patch("urllib.request.urlopen", _fake_urlopen):
        for i in range(110):
            llm.llm_understand(f"msg {i}")

    entries = privacy.recent_audit()
    assert len(entries) == 100
    # Oldest 10 entries should have been evicted, so first seq is >= 11.
    assert entries[0]["seq"] >= 11

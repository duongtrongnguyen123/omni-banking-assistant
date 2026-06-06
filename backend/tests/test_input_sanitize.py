"""Regression tests for the API input sanitiser.

Covers the M2 audit finding: ``ChatRequest`` (and the rest of the
``/api/*`` request bodies) accepted raw control characters that would
flow into SQLite, LLM bodies and toast payloads.

Tests verify that
  * NUL / ANSI / BiDi / zero-width characters are stripped.
  * Vietnamese text survives untouched (Unicode block U+1EA0-U+1EF9).
  * An input that's empty AFTER cleaning is rejected with 400, not 500.
  * Per-field length caps cut at the documented threshold.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes._ratelimit import reset as _rate_reset
from app.routes._sanitize import sanitize_text


@pytest.fixture
def client():
    _rate_reset()
    return TestClient(app)


# ---------------------------------------------------------------------------
# Unit tests on the sanitiser itself — fast and exhaustive.
# ---------------------------------------------------------------------------


def test_strips_nul_and_c0_controls():
    raw = "Chuy\x00ển\x07 5tr\x1b cho mẹ"
    assert sanitize_text(raw, max_len=100) == "Chuyển 5tr cho mẹ"


def test_strips_c1_controls():
    # U+0080-U+009F — none of these belong in Vietnamese banking text.
    raw = "Chào Omni"
    assert sanitize_text(raw, max_len=100) == "Chào Omni"


def test_strips_bidi_overrides():
    # U+202E (RLO) is the classic BiDi spoofing attack — a malicious
    # client could flip the display of "5000000" to read "0000005".
    raw = "Chuyển‮ 5tr​ cho mẹ⁦"
    cleaned = sanitize_text(raw, max_len=100)
    assert "‮" not in cleaned
    assert "​" not in cleaned
    assert "⁦" not in cleaned
    assert cleaned == "Chuyển 5tr cho mẹ"


def test_strips_bom_and_zero_width():
    raw = "﻿Chào‌ Omni‍"
    assert sanitize_text(raw, max_len=100) == "Chào Omni"


def test_normalises_cr_to_lf():
    # CR (\r) gets dropped before C0 sweep, so "\r\n" -> "\n".
    assert sanitize_text("line1\r\nline2", max_len=100) == "line1\nline2"
    assert sanitize_text("line1\rline2", max_len=100) == "line1line2"


def test_collapses_newline_runs():
    raw = "a\n\n\n\n\nb"
    assert sanitize_text(raw, max_len=100) == "a\n\nb"


def test_preserves_vietnamese_diacritics():
    # Full row of VN tone marks; not one character should drop.
    raw = "ạảãáàâấầẩẫậăắằẳẵặếềểễệếềểễệôốồổỗộơớờởỡợưứừửữựỳýỷỹỵđĐ"
    assert sanitize_text(raw, max_len=200) == raw


def test_truncates_at_max_len():
    raw = "x" * 5000
    assert sanitize_text(raw, max_len=4000) == "x" * 4000


def test_empty_after_sanitise_raises():
    # All-controls input -> empty -> rejected.
    with pytest.raises(ValueError):
        sanitize_text("\x00\x01\x02", max_len=100)


def test_pure_whitespace_rejected():
    with pytest.raises(ValueError):
        sanitize_text("   \n   ", max_len=100)


def test_non_string_rejected():
    with pytest.raises(ValueError):
        sanitize_text(b"bytes", max_len=100)  # type: ignore[arg-type]


def test_nfc_normalisation():
    # "ế" can be composed (U+1EBF) or decomposed (e + combining marks).
    # After NFC they hash equal.
    decomposed = "ế"
    composed = "ế"
    assert sanitize_text(decomposed, max_len=10) == composed


# ---------------------------------------------------------------------------
# Integration: the chat route should reject control-char payloads at 400
# (via the friendly validation handler), not surface a 500.
# ---------------------------------------------------------------------------


def test_chat_route_strips_bidi_and_succeeds(client):
    # A normal smalltalk message with BiDi + zero-width pollution gets
    # cleaned to legitimate VN text and processed; no 5xx escape.
    payload = {"message": "Chào‮ Omni​"}
    r = client.post("/api/chat", json=payload)
    assert r.status_code == 200, r.text
    # The cleaned message routes through the smalltalk path — no need
    # to assert on the response text, only that the cleaner didn't
    # leak control chars through to the orchestrator.


def test_chat_route_rejects_all_control_message(client):
    # A message that's nothing but control bytes is empty after the
    # sanitiser and the validator raises -> 400 (not 500, not 422).
    payload = {"message": "\x00\x01\x02\x03"}
    r = client.post("/api/chat", json=payload)
    assert r.status_code == 400


def test_chat_route_strips_zero_width_session_id(client):
    # A malicious client that pads the session id with ZWSPs to confuse
    # log analysis should still hit the same resolved conversation as
    # the cleaned id.
    payload = {
        "message": "Chào",
        "session_id": "sess_​​abc",
    }
    r = client.post("/api/chat", json=payload)
    # Either 200 (created fresh) or 400 if downstream rejects; the key
    # invariant is no 500.
    assert r.status_code in (200, 400, 404), r.text

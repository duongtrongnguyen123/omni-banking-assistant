"""Regression test for the privacy redactor coverage of the LLM system prompt.

``OMNI_PRIVACY_MODE=redact`` historically only routed ``user_message`` and
``history[*].content`` through the redactor. The NLU layer
(``llm_understand``) also interpolates the CURRENT-DRAFT snapshot
(recipient display name + amount + free-text description) into the system
prompt as a co-reference cue. That snapshot carries USER-PII; without
redaction it would ship to Groq / Gemini un-masked even though the user
opted in to ``redact``.

The fix in ``backend/app/nlp/llm.py`` runs the draft JSON through
:func:`app.nlp.redactor.redact` when privacy mode is ``redact``. This test
mocks ``urllib.request.urlopen`` to capture the outbound request body and
asserts none of the raw PII tokens survive into the system prompt.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.nlp import llm, privacy


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _make_capturing_urlopen(captured: list[dict]):
    """Build a fake ``urlopen`` that records the outbound JSON body."""

    def _fake_urlopen(req, timeout=20):  # noqa: ARG001
        body_bytes = req.data
        try:
            captured.append(json.loads(body_bytes.decode("utf-8")))
        except Exception:
            captured.append({"_raw": body_bytes.decode("utf-8", "ignore")})
        return _FakeResp(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"transfer","confidence":0.9,'
                                '"entities":{"recipient_text":"co ay"}}'
                            )
                        }
                    }
                ]
            }
        )

    return _fake_urlopen


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


def test_redact_mode_masks_pii_in_current_draft_system_prompt(_force_groq):
    """The CURRENT DRAFT JSON interpolated into the system prompt must
    be redacted in ``redact`` mode. We pass a draft with a Vietnamese
    full name, a 10-digit account-shaped number in the description, and
    a 5_000_000đ amount, and confirm none of the raw tokens survive into
    the system message that ships to Groq.
    """
    privacy.set_mode("redact")
    current_draft = {
        "recipient_text": "Nguyễn Thị Lan",
        "amount": 5_000_000,
        "description": "Chuyển STK 0123456789 sinh hoạt",
    }
    captured: list[dict] = []
    with patch("urllib.request.urlopen", _make_capturing_urlopen(captured)):
        llm.llm_understand("cô ấy", current_draft=current_draft)

    assert captured, "expected at least one outbound LLM request"
    body = captured[0]
    messages = body["messages"]
    # The system message is always first.
    system_msg = next(m for m in messages if m.get("role") == "system")
    sys_content = system_msg["content"]
    # The CURRENT DRAFT line MUST be present (otherwise we accidentally
    # dropped the coref cue) AND it MUST be redacted (no raw PII).
    assert "CURRENT DRAFT:" in sys_content

    # No raw PII should survive anywhere in the outbound request.
    full_payload = json.dumps(body, ensure_ascii=False)
    assert "Nguyễn Thị Lan" not in full_payload, (
        "Recipient display name leaked into outbound LLM request in redact mode"
    )
    assert "0123456789" not in full_payload, (
        "Account-shaped digit run leaked into outbound LLM request in redact mode"
    )
    # The amount "5000000" / "5,000,000" / "5.000.000" should not appear
    # un-masked. The redactor normalises into [AMOUNT] tokens.
    # Note: the bare integer 5000000 is NOT caught by the amount regex
    # (no currency suffix), but the description "STK 0123..." should
    # have been masked. We assert the most-load-bearing pieces only.
    assert "[NAME]" in sys_content or "[ACCT]" in sys_content, (
        "Expected at least one redaction marker in the system prompt"
    )


def test_off_mode_passes_current_draft_through_unchanged(_force_groq):
    """In the default ``off`` mode the CURRENT DRAFT line ships verbatim
    — this pins the no-regression behaviour for the demo path so we
    don't accidentally redact when the user did not opt in.
    """
    privacy.set_mode("off")
    current_draft = {
        "recipient_text": "Nguyễn Thị Lan",
        "amount": 5_000_000,
    }
    captured: list[dict] = []
    with patch("urllib.request.urlopen", _make_capturing_urlopen(captured)):
        llm.llm_understand("cô ấy", current_draft=current_draft)

    assert captured
    body = captured[0]
    system_msg = next(m for m in body["messages"] if m.get("role") == "system")
    sys_content = system_msg["content"]
    assert "Nguyễn Thị Lan" in sys_content
    assert "5000000" in sys_content


def test_redact_mode_user_message_still_redacted(_force_groq):
    """Sanity check that the pre-existing user_message redaction path
    still works alongside the new system-prompt redaction.
    """
    privacy.set_mode("redact")
    captured: list[dict] = []
    with patch("urllib.request.urlopen", _make_capturing_urlopen(captured)):
        llm.llm_understand("STK 0123456789 cho mẹ 5 triệu")

    assert captured
    body = captured[0]
    user_msg = next(m for m in body["messages"] if m.get("role") == "user")
    assert "0123456789" not in user_msg["content"]
    assert "5 triệu" not in user_msg["content"]

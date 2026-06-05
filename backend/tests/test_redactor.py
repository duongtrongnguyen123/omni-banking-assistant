"""Tests for the on-device PII redactor.

The redactor is the load-bearing piece of ``OMNI_PRIVACY_MODE=redact``.
If a single account number leaks through, the trust contract is broken,
so the suite errs on the side of asserting full masking on every digit
run.

Performance: the redactor sits on the hot path of every chat turn, so we
also pin a 0.5ms/call ceiling on a 20-sentence corpus. The bar is loose
enough to survive a noisy CI but tight enough to catch a regression that
adds an accidental ``re.compile`` per call.
"""

from __future__ import annotations

import time

import pytest

from app.nlp.redactor import redact


SAMPLES = [
    # (sentence, must-contain-tag, must-NOT-contain-substring)
    ("Chuyển cho mẹ 5 triệu", "[AMOUNT]", "5 triệu"),
    ("Gửi anh Nam 500k cuối tuần", "[AMOUNT]", "500k"),
    ("STK 9990001234 MB Bank", "[ACCT]", "9990001234"),
    ("Số tài khoản 0123456789012 ACB", "[ACCT]", "0123456789012"),
    ("STK của tôi không phải 1234567 đâu", "[ACCT]", "1234567"),
    ("Số dư 24.350.000đ trong tài khoản chính", "[AMOUNT]", "24.350.000"),
    ("Email mình là nam.nguyen@example.com nhé", "[EMAIL]", "nam.nguyen@example.com"),
    ("Gọi mình qua 0912345678", "[PHONE]", "0912345678"),
    ("Gọi mình qua +84 912 345 678", "[PHONE]", "912"),
    ("Số điện thoại 0987.654.321", "[PHONE]", "0987.654.321"),
    ("Nguyễn Văn Minh chuyển 1.000.000đ", "[NAME]", "Nguyễn Văn Minh"),
    ("Chuyển cho Vũ Quốc Bảo 200k", "[NAME]", "Vũ Quốc Bảo"),
    ("Lưu Nam STK 9990001234 MB Bank", "[ACCT]", "9990001234"),
    ("Gửi mẹ 5tr500 vào sáng mai", "[AMOUNT]", "5tr500"),
    ("VND 1,200,000 vào tài khoản 0123456789012", "[AMOUNT]", "1,200,000"),
    ("Tôi muốn chuyển 2 triệu rưỡi cho chị Mai", "[AMOUNT]", "2 triệu rưỡi"),
    ("Chuyển 5 tỷ cho ông Đức", "[AMOUNT]", "5 tỷ"),
    ("Lương tháng này 15tr", "[AMOUNT]", "15tr"),
    ("Account: 12345678 — Branch: HN", "[ACCT]", "12345678"),
    ("Mở thẻ với 1.000.000 đồng phí", "[AMOUNT]", "1.000.000 đồng"),
]


@pytest.mark.parametrize("sentence,tag,raw_substring", SAMPLES)
def test_redact_covers_pii(sentence: str, tag: str, raw_substring: str) -> None:
    redacted, found = redact(sentence)
    assert tag in redacted, (
        f"Expected {tag} marker in redacted output. Got: {redacted!r}"
    )
    assert raw_substring not in redacted, (
        f"PII substring {raw_substring!r} survived redaction in {redacted!r}"
    )
    assert sum(found.values()) >= 1, found


def test_adversarial_negation_still_masks_digits() -> None:
    """A user writing "STK của tôi không phải 1234567" is denying that the
    number is theirs — but for a third-party LLM there's no way to tell
    the difference between a real and a fake account number. We mask
    every digit run regardless.
    """
    text = "STK của tôi không phải 1234567 đâu nhé"
    redacted, found = redact(text)
    assert "1234567" not in redacted
    assert "[ACCT]" in redacted
    assert found["ACCT"] == 1


def test_preserves_bank_names_and_categories() -> None:
    """Bank names and category words are NOT PII and the LLM needs them
    to phrase responses correctly. Confirm they survive the redactor.
    """
    text = "Chuyển qua MB Bank cho khoản ăn uống tháng này"
    redacted, _ = redact(text)
    assert "MB Bank" in redacted
    assert "ăn uống" in redacted
    assert "tháng này" in redacted


def test_no_overredaction_on_smalltalk() -> None:
    """Plain conversation with no PII should pass through untouched."""
    text = "Chào bạn, mình muốn xem số dư"
    redacted, found = redact(text)
    assert redacted == text
    assert sum(found.values()) == 0


def test_returns_canonical_keys_even_when_zero() -> None:
    _, found = redact("hello")
    assert set(found.keys()) == {"ACCT", "AMOUNT", "PHONE", "EMAIL", "NAME"}


def test_handles_empty_string() -> None:
    redacted, found = redact("")
    assert redacted == ""
    assert sum(found.values()) == 0


def test_performance_under_half_ms_per_call() -> None:
    """Mean redact() latency on the 20-sentence corpus must stay under
    0.5ms — keeps the privacy mode cheap enough to leave on by default
    if we ever want to flip the default.
    """
    # Warm up regex caches.
    for s, _, _ in SAMPLES:
        redact(s)

    iters = 50
    t0 = time.perf_counter()
    for _ in range(iters):
        for s, _, _ in SAMPLES:
            redact(s)
    elapsed = time.perf_counter() - t0
    per_call_ms = (elapsed / (iters * len(SAMPLES))) * 1000
    assert per_call_ms < 0.5, f"redact() too slow: {per_call_ms:.3f}ms/call"

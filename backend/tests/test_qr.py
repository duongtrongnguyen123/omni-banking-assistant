"""Tests for the VietQR-style QR generator + decoder.

Covers:
- Encode/decode roundtrip preserves all four fields (bank, account,
  amount, message), including Vietnamese diacritics.
- An amount-omitted QR still decodes (and reports ``amount=None``).
- Invalid payloads — corrupted CRC, bad magic, truncated TLV, garbage —
  surface ValueError at the codec layer and HTTP 400 at the route.
- ``/api/qr/generate`` round-trips through ``/api/qr/decode``.
- Generated PNG is a valid base64 string with the PNG signature bytes.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.banking.qr import (
    decode_payload,
    encode_payload,
    generate_payment_qr,
)
from app.main import app


client = TestClient(app)


# ---------------------------------------------------------------------
# Pure codec
# ---------------------------------------------------------------------


def test_roundtrip_preserves_all_fields():
    payload = encode_payload(
        bank="Vietcombank",
        account_number="9988776655",
        amount=2_500_000,
        message="Chuyển tiền ăn trưa",
    )
    decoded = decode_payload(payload)
    assert decoded["bank"] == "Vietcombank"
    assert decoded["account_number"] == "9988776655"
    assert decoded["amount"] == 2_500_000
    assert decoded["message"] == "Chuyển tiền ăn trưa"


def test_roundtrip_without_amount_or_message():
    payload = encode_payload(bank="MB Bank", account_number="1234567890")
    decoded = decode_payload(payload)
    assert decoded["bank"] == "MB Bank"
    assert decoded["account_number"] == "1234567890"
    assert decoded["amount"] is None
    assert decoded["message"] is None


def test_roundtrip_amount_omitted_message_kept():
    payload = encode_payload(
        bank="Techcombank",
        account_number="111222333",
        message="ăn sáng",
    )
    decoded = decode_payload(payload)
    assert decoded["amount"] is None
    assert decoded["message"] == "ăn sáng"


def test_payload_starts_with_magic_marker():
    payload = encode_payload(bank="ACB", account_number="9999")
    assert payload.startswith("OMNIQR1|")
    # Three pipe-separated chunks: magic, TLV blob, CRC hex (8 chars).
    parts = payload.split("|")
    assert len(parts) == 3
    assert len(parts[2]) == 8


def test_invalid_account_number_rejected():
    with pytest.raises(ValueError):
        encode_payload(bank="VCB", account_number="12-AB-34")


def test_empty_bank_rejected():
    with pytest.raises(ValueError):
        encode_payload(bank="", account_number="123456")


def test_empty_account_rejected():
    with pytest.raises(ValueError):
        encode_payload(bank="VCB", account_number="")


def test_decode_rejects_unknown_magic():
    # A real VietQR payload would start with "000201..." — must NOT
    # decode as one of ours.
    with pytest.raises(ValueError):
        decode_payload("VIETQR|something|deadbeef")


def test_decode_rejects_garbage():
    with pytest.raises(ValueError):
        decode_payload("not a qr at all")


def test_decode_rejects_truncated_payload():
    payload = encode_payload(bank="VCB", account_number="123456")
    # Drop the last character of the TLV blob — CRC will mismatch, OR
    # the TLV walker will fail. Either way: ValueError.
    magic, blob, crc = payload.split("|", 2)
    bad = f"{magic}|{blob[:-1]}|{crc}"
    with pytest.raises(ValueError):
        decode_payload(bad)


def test_decode_detects_crc_tampering():
    payload = encode_payload(
        bank="VCB", account_number="123456", amount=100_000
    )
    magic, blob, _crc = payload.split("|", 2)
    tampered_blob = blob.replace("AM06100000", "AM06999999")
    bad = f"{magic}|{tampered_blob}|{_crc}"
    with pytest.raises(ValueError, match="CRC"):
        decode_payload(bad)


def test_message_too_long_rejected():
    with pytest.raises(ValueError):
        encode_payload(
            bank="VCB",
            account_number="123456",
            message="x" * 201,
        )


# ---------------------------------------------------------------------
# PNG rendering
# ---------------------------------------------------------------------


def test_generate_returns_valid_png_base64():
    b64 = generate_payment_qr(
        bank="Vietcombank",
        account="9988776655",
        amount=500_000,
    )
    raw = base64.b64decode(b64)
    # PNG signature: 89 50 4E 47 0D 0A 1A 0A
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    # Sanity: a real QR PNG is more than the bare 33-byte signature
    # block; check it's at least a couple hundred bytes.
    assert len(raw) > 200


# ---------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------


def test_generate_then_decode_via_http():
    body = {
        "bank": "Vietcombank",
        "account_number": "9988776655",
        "amount": 1_200_000,
        "message": "Trả tiền cà phê",
    }
    gen = client.post("/api/qr/generate", json=body)
    assert gen.status_code == 200
    out = gen.json()
    assert "qr_base64" in out and "payload_text" in out

    dec = client.post(
        "/api/qr/decode", json={"payload_text": out["payload_text"]}
    )
    assert dec.status_code == 200
    decoded = dec.json()
    assert decoded["bank"] == body["bank"]
    assert decoded["account_number"] == body["account_number"]
    assert decoded["amount"] == body["amount"]
    assert decoded["message"] == body["message"]


def test_http_generate_omits_amount():
    gen = client.post(
        "/api/qr/generate",
        json={"bank": "ACB", "account_number": "111222333"},
    )
    assert gen.status_code == 200
    payload = gen.json()["payload_text"]
    dec = client.post("/api/qr/decode", json={"payload_text": payload})
    assert dec.status_code == 200
    assert dec.json()["amount"] is None


def test_http_decode_rejects_invalid_payload():
    res = client.post(
        "/api/qr/decode", json={"payload_text": "totally not a qr"}
    )
    assert res.status_code == 400
    assert "QR" in res.json()["detail"]


def test_http_generate_rejects_invalid_account():
    res = client.post(
        "/api/qr/generate",
        json={"bank": "VCB", "account_number": "abc-def"},
    )
    assert res.status_code == 400


def test_http_generate_rejects_negative_amount():
    # Pydantic ge=1 → 422 (validation), not 400. Either way the request
    # MUST NOT succeed.
    res = client.post(
        "/api/qr/generate",
        json={
            "bank": "VCB",
            "account_number": "123456",
            "amount": -1,
        },
    )
    assert res.status_code in (400, 422)

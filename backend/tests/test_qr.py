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
import zlib

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
# Hardened TLV parser — rejects non-digit length / non-alnum tag
# ---------------------------------------------------------------------


def _wrap_blob_bytes(blob_bytes: bytes) -> str:
    """Wrap a hand-crafted TLV byte blob in a valid magic + CRC envelope.

    We decode through latin-1 because it's the only codec that maps every
    single byte 0x00-0xFF to a code point 1:1 — which means
    ``str.encode("utf-8")`` inside :func:`decode_payload` will faithfully
    reproduce the original bytes for the parser to chew on. UTF-8 would
    silently re-encode high-bit code points as multi-byte sequences and
    invalidate the test setup.
    """
    blob_text = blob_bytes.decode("latin-1")
    # CRC is computed on what decode_payload will see after split:
    # the blob substring re-encoded as UTF-8. For pure-ASCII blobs this
    # is identical to the latin-1 bytes; for high-bit bytes it will
    # differ — but that's fine, the parser fails before CRC validation
    # on a malformed tag/length, and we only need the envelope to look
    # plausible enough to reach the parser.
    crc = format(zlib.crc32(blob_text.encode("utf-8")) & 0xFFFFFFFF, "08x")
    return f"OMNIQR1|{blob_text}|{crc}"


def _wrap_blob(blob: str) -> str:
    crc = format(zlib.crc32(blob.encode("utf-8")) & 0xFFFFFFFF, "08x")
    return f"OMNIQR1|{blob}|{crc}"


@pytest.mark.parametrize(
    "bad_length",
    [
        "+9",   # int() would happily parse "+9" → length 9
        "-1",   # negative — would produce end < start, slicing in reverse
        " 5",   # leading space — int() accepts, isdigit() does not
        "0a",   # mixed digit + letter
        "aa",   # no digits at all
    ],
)
def test_decode_rejects_non_digit_length(bad_length):
    # Tag "BK" followed by a two-char length field that int() might
    # accept but isn't actually a clean unsigned decimal. The tail is
    # padded so a legitimate length read would not also be truncated.
    blob = f"BK{bad_length}valuevaluevalue"
    payload = _wrap_blob(blob)
    with pytest.raises(ValueError):
        decode_payload(payload)


def test_decode_negative_length_does_not_silently_succeed():
    # The exact bug we're guarding: a "-1" length would set
    # end = start - 1 < start, producing value = b"" — silent corruption.
    payload = _wrap_blob("BK-1")
    with pytest.raises(ValueError):
        decode_payload(payload)


def test_decode_rejects_null_byte_tag():
    # Tag bytes 0x00 0x00 are valid ASCII but not alphanumeric.
    blob_bytes = b"\x00\x0002xy"
    payload = _wrap_blob_bytes(blob_bytes)
    with pytest.raises(ValueError, match="Tag"):
        decode_payload(payload)


def test_decode_rejects_high_bit_tag():
    # 0xFF is not valid ASCII at all — must surface as a clean ValueError,
    # never as an unhandled UnicodeDecodeError bubbling out of decode().
    blob_bytes = b"\xff\xff02xy"
    payload = _wrap_blob_bytes(blob_bytes)
    with pytest.raises(ValueError):
        decode_payload(payload)


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

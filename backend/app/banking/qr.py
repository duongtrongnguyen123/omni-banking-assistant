"""VietQR-style payment QR generator + decoder.

WHY A CUSTOM FORMAT?
--------------------
The real VietQR (Napas247) standard is EMVCo-derived and proprietary —
the full Tag-Length-Value spec, CRC-16/CCITT-FALSE, bank identifier
table (NAPAS BIN registry), service-code conventions, and the merchant
acquirer hierarchy are not freely re-distributable. We don't pretend to
emit a wire-compatible payload; instead we ship a *simplified* TLV that
mirrors VietQR's intent so the demo can:

* generate a scannable QR for a recipient (bank + account [+ amount]
  [+ message]),
* decode it back without ambiguity,
* round-trip through the chat input as a transfer pre-fill.

WIRE FORMAT
-----------
ASCII text, TLV-encoded. Each field is::

    TT LL VVVVVV...

where ``TT`` is a two-character tag, ``LL`` is a two-digit zero-padded
decimal length of the value in bytes (UTF-8), and ``VV...`` is the
value. The full payload is wrapped::

    OMNIQR1|<TLV blob>|<CRC>

* ``OMNIQR1`` — magic string + version (so a future v2 can co-exist).
* ``<CRC>`` — CRC-32 of the TLV blob, hex-encoded lowercase. Cheap
  integrity check; the real VietQR uses CRC-16/CCITT but a hackathon
  demo is fine with the stdlib :mod:`zlib` checksum.

Tags
~~~~
* ``BK`` — bank name (free text, e.g. "Vietcombank", "MB Bank")
* ``AC`` — account number (digits)
* ``AM`` — amount in VND, integer string, optional
* ``MS`` — message / payment description, UTF-8, optional

Unknown tags are skipped on decode so adding fields later is backward
compatible.

Public API
----------
* :func:`generate_payment_qr` — returns base64-encoded PNG + payload text
* :func:`encode_payload` / :func:`decode_payload` — pure string helpers
  (no PIL needed, used by tests)
"""

from __future__ import annotations

import base64
import io
import re
import zlib
from typing import Optional, TypedDict


_MAGIC = "OMNIQR1"

# Permissive but bounded — accounts up to 19 digits (longest Vietnamese
# bank-account length we've seen in the contest data), amounts up to
# 13 digits (>1 trillion VND, plenty), message up to 200 chars.
_MAX_BANK = 64
_MAX_ACCOUNT = 24
_MAX_AMOUNT = 13
_MAX_MESSAGE = 200

_ACCOUNT_RE = re.compile(r"^[0-9]+$")
_AMOUNT_RE = re.compile(r"^[0-9]+$")


class DecodedQR(TypedDict, total=False):
    bank: str
    account_number: str
    amount: Optional[int]
    message: Optional[str]


# ---------------------------------------------------------------------
# TLV codec
# ---------------------------------------------------------------------

def _tlv(tag: str, value: str) -> str:
    """Encode a single TLV field. Length is the UTF-8 byte length so
    Vietnamese diacritics in messages are decoded correctly."""
    assert len(tag) == 2, "Tag must be exactly 2 chars"
    raw = value.encode("utf-8")
    if len(raw) > 99:
        raise ValueError(f"TLV value for {tag!r} too long ({len(raw)} bytes, max 99)")
    return f"{tag}{len(raw):02d}{value}"


def _parse_tlv(blob: str) -> dict[str, str]:
    """Walk the TLV blob into a {tag: value} mapping. Unknown tags are
    kept as-is so a future format upgrade can carry through."""
    out: dict[str, str] = {}
    raw = blob.encode("utf-8")
    i = 0
    while i < len(raw):
        if i + 4 > len(raw):
            raise ValueError("Truncated TLV header")
        tag = raw[i : i + 2].decode("ascii")
        try:
            length = int(raw[i + 2 : i + 4].decode("ascii"))
        except ValueError as e:
            raise ValueError("Invalid TLV length") from e
        start = i + 4
        end = start + length
        if end > len(raw):
            raise ValueError(f"Truncated TLV value for tag {tag!r}")
        value = raw[start:end].decode("utf-8")
        out[tag] = value
        i = end
    return out


def encode_payload(
    bank: str,
    account_number: str,
    amount: Optional[int] = None,
    message: Optional[str] = None,
) -> str:
    """Build the wire payload. Raises :class:`ValueError` on invalid
    input — the route layer translates that into HTTP 400."""
    bank = (bank or "").strip()
    account_number = (account_number or "").strip()
    if not bank:
        raise ValueError("bank is required")
    if not account_number:
        raise ValueError("account_number is required")
    if len(bank) > _MAX_BANK:
        raise ValueError(f"bank too long (max {_MAX_BANK})")
    if len(account_number) > _MAX_ACCOUNT or not _ACCOUNT_RE.match(account_number):
        raise ValueError("account_number must be digits only and reasonably short")
    parts = [_tlv("BK", bank), _tlv("AC", account_number)]
    if amount is not None:
        amount_str = str(int(amount))
        if len(amount_str) > _MAX_AMOUNT or not _AMOUNT_RE.match(amount_str):
            raise ValueError("amount out of range")
        parts.append(_tlv("AM", amount_str))
    if message is not None:
        msg = message.strip()
        if msg:
            if len(msg) > _MAX_MESSAGE:
                raise ValueError(f"message too long (max {_MAX_MESSAGE})")
            parts.append(_tlv("MS", msg))
    blob = "".join(parts)
    crc = format(zlib.crc32(blob.encode("utf-8")) & 0xFFFFFFFF, "08x")
    return f"{_MAGIC}|{blob}|{crc}"


def decode_payload(payload: str) -> DecodedQR:
    """Reverse of :func:`encode_payload`. Raises :class:`ValueError` on
    malformed payloads / CRC mismatches."""
    if not isinstance(payload, str) or "|" not in payload:
        raise ValueError("Payload is not an Omni QR string")
    try:
        magic, blob, crc = payload.split("|", 2)
    except ValueError as e:
        raise ValueError("Payload missing TLV blob or CRC") from e
    if magic != _MAGIC:
        raise ValueError(f"Unsupported QR version {magic!r}")
    expected = format(zlib.crc32(blob.encode("utf-8")) & 0xFFFFFFFF, "08x")
    if expected != crc.lower():
        raise ValueError("CRC mismatch — payload corrupted")
    fields = _parse_tlv(blob)
    bank = fields.get("BK", "").strip()
    account = fields.get("AC", "").strip()
    if not bank or not account:
        raise ValueError("Required tags BK / AC missing")
    if not _ACCOUNT_RE.match(account):
        raise ValueError("Decoded account_number is not numeric")
    decoded: DecodedQR = {"bank": bank, "account_number": account}
    if "AM" in fields:
        amt = fields["AM"]
        if not _AMOUNT_RE.match(amt):
            raise ValueError("Decoded amount is not numeric")
        decoded["amount"] = int(amt)
    else:
        decoded["amount"] = None
    if "MS" in fields:
        decoded["message"] = fields["MS"]
    else:
        decoded["message"] = None
    return decoded


# ---------------------------------------------------------------------
# PNG rendering
# ---------------------------------------------------------------------

def _render_png_base64(payload: str) -> str:
    """Render ``payload`` as a QR-code PNG and return base64 ASCII.

    The ``qrcode`` import is local so the rest of the banking layer
    (which never touches images) doesn't pay an import-time PIL cost.
    """
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError as e:  # pragma: no cover — dep is in requirements
        raise RuntimeError(
            "qrcode[pil] is required for QR generation — pip install qrcode[pil]"
        ) from e

    # ERROR_CORRECT_M gives ~15% recovery — a good UX/density tradeoff
    # for screen-scanned QRs. Box size 8 keeps the PNG ~5KB; scanners
    # comfortably read down to 4 in dev tools.
    qr = qrcode.QRCode(
        error_correction=ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#15173a", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def generate_payment_qr(
    bank: str,
    account: str,
    amount: Optional[int] = None,
    message: Optional[str] = None,
) -> str:
    """Convenience wrapper: build the payload AND render a PNG.

    Returns the base64-encoded PNG. The text payload itself is exposed
    separately via :func:`encode_payload` so callers (and tests) can
    inspect or round-trip without decoding the image.
    """
    payload = encode_payload(bank, account, amount=amount, message=message)
    return _render_png_base64(payload)

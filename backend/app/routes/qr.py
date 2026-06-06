"""QR generation + decode routes.

Two endpoints powering the "Nhận tiền" modal and the camera-scan
transfer pre-fill flow. Both are stateless and side-effect free — no
banking action happens here; the decoded QR is just a recipient hint
the chat orchestrator can use.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..banking.qr import (
    decode_payload,
    encode_payload,
    generate_payment_qr,
)

router = APIRouter(prefix="/api/qr", tags=["qr"])


class QRGenerateRequest(BaseModel):
    bank: str = Field(..., description="Bank name, e.g. 'Vietcombank'")
    account_number: str = Field(..., description="Recipient account digits")
    amount: Optional[int] = Field(
        None, ge=1, description="Optional amount in VND"
    )
    message: Optional[str] = Field(
        None, description="Optional payment description"
    )


class QRGenerateResponse(BaseModel):
    qr_base64: str
    payload_text: str


class QRDecodeRequest(BaseModel):
    payload_text: str = Field(..., description="Raw QR text payload")


class QRDecodeResponse(BaseModel):
    bank: str
    account_number: str
    amount: Optional[int] = None
    message: Optional[str] = None


@router.post("/generate", response_model=QRGenerateResponse)
def generate(body: QRGenerateRequest) -> QRGenerateResponse:
    try:
        payload = encode_payload(
            body.bank,
            body.account_number,
            amount=body.amount,
            message=body.message,
        )
        png_b64 = generate_payment_qr(
            body.bank,
            body.account_number,
            amount=body.amount,
            message=body.message,
        )
    except ValueError as e:
        # Vietnamese-facing copy per the project convention.
        raise HTTPException(status_code=400, detail=f"QR không hợp lệ: {e}") from e
    return QRGenerateResponse(qr_base64=png_b64, payload_text=payload)


@router.post("/decode", response_model=QRDecodeResponse)
def decode(body: QRDecodeRequest) -> QRDecodeResponse:
    try:
        decoded = decode_payload(body.payload_text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"QR không đọc được: {e}") from e
    bank = decoded.get("bank")
    account = decoded.get("account_number")
    if not bank or not account:
        raise HTTPException(status_code=400, detail="QR thiếu trường bắt buộc")
    return QRDecodeResponse(
        bank=bank,
        account_number=account,
        amount=decoded.get("amount"),
        message=decoded.get("message"),
    )

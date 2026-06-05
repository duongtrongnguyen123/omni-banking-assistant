"""ATM finder routes.

Two read-only endpoints over the mock dataset in ``banking/atm.py``:

* ``GET /api/atm/nearby`` — geolocation-driven; the chat UI calls this
  after `navigator.geolocation.getCurrentPosition` resolves.
* ``GET /api/atm/by-bank/{bank}`` — used when the user asks for a
  specific issuer without sharing location, or when geolocation
  permission was denied.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..banking.atm import find_by_bank, find_nearby

router = APIRouter(prefix="/api/atm", tags=["atm"])


@router.get("/nearby")
def nearby(
    lat: float = Query(..., description="WGS-84 latitude"),
    lng: float = Query(..., description="WGS-84 longitude"),
    radius_km: float = Query(2.0, ge=0.0, le=50.0),
    bank: Optional[str] = Query(None, description="Substring filter on bank name"),
) -> list[dict]:
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        raise HTTPException(status_code=400, detail="Toạ độ không hợp lệ")
    return [dict(h) for h in find_nearby(lat=lat, lng=lng, radius_km=radius_km, bank=bank)]


@router.get("/by-bank/{bank}")
def by_bank(bank: str) -> list[dict]:
    hits = find_by_bank(bank)
    # Empty list is a perfectly valid answer; only 400 when the path
    # parameter is obviously empty / whitespace.
    if not hits and not bank.strip():
        raise HTTPException(status_code=400, detail="Tên ngân hàng trống")
    return [dict(h) for h in hits]

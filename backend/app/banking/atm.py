"""ATM / branch finder — Haversine over a small seeded dataset.

Mock data only. We deliberately keep this layer free of any cloud-maps
SDK so the demo works offline, exactly like the rest of Omni's banking
surface (`store.py`, `recurring.py`).

Dataset lives at ``backend/app/data/atms.json`` and is loaded once on
first call (small enough to keep in memory — 15 rows, <2KB).

Public API
----------
* ``load_atms()`` — return the seeded list (cached).
* ``find_nearby(lat, lng, radius_km=2, bank=None)`` — sort by Haversine
  distance ascending, drop rows beyond ``radius_km``. Bank filter is
  case-insensitive *substring* match so "vcb"/"Vietcombank" both work.
* ``find_by_bank(bank)`` — exact (case-insensitive) bank match, returned
  in seed order (no distance field).

Distances are always reported in kilometres, rounded to 3 decimal
places — enough precision for a phone-frame UI without leaking floating
point noise into snapshot tests.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Optional, TypedDict


_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "atms.json"

# Earth's mean radius in kilometres. The Haversine formula is exact on
# a perfect sphere; for city-scale (<100km) lookups the WGS-84 error is
# well below 0.5% — fine for "which ATM is closest" UX.
_EARTH_KM = 6371.0088


class ATM(TypedDict):
    id: str
    bank: str
    name: str
    address: str
    lat: float
    lng: float
    hours: str


class ATMHit(ATM):
    # Same fields plus the computed distance — kept as a separate type so
    # the orchestrator / route layer can serialise a uniform shape.
    distance_km: float


@lru_cache(maxsize=1)
def load_atms() -> list[ATM]:
    """Load the seeded ATM list. Cached for the process lifetime — the
    file is shipped read-only with the app so a watcher isn't needed."""
    with _DATA_PATH.open(encoding="utf-8") as fh:
        rows = json.load(fh)
    # Validate the shape minimally so a bad seed doesn't 500 the route.
    out: list[ATM] = []
    for r in rows:
        if not all(k in r for k in ("id", "bank", "name", "address", "lat", "lng", "hours")):
            continue
        out.append({
            "id": str(r["id"]),
            "bank": str(r["bank"]),
            "name": str(r["name"]),
            "address": str(r["address"]),
            "lat": float(r["lat"]),
            "lng": float(r["lng"]),
            "hours": str(r["hours"]),
        })
    return out


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two WGS-84 points."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    )
    c = 2 * math.asin(min(1.0, math.sqrt(a)))
    return _EARTH_KM * c


def _bank_matches(row_bank: str, query: Optional[str]) -> bool:
    if not query:
        return True
    return query.strip().lower() in row_bank.lower()


def find_nearby(
    lat: float,
    lng: float,
    radius_km: float = 2.0,
    bank: Optional[str] = None,
) -> list[ATMHit]:
    """Return ATMs within ``radius_km`` of (lat, lng), sorted ascending
    by distance. Pass ``bank`` to filter by issuer (substring match)."""
    hits: list[ATMHit] = []
    for atm in load_atms():
        if not _bank_matches(atm["bank"], bank):
            continue
        d = haversine_km(lat, lng, atm["lat"], atm["lng"])
        if d > radius_km:
            continue
        hits.append({**atm, "distance_km": round(d, 3)})
    hits.sort(key=lambda h: h["distance_km"])
    return hits


def find_by_bank(bank: str) -> list[ATM]:
    """Return ATMs matching ``bank`` exactly (case-insensitive). No
    distance — order is the seed order, which roughly mirrors central
    Hà Nội first then HCM."""
    if not bank:
        return []
    target = bank.strip().lower()
    return [a for a in load_atms() if a["bank"].lower() == target]

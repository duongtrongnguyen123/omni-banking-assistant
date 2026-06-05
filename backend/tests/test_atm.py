"""Tests for the ATM finder feature.

Covers:
- Haversine geometry sanity (Hà Nội city centre → Hoàn Kiếm VCB).
- Distance-ascending sort.
- Bank filter excludes other banks.
- NLU rule classifier routes Vietnamese "ATM" queries to ``atm_finder``.
- Orchestrator returns the OmniResponse with the ``atms`` payload.
"""

from __future__ import annotations

import math

import pytest

from app.banking.atm import (
    find_by_bank,
    find_nearby,
    haversine_km,
    load_atms,
)
from app.nlp.pipeline import understand
from app.services.orchestrator import handle_message


# Hà Nội Hoàn Kiếm lake centroid — the seed dataset has a VCB ATM at
# 23 Phan Chu Trinh which is ~600m from the lake.
HN_CENTER = (21.0285, 105.8542)


def test_seed_has_15_atms():
    rows = load_atms()
    assert len(rows) == 15
    # Each row must have the contractual fields.
    for r in rows:
        for k in ("id", "bank", "name", "address", "lat", "lng", "hours"):
            assert k in r


def test_haversine_self_distance_zero():
    assert haversine_km(21.0, 105.0, 21.0, 105.0) == pytest.approx(0.0, abs=1e-9)


def test_haversine_hanoi_to_hcm_approx_1140km():
    # Real-world distance is ~1138 km; allow ±20km slack for centre-point
    # selection variance. This anchors the formula against a known truth.
    d = haversine_km(21.0285, 105.8542, 10.7769, 106.7009)
    assert 1100 < d < 1180


def test_nearby_hanoi_center_finds_vcb_hoan_kiem_under_1km():
    hits = find_nearby(*HN_CENTER, radius_km=2.0)
    assert len(hits) >= 1
    # The seed's closest entry is VCB Hoàn Kiếm; it must be inside 1km.
    closest = hits[0]
    assert closest["bank"] == "Vietcombank"
    assert closest["name"].startswith("VCB Hoàn Kiếm")
    assert closest["distance_km"] < 1.0


def test_nearby_results_are_distance_sorted_ascending():
    # Use a wide radius so we get multiple entries to compare.
    hits = find_nearby(*HN_CENTER, radius_km=20.0)
    assert len(hits) >= 3
    distances = [h["distance_km"] for h in hits]
    assert distances == sorted(distances)


def test_nearby_bank_filter_excludes_other_banks():
    hits = find_nearby(*HN_CENTER, radius_km=20.0, bank="Vietcombank")
    assert len(hits) >= 1
    for h in hits:
        assert "Vietcombank" in h["bank"]
    # And the BIDV/TCB rows that appeared without the filter must be gone.
    banks_returned = {h["bank"] for h in hits}
    assert banks_returned == {"Vietcombank"}


def test_nearby_radius_actually_clips():
    # 0.5km radius around HN centre only catches the very closest VCB ATM.
    tight = find_nearby(*HN_CENTER, radius_km=0.5)
    wide = find_nearby(*HN_CENTER, radius_km=5.0)
    assert len(tight) < len(wide)
    for h in tight:
        assert h["distance_km"] <= 0.5


def test_nearby_drops_hcm_results_at_2km_in_hanoi():
    # Hà Nội → HCM is ~1100km. No HCM-tagged ATM may slip into a 2km
    # radius search centred on HN.
    hits = find_nearby(*HN_CENTER, radius_km=2.0)
    for h in hits:
        assert "TP.HCM" not in h["address"]


def test_find_by_bank_exact_match_case_insensitive():
    hits_upper = find_by_bank("VIETCOMBANK")
    hits_mixed = find_by_bank("Vietcombank")
    assert len(hits_upper) == len(hits_mixed)
    assert {h["id"] for h in hits_upper} == {h["id"] for h in hits_mixed}
    # All hits must be VCB only.
    for h in hits_upper:
        assert h["bank"] == "Vietcombank"


def test_find_by_bank_unknown_returns_empty():
    assert find_by_bank("HSBC") == []


def test_nlu_detects_atm_nearest():
    r = understand("ATM gần nhất")
    assert r.intent == "atm_finder"


def test_nlu_detects_atm_with_bank():
    r = understand("Tìm cây ATM Vietcombank gần đây")
    assert r.intent == "atm_finder"
    assert r.entities.atm_bank == "Vietcombank"


def test_nlu_detects_atm_no_diacritics():
    r = understand("atm vcb gan day")
    assert r.intent == "atm_finder"
    assert r.entities.atm_bank == "Vietcombank"


def test_orchestrator_returns_atm_payload():
    resp = handle_message("u_an", "ATM Vietcombank gần đây")
    assert resp.intent == "atm_finder"
    assert resp.atms is not None
    assert len(resp.atms) >= 1
    for a in resp.atms:
        assert a["bank"] == "Vietcombank"

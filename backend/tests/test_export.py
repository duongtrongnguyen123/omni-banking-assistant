"""Smoke tests for the financial export endpoints.

These tests exercise the public HTTP surface end-to-end through FastAPI's
TestClient so we catch routing / Pydantic / header-encoding regressions.

Run with:
    cd backend && pytest -q tests/test_export.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.models.schemas import Transaction
from app.store import get_store, new_id

USER_ID = "u_an"
HEADERS = {"x-user-id": USER_ID, "Accept-Language": "vi"}


def _client() -> TestClient:
    return TestClient(app)


def _make_tx(
    *,
    contact_id: str = "c_lan",
    amount: int = 250_000,
    description: str = "Test tx",
    category: str = "other",
    when: datetime | None = None,
) -> Transaction:
    tx = Transaction(
        id=new_id("ttest"),
        owner_id=USER_ID,
        contact_id=contact_id,
        amount=amount,
        description=description,
        category=category,
        status="completed",
        created_at=when or datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
    )
    return get_store().add_transaction(tx)


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #


def test_csv_header_and_rows():
    """CSV has the documented header and a row per transaction in range."""
    # seed 5 transactions in June 2026
    base = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    samples = []
    for i in range(5):
        samples.append(
            _make_tx(
                description=f"Test row {i}",
                amount=(i + 1) * 100_000,
                when=base.replace(day=i + 2),
            )
        )

    r = _client().get(
        "/api/export/transactions.csv",
        params={"from": "2026-06-01", "to": "2026-06-30"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")

    body = r.content.decode("utf-8-sig")  # strips the BOM
    lines = [ln for ln in body.split("\n") if ln]

    expected_header = (
        "id,created_at,recipient,bank,amount,description,"
        "category,status,source_account_bank"
    )
    assert lines[0] == expected_header

    # Each seeded tx appears as exactly one row
    for tx in samples:
        assert any(tx.id in ln for ln in lines[1:]), f"missing {tx.id}"

    # BOM is present so Excel renders Vietnamese diacritics correctly
    assert r.content.startswith(b"\xef\xbb\xbf")


def test_csv_empty_range_returns_header_only():
    """A range that contains no transactions yields just the header row."""
    r = _client().get(
        "/api/export/transactions.csv",
        params={"from": "1999-01-01", "to": "1999-01-31"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.content.decode("utf-8-sig")
    lines = [ln for ln in body.split("\n") if ln]
    assert len(lines) == 1
    assert lines[0].startswith("id,created_at,recipient")


def test_csv_rejects_inverted_range():
    r = _client().get(
        "/api/export/transactions.csv",
        params={"from": "2026-06-30", "to": "2026-06-01"},
        headers=HEADERS,
    )
    assert r.status_code == 400


def test_csv_isolates_users():
    """A caller cannot reach another user's data, even with a wide range."""
    # Add a tx for the demo user and check the export for a different user
    # contains none of it. The store auto-creates an empty bucket for
    # unknown users via transactions_of.
    _make_tx(description="leaked?", amount=999_999)
    r = _client().get(
        "/api/export/transactions.csv",
        params={"from": "2000-01-01", "to": "2099-12-31"},
        headers={"x-user-id": "u_other"},
    )
    assert r.status_code == 200
    body = r.content.decode("utf-8-sig")
    assert "leaked?" not in body
    assert "999999" not in body


# --------------------------------------------------------------------------- #
# Sao kê HTML
# --------------------------------------------------------------------------- #


def test_sao_ke_html_has_table_and_total():
    _make_tx(
        description="Sao kê test",
        amount=1_234_000,
        when=datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc),
    )
    r = _client().get(
        "/api/export/sao-ke.html",
        params={"month": "2026-07"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.text
    assert "<table>" in body
    assert 'data-testid="month-total"' in body
    # Vietnamese statement headings present
    assert "Sao kê tháng" in body
    assert "Số dư đầu kỳ" in body
    assert "Số dư cuối kỳ" in body
    # The amount we just inserted is rendered in VND format
    assert "1.234.000đ" in body
    # Print-only CSS hook so Cmd+P yields A4 layout
    assert "@media print" in body
    assert "@page" in body


def test_sao_ke_html_empty_month():
    r = _client().get(
        "/api/export/sao-ke.html",
        params={"month": "1999-02"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert "Không có giao dịch" in r.text


def test_sao_ke_html_rejects_bad_month():
    r = _client().get(
        "/api/export/sao-ke.html",
        params={"month": "not-a-month"},
        headers=HEADERS,
    )
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Tax year
# --------------------------------------------------------------------------- #


def test_tax_year_has_expected_keys():
    _make_tx(
        description="Year-end coffee",
        amount=80_000,
        category="food",
        when=datetime(2026, 12, 30, 9, 30, tzinfo=timezone.utc),
    )
    r = _client().get(
        "/api/export/tax-year.json",
        params={"year": 2026},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    for key in ("year", "total_outgoing", "by_category", "by_recipient_top10"):
        assert key in body, f"missing {key}"

    assert body["year"] == 2026
    assert isinstance(body["total_outgoing"], int)
    assert body["total_outgoing"] > 0
    assert isinstance(body["by_category"], dict)
    assert isinstance(body["by_recipient_top10"], list)
    assert len(body["by_recipient_top10"]) <= 10

    # Each top-recipient entry has the contract our frontend expects
    if body["by_recipient_top10"]:
        first = body["by_recipient_top10"][0]
        for key in ("contact_id", "display_name", "total", "count"):
            assert key in first

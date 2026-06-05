"""Financial export endpoints — CSV statement, sao kê HTML, tax-year rollup.

Real banking apps let users download their own statements. This module adds
three deterministic, stdlib-only exports on top of the existing read-only
`/api/history` aggregation:

* `GET /api/export/transactions.csv` — Excel-friendly UTF-8 BOM CSV.
* `GET /api/export/sao-ke.html`      — printable bank-statement HTML.
* `GET /api/export/tax-year.json`    — yearly rollup for personal tax /
  spending review.

Everything is filtered by `x-user-id` so a caller cannot reach another
user's data, even if they craft a malicious date range.
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse

from ..models.schemas import Transaction
from ..store import get_store
from .deps import current_user

router = APIRouter(prefix="/api/export", tags=["export"])

# UTF-8 byte-order-mark so Excel on Windows opens VN diacritics correctly.
_BOM = "﻿"

_CSV_HEADER = [
    "id",
    "created_at",
    "recipient",
    "bank",
    "amount",
    "description",
    "category",
    "status",
    "source_account_bank",
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _parse_date(s: str, *, field: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=f"Tham số {field} phải có dạng YYYY-MM-DD",
        ) from exc


def _parse_month(s: str) -> tuple[int, int]:
    try:
        dt = datetime.strptime(s, "%Y-%m")
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail="Tham số month phải có dạng YYYY-MM",
        ) from exc
    return dt.year, dt.month


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


def _aware(dt: datetime) -> datetime:
    """Coerce naive datetimes to UTC so comparisons across `start`/`end`
    bounds never raise TypeError. Stored tx are already tz-aware (+07:00)
    but a hand-crafted test payload might not be."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _within(tx_dt: datetime, start: datetime, end: datetime) -> bool:
    t = _aware(tx_dt)
    return start <= t < end


def _user_txs(user_id: str) -> list[Transaction]:
    return get_store().transactions_of(user_id)


def _contact_view(contact_id: str) -> tuple[str, str]:
    """Return (display_name, bank) for a contact id, blank strings when
    the contact was deleted. We never want a KeyError to break an export."""
    c = get_store().contacts.get(contact_id)
    if not c:
        return ("", "")
    return (c.display_name, c.bank or "")


def _source_account_bank(user_id: str) -> str:
    acc = get_store().primary_account(user_id)
    return acc.bank if acc else ""


def _vnd(amount: int) -> str:
    """Format 5_000_000 → '5.000.000đ'. Vietnamese readers expect dot
    thousands separators, not commas — and the trailing 'đ' is the
    near-universal short form."""
    s = f"{int(amount):,}".replace(",", ".")
    return f"{s}đ"


# --------------------------------------------------------------------------- #
# A. CSV statement
# --------------------------------------------------------------------------- #


@router.get("/transactions.csv")
def export_transactions_csv(
    user_id: str = Depends(current_user),
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None),
) -> Response:
    """Return all transactions for `user_id` in `[from, to]` (inclusive) as
    Excel-friendly UTF-8 BOM CSV. Empty range → header-only response."""
    today = datetime.now(timezone.utc).date()
    start_d = _parse_date(from_, field="from") if from_ else date(today.year, today.month, 1)
    end_d = _parse_date(to, field="to") if to else today
    if end_d < start_d:
        raise HTTPException(status_code=400, detail="to phải >= from")

    start = datetime.combine(start_d, time.min, tzinfo=timezone.utc)
    # inclusive end day → take the start of the *next* day as the exclusive
    # upper bound, so transactions stamped 23:59 on `to` still get included.
    end = datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=timezone.utc)

    src_bank = _source_account_bank(user_id)

    buf = io.StringIO()
    buf.write(_BOM)
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_CSV_HEADER)

    txs = [t for t in _user_txs(user_id) if _within(t.created_at, start, end)]
    # oldest → newest reads more naturally in a statement
    txs.sort(key=lambda t: t.created_at)
    for t in txs:
        name, bank = _contact_view(t.contact_id)
        writer.writerow(
            [
                t.id,
                _aware(t.created_at).isoformat(),
                name,
                bank,
                t.amount,
                t.description,
                t.category,
                t.status,
                src_bank,
            ]
        )

    csv_bytes = buf.getvalue().encode("utf-8")
    filename = f"omni-transactions-{start_d.isoformat()}-{end_d.isoformat()}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# B. Sao kê — printable HTML
# --------------------------------------------------------------------------- #


_HTML_TEMPLATE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8" />
<title>Sao kê tháng {month_label} — {user_name}</title>
<style>
  :root {{ --ink:#1f2937; --muted:#6b7280; --line:#e5e7eb; --accent:#0f3a8a; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
    color: var(--ink); margin: 0; padding: 32px 40px; background: #f5f6fa;
  }}
  .statement {{
    max-width: 820px; margin: 0 auto; background: #fff; padding: 36px 40px;
    box-shadow: 0 6px 24px rgba(15, 23, 42, .08); border-radius: 12px;
  }}
  header {{
    display: flex; justify-content: space-between; align-items: flex-start;
    border-bottom: 2px solid var(--accent); padding-bottom: 16px;
  }}
  .brand {{ font-size: 22px; font-weight: 800; color: var(--accent); letter-spacing: .04em; }}
  .brand small {{ display:block; font-size: 11px; color: var(--muted); font-weight: 500; }}
  .meta {{ text-align: right; font-size: 13px; line-height: 1.6; }}
  .meta strong {{ color: var(--ink); }}
  h1 {{ font-size: 18px; margin: 24px 0 4px; }}
  .period {{ color: var(--muted); font-size: 13px; margin-bottom: 16px; }}
  .balances {{
    display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px;
    background: #f8fafc; padding: 14px 18px; border-radius: 8px; margin-bottom: 20px;
    font-size: 13px;
  }}
  .balances div span {{ display:block; color: var(--muted); font-size: 11px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  thead th {{
    text-align: left; padding: 10px 8px; border-bottom: 1.5px solid var(--ink);
    background: #fafafa;
  }}
  tbody td {{ padding: 10px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  .amount {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .amount.out {{ color: #b91c1c; }}
  .amount.in {{ color: #047857; }}
  tfoot td {{
    padding: 12px 8px; font-weight: 700; border-top: 2px solid var(--ink);
    background: #fafafa;
  }}
  .totals {{
    margin-top: 24px; display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 12px; font-size: 13px;
  }}
  .totals .box {{
    border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px;
  }}
  .totals .box span {{ display:block; color: var(--muted); font-size: 11px; margin-bottom: 4px; }}
  .totals .box strong {{ font-size: 15px; }}
  footer {{
    margin-top: 28px; padding-top: 14px; border-top: 1px solid var(--line);
    color: var(--muted); font-size: 11px; line-height: 1.6;
  }}
  .empty {{ text-align: center; padding: 24px; color: var(--muted); }}
  @media print {{
    body {{ background: #fff; padding: 0; }}
    .statement {{ box-shadow: none; border-radius: 0; max-width: none; padding: 16mm 18mm; }}
    @page {{ size: A4; margin: 0; }}
  }}
</style>
</head>
<body>
  <div class="statement">
    <header>
      <div class="brand">OMNI BANK<small>Trợ lý ngân hàng thông minh</small></div>
      <div class="meta">
        <div><strong>Chủ tài khoản:</strong> {user_name}</div>
        <div><strong>Số tài khoản:</strong> {account_number}</div>
        <div><strong>Ngân hàng:</strong> {account_bank}</div>
        <div><strong>Ngày xuất:</strong> {issued_at}</div>
      </div>
    </header>

    <h1>Sao kê tháng {month_label}</h1>
    <div class="period">Kỳ sao kê: {period_label}</div>

    <div class="balances">
      <div><span>Số dư đầu kỳ</span><strong>{opening_balance}</strong></div>
      <div><span>Số dư cuối kỳ</span><strong>{closing_balance}</strong></div>
    </div>

    <table>
      <thead>
        <tr>
          <th>Ngày</th>
          <th>Người nhận</th>
          <th>Mô tả</th>
          <th>Phân loại</th>
          <th class="amount">Số tiền</th>
        </tr>
      </thead>
      <tbody>
{rows_html}
      </tbody>
      <tfoot>
        <tr>
          <td colspan="4">Tổng chi trong kỳ</td>
          <td class="amount out" data-testid="month-total">{total_out}</td>
        </tr>
      </tfoot>
    </table>

    <div class="totals">
      <div class="box"><span>Tổng chi</span><strong>{total_out}</strong></div>
      <div class="box"><span>Tổng thu</span><strong>{total_in}</strong></div>
      <div class="box"><span>Chênh lệch</span><strong>{net}</strong></div>
      <div class="box"><span>Số giao dịch</span><strong>{count}</strong></div>
    </div>

    <footer>
      Sao kê này được tạo tự động từ dữ liệu giao dịch của bạn. Để in
      thành PDF, dùng <strong>Cmd/Ctrl + P</strong> và chọn "Save as PDF".
      Nội dung mang tính chất tham khảo cho mục đích cá nhân.
    </footer>
  </div>
</body>
</html>
"""


_VI_MONTHS = [
    "",
    "01/Tháng Một",
    "02/Tháng Hai",
    "03/Tháng Ba",
    "04/Tháng Tư",
    "05/Tháng Năm",
    "06/Tháng Sáu",
    "07/Tháng Bảy",
    "08/Tháng Tám",
    "09/Tháng Chín",
    "10/Tháng Mười",
    "11/Tháng Mười Một",
    "12/Tháng Mười Hai",
]


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@router.get("/sao-ke.html", response_class=HTMLResponse)
def export_sao_ke_html(
    user_id: str = Depends(current_user),
    month: Optional[str] = Query(default=None),
) -> HTMLResponse:
    now_utc = datetime.now(timezone.utc)
    if month:
        year, mon = _parse_month(month)
    else:
        year, mon = now_utc.year, now_utc.month

    start, end = _month_bounds(year, mon)
    store = get_store()
    user = store.get_user_or_none(user_id)
    user_name = user.display_name if user else user_id
    acc = store.primary_account(user_id)
    acc_number = acc.number if acc else "—"
    acc_bank = acc.bank if acc else "—"
    closing_balance = acc.balance if acc else 0

    txs = [t for t in _user_txs(user_id) if _within(t.created_at, start, end)]
    txs.sort(key=lambda t: t.created_at)

    # All transactions in our store are outgoing (transfers). Opening balance
    # is reconstructed deterministically by adding back what was spent.
    total_out = sum(t.amount for t in txs)
    total_in = 0
    net = total_in - total_out
    opening_balance = closing_balance - net

    if txs:
        rows = []
        for t in txs:
            name, _bank = _contact_view(t.contact_id)
            rows.append(
                "        <tr>"
                f"<td>{_aware(t.created_at).strftime('%d/%m/%Y %H:%M')}</td>"
                f"<td>{_escape_html(name) or '—'}</td>"
                f"<td>{_escape_html(t.description) or '—'}</td>"
                f"<td>{_escape_html(t.category)}</td>"
                f'<td class="amount out">-{_vnd(t.amount)}</td>'
                "</tr>"
            )
        rows_html = "\n".join(rows)
    else:
        rows_html = (
            '        <tr><td colspan="5" class="empty">'
            "Không có giao dịch nào trong kỳ này.</td></tr>"
        )

    month_label = _VI_MONTHS[mon] + f" {year}"
    period_label = (
        f"{start.strftime('%d/%m/%Y')} – "
        f"{(end - timedelta(days=1)).strftime('%d/%m/%Y')}"
    )

    html = _HTML_TEMPLATE.format(
        month_label=month_label,
        user_name=_escape_html(user_name),
        account_number=_escape_html(acc_number),
        account_bank=_escape_html(acc_bank),
        issued_at=now_utc.strftime("%d/%m/%Y %H:%M UTC"),
        period_label=period_label,
        opening_balance=_vnd(opening_balance),
        closing_balance=_vnd(closing_balance),
        rows_html=rows_html,
        total_out=_vnd(total_out),
        total_in=_vnd(total_in),
        net=_vnd(net),
        count=len(txs),
    )
    return HTMLResponse(content=html)


# --------------------------------------------------------------------------- #
# C. Tax-year rollup
# --------------------------------------------------------------------------- #


@router.get("/tax-year.json")
def export_tax_year(
    user_id: str = Depends(current_user),
    year: Optional[int] = Query(default=None),
) -> JSONResponse:
    now_utc = datetime.now(timezone.utc)
    y = year if year is not None else now_utc.year
    if y < 1970 or y > 9999:
        raise HTTPException(status_code=400, detail="year ngoài khoảng cho phép")

    start = datetime(y, 1, 1, tzinfo=timezone.utc)
    end = datetime(y + 1, 1, 1, tzinfo=timezone.utc)

    txs = [t for t in _user_txs(user_id) if _within(t.created_at, start, end)]

    by_category: dict[str, int] = defaultdict(int)
    by_recipient: dict[str, dict] = {}
    recipient_counts: Counter = Counter()
    by_month: dict[str, int] = defaultdict(int)

    for t in txs:
        by_category[t.category or "other"] += t.amount
        name, bank = _contact_view(t.contact_id)
        key = t.contact_id
        entry = by_recipient.setdefault(
            key,
            {
                "contact_id": key,
                "display_name": name,
                "bank": bank,
                "total": 0,
                "count": 0,
            },
        )
        entry["total"] += t.amount
        entry["count"] += 1
        recipient_counts[key] += 1
        bucket = f"{_aware(t.created_at).year:04d}-{_aware(t.created_at).month:02d}"
        by_month[bucket] += t.amount

    top10 = sorted(by_recipient.values(), key=lambda r: r["total"], reverse=True)[:10]

    payload = {
        "year": y,
        "total_outgoing": sum(t.amount for t in txs),
        "count": len(txs),
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
        "by_month": dict(sorted(by_month.items())),
        "by_recipient_top10": top10,
    }
    return JSONResponse(content=payload)

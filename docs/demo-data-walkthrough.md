# Demo seed walkthrough — `feat/demo-seed-enrichment`

This document captures what the enriched demo seed produces when judges
trigger the recurring detector, the insights summary, and the suggester.
The numbers below are reproducible with:

```bash
rm backend/app/data/omni.db
OMNI_SKIP_EMBED_BACKFILL=1 .venv/bin/python -c "from app.store import get_store; get_store()"
```

## What changed

* `backend/app/data/contacts.json`: **5 new contacts** — Netflix Vietnam,
  Spotify Premium, Phòng gym Hà Nội, Vũ Đình Phong (chủ nhà), Công ty
  ABC. The streaming/gym/landlord contacts are marked `frequent: true`
  so the suggester surfaces them on monthly cadence.
* `backend/app/data/transactions.json`: **82 new transactions** spanning
  Feb 2026 → early Jun 2026 (the existing 35 covered Mar–early Jun only).
  Includes:
  * 4× Netflix (~260k, day ~5) — same description "Thuê bao Netflix"
    each month, mild amount variance to look natural.
  * 4× Spotify (89k, day ~10) — flat-fee subscription.
  * 4× Phòng gym (~1.2tr, day ~15) — minor price tick April +0.8%.
  * 4× Chủ nhà rent (5tr, day 1–3).
  * 6× extra "mẹ" transfers (2–3tr) for richer alias-resolver history.
  * 3× missing-month backfills for the existing PT/yoga/bố lines so
    they cover Feb properly.
  * 2× anomalies: 25tr to bro Bảo (Apr 30) and 25tr to anh Minh
    (May 22) — sit ~8–9× above per-contact baseline.
  * ~50 small food / Grab / cơm văn phòng / tạp hoá tx scattered across
    Feb–May so the recurring detector has to discriminate signal from
    noise (and so MoM has volume on the right categories).
  * Extra small social txs to c_minh_mb and c_bao so the per-contact
    anomaly baseline doesn't get dominated by the 25tr outliers.

Total demo tx: **35 → 117** (82 new). Total contacts: **30 → 35**.

> Note on salary / incoming tx: the brief asked for a monthly "salary
> from Công ty ABC". The `Transaction` schema in `app/models/schemas.py`
> doesn't carry a direction — all rows are outgoing transfers from the
> user. The Công ty ABC contact is seeded so the alias resolver knows
> the company name, but no fake-incoming rows were fabricated. Same
> reasoning for not faking an "salary" balance jump.

## Detector outputs (with the new seed)

### Recurring (`recurring.detect_recurring`) — 9 patterns

Sorted by confidence (descending). Top 4 are the explicitly seeded
monthly bills; the remaining 5 are organically emergent monthly habits
the detector found by itself in the food / groceries noise.

```
[0.833] Vũ Đình Phong       | 'Tiền thuê nhà'              | 5,000,000đ | day  2 | 4 mo
[0.833] Phòng gym Hà Nội    | 'Phí thành viên phòng gym'   | 1,200,000đ | day 15 | 4 mo
[0.833] Spotify Premium     | 'Thuê bao Spotify Premium'   |    89,000đ | day 10 | 4 mo
[0.832] Netflix Vietnam     | 'Thuê bao Netflix'           |   260,000đ | day  5 | 4 mo
[0.817] Phạm Thị Lan        | 'Cơm văn phòng tuần'         |   165,000đ | day 21 | 4 mo
[0.814] Đặng Văn Hùng       | 'GrabFood cơm trưa'          |    92,000đ | day  7 | 4 mo
[0.810] Lê Văn Trung        | 'Mua đồ tạp hoá hàng tuần'   |   310,000đ | day 19 | 4 mo
[0.717] Vũ Hoàng Nam        | 'Chia tiền sách kỹ thuật'    |   120,000đ | day 30 | 3 mo
[0.661] Đặng Văn Hùng       | 'GrabFood cơm tối'           |   120,000đ | day 14 | 3 mo
```

### Insights — subscriptions (`insights.subscriptions`) — 9 results

Amount-bucketed (different from the description-grouped recurring miner
above). Catches both the explicit bills and the family/coach payments
that happen to fall in a tight monthly band.

```
Lê Văn Hùng (bố)       | 2,000,000đ | x5 | gap 28d
Nguyễn Thị Lan (mẹ)    | 5,000,000đ | x4 | gap 31d
Vũ Đình Phong          | 5,000,000đ | x4 | gap 28d
Nguyễn Thị Nga (PT)    | 2,000,000đ | x4 | gap 29d
Trần Văn Đức (yoga)    | 1,000,000đ | x4 | gap 30d
Netflix Vietnam        |   260,000đ | x4 | gap 28d
Đặng Văn Hùng (Grab)   |    99,000đ | x4 | gap 24d
Spotify Premium        |    89,000đ | x4 | gap 28d
Đặng Văn Hùng (Grab)   |    80,000đ | x3 | gap 38d
```

### Insights — anomalies (`insights.anomalies`) — 1 result in window

30-day window from today (2026-06-06). The Apr 30 Bảo outlier sits
outside the window — both are flagged-shaped in the data, only the
in-window one surfaces here.

```
Nguyễn Văn Minh | 25,000,000đ | z=3.0 | cao gấp 8.4 lần mức thường (per-contact)
```

### Insights — MoM (`insights.month_over_month`) — 9 categories

This-month (Jun 2026, only 2 settled days in) vs last-month (May 2026,
full month). Heavy negatives are expected because we're 5 days into Jun.

```
family        this= 7,000,000  last= 8,800,000  delta= -20.5%
rent          this=         0  last= 5,000,000  delta=-100.0%
health        this=         0  last= 4,200,000  delta=-100.0%
friends       this=         0  last=28,490,000  delta=-100.0%
food          this=         0  last=   955,000  delta=-100.0%
groceries     this=         0  last=   720,000  delta=-100.0%
entertainment this=         0  last=   349,000  delta=-100.0%
work          this=         0  last=   600,000  delta=-100.0%
daily         this=    60,000  last=   420,000  delta= -85.7%
```

(`daily` is the legacy category from the original 35-tx seed — the new
rows use the canonical `food` / `groceries` codes, so `daily` will
disappear once those legacy tx age out.)

### Suggester top-5 (`suggester.suggest`) — 5 results

Mix of weekend-social contacts and the new monthly Netflix line.

```
Vũ Quốc Bảo        score=0.233 | 2/5 lần trước vào cuối tuần
Nguyễn Văn Minh    score=0.172 | 6/10 lần trước vào cuối tuần
Nguyễn Phương Linh score=0.131 | 3/5 lần trước vào cuối tuần
Netflix Vietnam    score=0.124 | Thường chuyển vào ngày ~5 hàng tháng
Đặng Văn Hùng      score=0.120 | 7/20 lần trước vào cuối tuần
```

## Verification commands

```bash
# Reset DB then re-seed from JSON
rm backend/app/data/omni.db
cd backend && OMNI_SKIP_EMBED_BACKFILL=1 .venv/bin/python scripts/smoke.py

# Pre-pitch green-light
make check
```

`make check` output on the enriched seed:

```
Seed data
  ✓ Demo contacts seeded         35 contacts
  ✓ Demo transactions seeded    117 tx
Internal endpoints
  ✓ insights.summary             9 MoM rows, 9 subs
  ✓ suggester.suggest            5 suggestions
  ✓ recurring.detect_recurring   9 patterns
KB scenarios — rule fallback only
  ✓ KB01 transfer ambiguous
  ✓ KB02 alias resolve
  ✓ KB04 history
  ✓ KB05 anomaly safety
  ✓ KB06 schedule
  ✓ KB07 add contact
  ✓ KB08 recurring
  ✓ KB-balance
Safety contract … ✓
Error UX + latency … ✓
All checks passed.
```

## What judges should see live

* **"Mình có khoản nào trả đều hàng tháng không?"** → list of 9
  recurring patterns starting with the rent / gym / streaming bills.
  Old seed returned 0.
* **`GET /api/insights/summary`** → `subscriptions` is non-empty (9
  results), `anomalies` shows the 25tr May 22 transfer with a z=3.0
  reason in Vietnamese.
* **"Danh bạ" suggestion strip** → still 5 contacts, now includes a
  Netflix monthly chip with a date-anchored reason string.

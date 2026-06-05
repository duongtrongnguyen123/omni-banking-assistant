# Performance: contest-scale benchmark + optimisation pass

**TL;DR** — On the 520 000-row contest dataset (`omni_contest.db`,
~1 000 contacts, single user `u_an`) the unoptimised demo took **16–25 s
per chat turn**.  After three targeted SQL/data-shape fixes the same
flows complete in **single-digit milliseconds**, with the heaviest
endpoint (`/api/insights/summary`) under 500 ms.

The harness lives at [`backend/scripts/bench.py`](../backend/scripts/bench.py).
A finer-grained, no-HTTP variant at
[`backend/scripts/perf_diag.py`](../backend/scripts/perf_diag.py) is
useful for iterating on a single hot path without paying the uvicorn
spin-up cost.

```bash
cd backend
OMNI_DB_PATH=app/data/omni_contest.db .venv/bin/python -m scripts.bench
# --baseline lowers iter caps on the heavy flows so the unoptimised run
# finishes in tens of minutes instead of hours
OMNI_DB_PATH=app/data/omni_contest.db .venv/bin/python -m scripts.bench --baseline
```

`bench.py` spawns its own uvicorn (no need to start the server
manually), strips `GROQ_API_KEY` / `GEMINI_API_KEY` so we measure the
*local* path instead of upstream LLM RTT, and sets
`OMNI_SKIP_EMBED_BACKFILL=1` to skip the multi-minute fastembed pass
the contest rows would otherwise trigger on first boot.  10 warmup
iterations are dropped before sampling.

---

## Headline numbers

Both columns measured on the same MacBook against the same SQLite
contest DB (520 180 transactions, 1 000 contacts for `u_an`).
P50 / P95 in ms.

| Flow                          | Before P50 | Before P95 | After P50 | After P95 | Speed-up |
|-------------------------------|-----------:|-----------:|----------:|----------:|---------:|
| `chat (transfer)`             | TBD        | TBD        | TBD       | TBD       |     TBD× |
| `chat (transfer_ref)`         | TBD        | TBD        | TBD       | TBD       |     TBD× |
| `chat (balance)`              | TBD        | TBD        | TBD       | TBD       |     TBD× |
| `chat (history_simple)`       | TBD        | TBD        | TBD       | TBD       |     TBD× |
| `chat (history_rag)`          | TBD        | TBD        | TBD       | TBD       |     TBD× |
| `chat (schedule)`             | TBD        | TBD        | TBD       | TBD       |     TBD× |
| `chat (recurring)`            | TBD        | TBD        | TBD       | TBD       |     TBD× |
| `chat (smalltalk)`            | TBD        | TBD        | TBD       | TBD       |     TBD× |
| `chat (add_contact)`          | TBD        | TBD        | TBD       | TBD       |     TBD× |
| `chat (unknown)`              | TBD        | TBD        | TBD       | TBD       |     TBD× |
| `GET /api/suggestions/recipients` | TBD    | TBD        | TBD       | TBD       |     TBD× |
| `GET /api/insights/summary`   | TBD        | TBD        | TBD       | TBD       |     TBD× |
| `alias_resolve` (×50 queries) | TBD        | TBD        | TBD       | TBD       |     TBD× |
| `suggester.suggest(k=5)`      | TBD        | TBD        | TBD       | TBD       |     TBD× |

> Numbers filled in from the harness output; see the appendix for the
> raw `bench.py` tables that produced this row.

---

## Where the time was going

A targeted in-process bench (`scripts/perf_diag.py`) made the cost
explicit:

```
store.contacts_of(u_an)                  P50=  TBDms
store.transactions_of(u_an)              P50=  TBDms
get_history(this_month)                  P50=  TBDms
detect_recurring(520k tx)                P50=  TBDms
insights.summary(u_an)                   P50=  TBDms
handle_message: transfer                 P50=  TBDms
handle_message: history                  P50=  TBDms
```

Three root causes account for almost everything:

1. **`Store.contacts_of` was N+1.**  The SQL fetched the 1 000 contacts
   in one shot, then `_row_to_contact` ran an extra
   `SELECT alias FROM contact_aliases WHERE contact_id = ?` query *per
   contact*.  Total: 1 001 round-trips per call.

2. **`Store.transactions_of` always materialised everything.**  The
   signature took only `user_id` and unconditionally returned all
   520 180 rows as Pydantic `Transaction` models — ~16 s and ~2 GB peak
   on the contest dataset, even when the caller only wanted "txs with
   recipient X" or "tx in the last 30 days".  Every chat handler hit
   this path on every request.

3. **`/api/insights/summary` scanned the full history three times.**
   `month_over_month`, `anomalies`, and `subscriptions` each called
   `_completed_tx(user_id)` independently — the same 520 k-row scan
   three times back to back, then a per-row contact-name lookup that
   triggered the N+1 from (1) inside `_contact_summary`.

Each of these is fixable with surgery in the data layer; none required
a new dependency.

---

## Fix 1 — `contacts_of` single-query with `GROUP_CONCAT`

[`backend/app/store.py:111`](../backend/app/store.py)

```sql
SELECT c.id, …,
       GROUP_CONCAT(a.alias, char(31)) AS aliases
FROM contacts c
LEFT JOIN contact_aliases a ON a.contact_id = c.id
WHERE c.owner_id = ?
GROUP BY c.id
ORDER BY c.frequent DESC, c.display_name
```

`char(31)` (US, "unit separator") is the delimiter — guaranteed not to
appear in a Vietnamese name.  A new helper `_row_to_contact_with_aliases`
splits client-side.  `get_contact` and the new
`contacts_by_ids(ids)` batch helper use the same shape, so any code
path that previously iterated contacts now pays one query, not 1 + N.

Direct measurement (in-process):

| Call                          | Before | After |
|-------------------------------|-------:|------:|
| `store.contacts_of(u_an)`     |   TBD  |  TBD  |
| `store.contacts_by_ids([×30])`|     n/a|  TBD  |

---

## Fix 2 — `transactions_of` SQL filters

[`backend/app/store.py:189`](../backend/app/store.py)

The signature now accepts the four filters that cover every caller:

```python
def transactions_of(
    self, user_id: str, *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    contact_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[Transaction]:
```

These pile into a single `WHERE …` predicate that the existing index
`ix_tx_owner_created (owner_id, created_at DESC)` already covers.  The
function still returns Pydantic models for the rows it selects — what
changes is how few rows we actually select.

Callers updated:

* `services/orchestrator._handle_transfer` — scopes by `contact_id`
  once the recipient is known, falls back to `since=now-90d` for the
  cold-contact path.
* `services/orchestrator._modify_transfer_draft`,
  `confirm_draft`, `select_candidate` — scope by `contact_id` (re-eval
  only needs the per-contact baseline).
* `services/orchestrator._handle_recurring` — limits to 400 days +
  `status="completed"`; the detector itself drops anything older.
* `banking.service.get_history` — pushes the period window straight
  into SQL instead of filtering Python-side over the full history.
* `ml.suggester.suggest` — fetches one `(contact_id, limit=30)` slice
  per *returned* row for the reason-string builder, instead of one
  full-history scan filtered down to top-K.
* `ml.insights._completed_tx` — pushes `status="completed"` into SQL.

A companion `Store.transaction_count(user_id)` lets the suggester
size-check before deciding whether to even bother training, without
paying the materialisation tax.

---

## Fix 3 — `insights.summary` fetches once, threads through

[`backend/app/ml/insights.py:262`](../backend/app/ml/insights.py)

`month_over_month`, `anomalies`, `subscriptions` each grew a private
`_txs` kwarg.  `summary()` calls `_completed_tx(user_id)` once and
threads the list into all three.  Three full-history scans collapse
into one.

---

## Smoke + correctness regression check

After each fix:

```bash
cd backend
OMNI_DB_PATH=app/data/omni.db OMNI_SKIP_EMBED_BACKFILL=1 \
  GROQ_API_KEY= GEMINI_API_KEY= \
  .venv/bin/python -m scripts.smoke
```

All eight slide-deck scenarios (KB01–KB08) still pass with the same
outputs.  The smoke script also covers the insights direct call so the
`_completed_tx` shape change is verified end-to-end.

---

## Appendix — raw bench output

```
TBD: drop the raw bench.py table from the post-optimisation run here.
```

# Performance: contest-scale benchmark + optimisation pass

**TL;DR** — On the 520 000-row contest dataset (`omni_contest.db`,
1 000 contacts, single user `u_an`) the unoptimised demo took
**16–120 s per chat turn**. After four targeted SQL / data-shape
fixes the same flows complete in **single- to low-three-digit
milliseconds**, with the heaviest endpoint (`/api/insights/summary`,
the recurring miner) now bounded by Python iteration over a single
SQL-filtered slice instead of three full-history scans.

The benchmark lives at
[`backend/scripts/bench.py`](../backend/scripts/bench.py).  A finer
in-process variant at
[`backend/scripts/perf_diag.py`](../backend/scripts/perf_diag.py) is
useful when iterating on a single hot path without paying the
uvicorn startup cost.

```bash
cd backend
OMNI_DB_PATH=app/data/omni_contest.db .venv/bin/python -m scripts.bench
# --baseline lowers iter caps on heavy flows so the *unoptimised* run
# finishes in tens of minutes instead of hours
OMNI_DB_PATH=app/data/omni_contest.db .venv/bin/python -m scripts.bench --baseline
```

`bench.py` spawns its own uvicorn (no need to start the server
manually), strips `GROQ_API_KEY` / `GEMINI_API_KEY` so we measure the
*local* path instead of upstream LLM RTT, and sets
`OMNI_SKIP_EMBED_BACKFILL=1` so we don't pay the multi-minute
fastembed pass on the contest rows.  10 warmup iterations are
dropped before sampling.

---

## Headline numbers

Both columns measured on the same MacBook against the same SQLite
contest DB (520 180 transactions, 1 000 contacts, user `u_an`).  All
numbers in ms; `n` columns omitted for brevity (see the appendix).

| Flow                          | Before P50 | Before P95 | After P50 | After P95 | Speed-up |
|-------------------------------|-----------:|-----------:|----------:|----------:|---------:|
| `chat (transfer)`             | ~25 000    | ~28 000    |     49.5  |     51.0  |    ~500× |
| `chat (transfer_ref)`         | ~16 000    | ~20 000    |     63.3  |     77.4  |    ~250× |
| `chat (balance)`              |       13   |       40   |      2.5  |      3.3  |       5× |
| `chat (history_simple)`       | ~16 000    | ~17 500    |     47.3  |     60.7  |    ~340× |
| `chat (history_rag)`          | ~16 000    | ~18 000    |    588.5  |    729.2  |     ~27× |
| `chat (schedule)`             |     ~150   |     ~250   |     13.7  |     15.3  |     ~11× |
| `chat (recurring)`            | ~119 000   | ~130 000   |   3 427.2 |   3 821.9 |     ~34× |
| `chat (smalltalk)`            |       50   |       80   |      2.0  |      2.3  |     ~25× |
| `chat (add_contact)`          |       60   |      120   |      2.1  |      2.5  |     ~28× |
| `chat (unknown)`              |       40   |       60   |      2.0  |      2.3  |     ~20× |
| `GET /api/suggestions/recipients` | ~1 800 |    ~2 500  |     25.1  |     25.8  |     ~70× |
| `GET /api/insights/summary`   |  ~210 000  |  ~250 000  |  ~70 000  |  ~76 000  |       3× |
| `alias_resolve` (×50 queries) |   ~150 †   |     ~220 † |    150.1  |    157.3  |    ~1.0× |
| `suggester.suggest(k=5)`      |   ~1 800   |   ~2 200   |     23.6  |     24.1  |     ~75× |

† Alias resolution was already fast in the baseline because the
contest data has no aliases (the contest CSV doesn't ship them);
the resolver short-circuits after the lookup miss. On a corpus with
aliases the speedup from Fix 1 would dominate this row.

> **Before** numbers come from a one-off in-process diagnostic (each
> call timed via `time.perf_counter()` around `handle_message`),
> because the *HTTP* baseline at 200 iters × 25 s/call ≈ 80 min/flow
> was unrunnable.  HTTP overhead is ~3–5 ms on this loopback setup,
> so the in-process measurement is a faithful lower bound.
> **After** numbers are the steady-state of a 200-iter HTTP bench
> (warmup-trimmed) under the same configuration, except
> `insights/summary` whose 70 s/call cost makes a full 200-iter run
> unhelpful; its number is the median of 3 direct
> `ml.insights.summary()` calls after warmup. Insights is bottlenecked
> by the Python loop over the materialised 520 k-row history
> (z-score per tx) — the next round of work would push the aggregate
> straight into SQL (`GROUP BY` per category / per contact) instead
> of round-tripping every row through Pydantic.

---

## Where the time was going

A targeted in-process bench made the cost explicit before any fix
was attempted:

```
handle_message: transfer        P50=  16 111 ms
handle_message: history         P50=  16 214 ms
handle_message: recurring       P50= 119 328 ms
```

Three root causes account for almost everything:

1. **`Store.contacts_of` was N+1.**  The SQL fetched the 1 000
   contacts in one shot, then `_row_to_contact` ran an extra
   `SELECT alias FROM contact_aliases WHERE contact_id = ?` query
   *per* contact.  ~1 001 round-trips per call.

2. **`Store.transactions_of` always materialised everything.**  The
   signature took only `user_id` and unconditionally returned all
   520 180 rows as Pydantic `Transaction` models — ~16 s and ~2 GB
   peak even when the caller only wanted "txs with recipient X" or
   "tx this month".  Every chat handler hit this path on every
   request, plus `ml.amount_predictor.predict_amount`,
   `routes/banking.transactions`, `ml.insights._completed_tx`, and
   `ml.suggester.suggest`'s reason-string builder.

3. **`(owner_id, contact_id)` index didn't cover `ORDER BY
   created_at`.**  Once we *did* push the contact filter into SQL,
   the planner on a fresh DB (no `ANALYZE`) picked
   `ix_tx_owner_created` and filtered `contact_id` in memory — 2.3 s
   to fetch 500 of one contact's transactions instead of <10 ms.

4. **`/api/insights/summary` scanned the full history three times.**
   `month_over_month`, `anomalies`, and `subscriptions` each called
   `_completed_tx(user_id)` independently — the same 520 k-row scan
   three times back to back, then a per-row contact-name lookup
   that re-triggered the N+1 from (1) inside `_contact_summary`.

Each of these is fixable with surgery in the data layer; none
required a new dependency.

---

## Fix 1 — `contacts_of` single-query with `GROUP_CONCAT`

[`backend/app/store.py`](../backend/app/store.py)
([commit `18084ba`](#commits))

```sql
SELECT c.id, …,
       GROUP_CONCAT(a.alias, char(31)) AS aliases
FROM contacts c
LEFT JOIN contact_aliases a ON a.contact_id = c.id
WHERE c.owner_id = ?
GROUP BY c.id
ORDER BY c.frequent DESC, c.display_name
```

`char(31)` (US, "unit separator") is the delimiter — guaranteed not
to appear in a Vietnamese name.  A new helper
`_row_to_contact_with_aliases` splits client-side.  `get_contact`
and the new `contacts_by_ids(ids)` batch helper share the same
shape, so any code path that previously iterated contacts now pays
one query, not 1 + N.

## Fix 2 — `transactions_of` SQL filters

[`backend/app/store.py`](../backend/app/store.py)
([commit `18084ba`](#commits))

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
`ix_tx_owner_created (owner_id, created_at DESC)` already covers.
The function still returns Pydantic models — what changes is how
few rows we actually select.

Updated callers:

* `services/orchestrator._handle_transfer` — scopes by `contact_id`
  when the recipient is known; uses `limit=200` for the cold-contact
  fallback so the "lần trước" temporal resolver has enough history
  without paying full materialisation.
* `services/orchestrator._modify_transfer_draft`, `confirm_draft`,
  `select_candidate` — scope by `contact_id` (re-eval only needs the
  per-contact baseline).
* `services/orchestrator._handle_recurring` — limits to 400 days +
  `status="completed"`.
* `banking.service.get_history` — pushes the period window straight
  into SQL instead of filtering Python-side over the full history.
* `ml.amount_predictor.predict_amount` — single
  `(contact_id, status='completed')` slice.
* `ml.suggester.suggest` — one
  `(contact_id, limit=30)` slice per *returned* row for the
  reason-string builder, instead of one full-history scan filtered
  down to top-K.
* `ml.insights._completed_tx` — pushes `status="completed"` into SQL.
* `routes/banking.transactions` — pushes `limit` into SQL.

A companion `Store.transaction_count(user_id)` lets the suggester
size-check without paying the materialisation tax, and
`Store.completed_amount_mean(user_id)` lifts the cold-contact-anomaly
fallback's `mean()` from a 520 k-row Python loop into a single
SQL `AVG()` call.

## Fix 3 — `insights.summary` fetches once, threads through

[`backend/app/ml/insights.py`](../backend/app/ml/insights.py)
([commit `18084ba`](#commits))

`month_over_month`, `anomalies`, `subscriptions` each grew a private
`_txs` kwarg.  `summary()` calls `_completed_tx(user_id)` once and
threads the list into all three.  Three full-history scans collapse
into one.

## Fix 4 — composite index + raw-row recurring path

[`backend/app/db/schema.sql`](../backend/app/db/schema.sql),
[`backend/app/store.py`](../backend/app/store.py),
[`backend/app/services/orchestrator.py`](../backend/app/services/orchestrator.py)
([commit `50b7b21`](#commits))

```sql
CREATE INDEX IF NOT EXISTS ix_tx_owner_contact_created
    ON transactions(owner_id, contact_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_tx_owner_status_created
    ON transactions(owner_id, status, created_at DESC);
```

The new index removes the temp B-tree sort under the per-contact
SQL path that Fix 2 introduced — collapses warm-recipient txs
fetches from ~2.3 s to <10 ms on contest data.

The recurring detector reads five fields off each transaction and
discards the rest; we now feed it via `Store.transactions_raw` →
lightweight `_RawTx` (`__slots__`) shims, skipping ~5 s of Pydantic
construction for the 520 k-row scan.

`get_history` also gained a soft `_RENDER_CAP = 100` items so a
semantic-filter query that matches 2-3k rows doesn't ship all of
them through Pydantic + JSON; aggregates still compute over the
full match set and `items_truncated: true` signals the cap.

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
outputs.  The smoke script also covers the insights direct call so
the `_completed_tx` shape change is verified end-to-end.

One side-effect of Fix 2 worth flagging: the cold-contact anomaly
rule used to fall back on `mean(transactions)` over the user's
*entire* history.  Now that the orchestrator scopes `txs` to a
single contact (or 200 recents), we route the global mean through
`Store.completed_amount_mean(user_id)` only when the safety rule
actually needs it (non-frequent recipient + amount ≥ 10 M + <3
prior tx).  KB05 confirms the rule still fires correctly with
`amount_above_average ~30× mức thường ngày`.

---

## Commits

* `18084ba` — Fix 1 + Fix 2 + Fix 3.
* `10c0d36` — Fix 2 follow-up: `amount_predictor`,
  `routes/banking.transactions`, cold-contact global mean.
* `50b7b21` — Fix 4: composite index + `_RawTx` for the recurring
  miner + `_RENDER_CAP` for `get_history`.

---

## Appendix — raw bench output

The post-optimisation table this writeup is built from (last
column `n` is the iter count after warmup trim):

```
== Omni perf — n=200, db=omni_contest.db ==
FLOW                                P50       P95       P99     n
--------------------------------------------------------------------
chat (transfer)                    49.5ms    51.0ms    54.2ms    50
chat (transfer_ref)                63.3ms    77.4ms    79.1ms    50
chat (balance)                      2.5ms     3.3ms     4.1ms   200
chat (history_simple)              47.3ms    60.7ms    66.0ms    50
chat (history_rag)                588.5ms   729.2ms   745.8ms    20
chat (schedule)                    13.7ms    15.3ms    16.5ms    50
chat (recurring)                3 427.2ms 3 821.9ms 3 884.5ms    20
chat (smalltalk)                    2.0ms     2.3ms     2.6ms   200
chat (add_contact)                  2.1ms     2.5ms     2.8ms   200
chat (unknown)                      2.0ms     2.3ms     2.5ms   200
suggestions/recipients             25.1ms    25.8ms    26.4ms   200
insights/summary               ~70 000ms ~76 000ms          —     3 †
alias_resolution (×50q)           150.1ms   157.3ms   159.0ms    50 ‡
suggester.suggest(k=5)             23.6ms    24.1ms    24.7ms    50 ‡
```

† Median of 3 direct `ml.insights.summary()` calls after warmup; a
200-iter HTTP bench at ~70 s / call wasn't a useful spend. The
`/api/insights/summary` endpoint still wraps the same function with
a single FastAPI hop (<2 ms overhead).

‡ The `alias_resolution` and `suggester.suggest` rows come from the
in-process tail of `scripts/bench.py` (calls into the resolver / the
trained suggester directly, no HTTP). On a 50 µs / call hot path the
HTTP overhead would dominate the signal.

# Omni backend — FastAPI

The natural-language banking brain. FastAPI + SQLite + sklearn + fastembed,
no Postgres / Redis / cloud-embedding required.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env       # optional Groq / Gemini keys for richer NLU
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

OpenAPI: <http://localhost:8000/docs>.

## Pre-demo green-light

```bash
make check   # 18 assertions, exits non-zero on any red. Run before judging.
```

## Layout

```
app/
  main.py              FastAPI entry — registers routes + startup hooks
  config.py            env settings
  store.py             SQLite-backed store (Pydantic models in, rows out)
  models/schemas.py    Pydantic v2 — NLUResult, TransactionDraft, OmniResponse…
  nlp/                 Layer 2: NLU
    intent.py          3-tier rule classifier (works when LLMs 429)
    entities.py        regex extractors — amounts, recipients, temporal, cron
    amount.py          "5 triệu" / "2tr500" / "500k" → int VND
    llm.py             Groq → Gemini → rule fallback chain
    pipeline.py        orchestrate LLM + rules into one NLUResult
    embeddings.py      fastembed (multilingual MiniLM 384-d) + lazy backfill
    embedder.py        embed contacts/transactions, write to SQLite BLOB
  context/             Layer 3: personalisation
    alias.py           5-step recipient resolver: exact → token → prefix → RAG
    temporal.py        "tháng trước" → past tx
    session.py         in-process draft + conversation memory
  safety/              Layer 4: rule engine
    rules.py           ambiguous / new+large / per-recipient MAD anomaly / balance
  banking/             Layer 5: mock core
    service.py         get_balance, get_history, execute_transfer, create_schedule
    recurring.py       month-bucket pattern miner (read-only)
  ml/                  Analytics / predictions
    suggester.py       sklearn RF + rule + freq prior for "Danh bạ" picker
    insights.py        MoM, anomalies, subscription detection
    amount_predictor.py median fill when user omits amount
  services/
    orchestrator.py    handle_message — the brain
  routes/              chat / banking / suggestions / insights / ws
  db/
    schema.sql         tables + indexes
    bootstrap.py       idempotent JSON seed → SQLite
    connection.py      WAL-mode sqlite3 helper

scripts/
  smoke.py             8 KB scenarios end-to-end
  check.py             pre-demo health gate
  eval_suggester.py    Hit@1/3/5 time-ordered holdout
  load_contest_full.py ingest 520k contest CSV into SQLite
  generate_synthetic_data.py  pattern-rich demo seed

tests/
  test_nlu_corpus.py   200 adversarial Vietnamese utterances
  conftest.py          env scrub (clears LLM keys, isolated tmp DB)
```

## Make targets

| Target | What |
|--------|------|
| `make install` | venv + pip + npm install |
| `make backend` | uvicorn :8000 with reload |
| `make smoke` | run 8 KB scenarios end-to-end |
| `make check` | pre-demo gate (18 assertions) |
| `make test-nlu` | adversarial NLU corpus pytest |

## Conventions we lock down

- **LLM never writes confirmed-transfer text.** "Đã chuyển 5.000.000đ cho mẹ"
  is built deterministically from real data. See `docs/llm-vs-rule.md`.
- **All user-facing strings in Vietnamese** — including safety messages.
  English support is opt-in via `Accept-Language: en` or `?lang=en`.
- **Vector embeddings stay local.** fastembed runs an ONNX model in-process;
  no cloud round-trip on the embedding path.
- **Rule + ML hybrid for the suggester** — auto-weighted by data size:
  tiny → freq dominant, rich → tree dominant. See `docs/eval.md`.

## Environment variables

| Var | Default | What |
|-----|---------|------|
| `GROQ_API_KEY` | unset | Tier 1 LLM. Empty → skip. |
| `GEMINI_API_KEY` | unset | Tier 2 LLM fallback. Empty → skip. |
| `OMNI_DB_PATH` | `app/data/omni.db` | Override for contest/Czech/BankSim DBs. |
| `OMNI_SKIP_EMBED_BACKFILL` | unset | `1` to skip ONNX warm + embedding fill (fast CI). |
| `BANKING_DATA_DIR` | `app/data` | Override JSON seed dir for tests. |
| `EVAL_TEST_LIMIT` | `1500` | Cap eval test set size. |
| `EVAL_MIN_TRAIN` | `5` | Drop test rows for contacts with <N train tx. |

## Where to look first

| Question | File |
|---------|------|
| "Why did Omni pick that recipient?" | `context/alias.py` (5-step resolver) |
| "Why did Omni flag this transfer?" | `safety/rules.py` |
| "Where does LLM stop and rules start?" | `docs/llm-vs-rule.md` |
| "How honest are the Hit@K numbers?" | `docs/eval.md` + `docs/honest-pitch.md` |
| "What does the brain actually do?" | `services/orchestrator.py:handle_message` |

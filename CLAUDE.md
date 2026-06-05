# Omni — AI Banking Assistant

**Competition:** HACK\<CX\>TOGETHER · **Team:** One Last Token
**Slide deck source of truth:** `Bản-sao-của-hackbanking-1.pdf` at repo root.

## Objective

Build a Vietnamese natural-language banking assistant that **collapses the
classic 7-step Smart Banking transfer flow into Chat → Confirm → Done**,
with three differentiators the slide deck explicitly calls out:

1. **Hiểu ý định, không phải câu chữ** — "Gửi mẹ 5 triệu như tháng trước"
   resolves to the right contact, the right amount, and the right note
   from history.
2. **Hiểu ngữ cảnh cá nhân** — aliases (mẹ → Nguyễn Thị Lan), temporal
   references (như tháng trước), conversational follow-ups (đổi sang 3
   triệu, người kia, cuối, …).
3. **An toàn vẫn được đảm bảo** — rule engine flags ambiguous recipients,
   new-recipient + large amount, statistical anomalies, insufficient
   balance; OTP step-up for warn-flag transfers.

We also explore three things beyond the slide brief:

4. **RAG fuzzy contact lookup** ("cô bán bún chợ", "anh đồng nghiệp
   marketing") via local fastembed embeddings + SQLite.
5. **Tree-based next-recipient suggester** that ranks the user's
   contacts for "right now" — date features (X/X sale days, đầu/cuối
   tháng, decade-of-month, DOW), combined with frequency and rule
   priors. Powers the "Danh bạ" picker.
6. **Vector + lexical history search** so questions like "tiêu cho ăn
   uống tháng trước" resolve semantically against transaction
   descriptions, not just by category labels.
7. **Recurring-payment detection** — mines history bucketed by
   `(year, month)` to surface "Mình có khoản nào trả định kỳ không?"
   without the user setting up a schedule explicitly. Filters noisy
   descriptions (`ok`, `test`, `asdf`) common in the contest dataset.

## Architecture (slide layers)

| # | Layer | Tech | Files |
|---|-------|------|-------|
| 1 | Chat UI | React + Vite (TS), phone-frame mock | `frontend/src/` |
| 2 | NLU | Groq Llama 3.3 70B + Gemini fallback + rule extractors | `backend/app/nlp/` |
| 3 | Context | alias resolver, temporal resolver, in-process session, RAG | `backend/app/context/`, `backend/app/db/` |
| 4 | Safety | rule engine (ambiguous, anomaly, balance, OTP step-up) | `backend/app/safety/rules.py` |
| 5 | Banking | mock transfer/balance/history/schedule, all gated by user confirm | `backend/app/banking/`, `backend/app/store.py` |
| + | ML suggester | sklearn RandomForest + rule scorer + freq prior | `backend/app/ml/suggester.py` |
| + | Embeddings | fastembed (local ONNX, multilingual MiniLM 384-d) | `backend/app/nlp/embeddings.py` |
| + | Recurring detector | month-bucket pattern miner over history | `backend/app/banking/recurring.py` |

The orchestrator (`backend/app/services/orchestrator.py:handle_message`)
threads everything together: continuation paths (xác nhận / huỷ / OTP) →
NLU (LLM with conversation history, rule fallback) → modify-draft check
→ intent dispatch → response composition.

## How to run

```bash
# Backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env       # paste GROQ_API_KEY / GEMINI_API_KEY if you have them
.venv/bin/python -m uvicorn app.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev                # http://localhost:5173
```

Live UI: <http://localhost:5173>. OpenAPI: <http://localhost:8000/docs>.

`make backend` / `make frontend` / `make smoke` are shortcuts in the
top-level `Makefile`.

## Branching & merge policy

* `main` is what's safe to demo.
* `feat/db-rag` is the active development branch (SQLite migration +
  fastembed RAG + suggester ML + contest-data eval). Most recent work
  lives here.
* Open a PR from `feat/db-rag` → `main` once a feature passes the
  scenarios in `backend/scripts/smoke.py` end-to-end.

## Data

| Location | What | Used by |
|----------|------|---------|
| `backend/app/data/*.json` | Hand-curated 30-contact / 35-tx demo seed | Default bootstrap into `omni.db` |
| `data/demo/*.json` | Contest-derived 1000-contact / 1888-tx subset | `BANKING_DATA_DIR=../data/demo` |
| `generated/transactions_enriched_6m.csv` | Full 591k-row contest CSV | `scripts/load_contest_full.py` → `omni_contest.db` |
| `data/public/czech_pkdd99/*.tsv` | Czech PKDD'99 real bank data (1.05M tx + `permanent_orders` ground truth) | `scripts/load_czech.py` → `omni_czech.db` |
| `data/public/banksim/bs140513_032310.csv` | BankSim (594k tx with labelled fraud) | `scripts/load_banksim.py` → `omni_banksim.db` |
| `backend/app/data/omni.db` | Runtime SQLite (gitignored) | Bootstraps from JSON on first run |

Reset: `rm backend/app/data/omni.db` and restart uvicorn.
Public dataset download: `data/public/README.md`.

## Honest caveats (numbers from `docs/eval-real-data.md`)

Replaces the earlier "synthetic proof" wording in pitch decks. Every
number below is from a public real-world dataset, evaluated by scripts
checked into `backend/scripts/`.

* **Recurring detector** — F1 **0.74** (precision 0.69, recall 0.80) on
  Czech PKDD'99 `permanent_orders` ground truth (25 real orders across
  5 demo accounts / 2 488 tx). The 9 "false positives" are de-facto
  monthly patterns the customer never formally registered — operationally
  useful, not failure modes.
* **Suggester** — Hit@1 **0.81** / Hit@3 **0.92** / Hit@5 **0.97** on
  BankSim (50 merchants, 50 users, 1 740-row held-out test). Best
  ablation is `tree+freq` (0.60/0.40, no rule); the VN-specific rule
  scorer *hurts* on non-VN data — keep it locale-gated.
* **Fraud Isolation Forest** — separates fraud (median score 0.58) from
  legit (0.22) on BankSim labelled fraud. At threshold 0.5: recall
  **0.75**, precision 0.14, FP-rate-on-legit 0.11 — usable as an
  OTP step-up signal. The current `FRAUD_RISK_THRESHOLD=0.7` default is
  mis-calibrated (recall drops to 0.13) and should be lowered.

## Key conventions (rules we've committed to)

* **Vietnamese error / response strings.** Even safety messages are in
  Vietnamese — this is a Vietnamese banking product.
* **LLM never writes a confirmed-transfer line.** "Đã chuyển 5.000.000đ
  cho mẹ" is a *safety contract* — built deterministically from real
  data. See `docs/llm-vs-rule.md` for the full boundary table.
* **Multi-provider LLM chain.** Groq tried first, Gemini fallback,
  rule-based extractors below that. The demo can't break on rate limit.
* **Rule + ML hybrid for the suggester.** Auto-weighted by data size —
  tiny data → freq dominant, rich data → tree dominant.
* **Vector embeddings stay local.** fastembed + multilingual MiniLM, no
  cloud dependency on the embedding path. Backfilled at startup.
* **No emoji in user-facing strings unless asked.** No marketing
  flourishes in chat replies.

## Important files

| File | Role |
|------|------|
| `backend/app/services/orchestrator.py` | Brain — `handle_message` dispatch |
| `backend/app/nlp/pipeline.py` | NLU entry; merges LLM + rule extractor |
| `backend/app/nlp/llm.py` | Multi-provider chain (Groq / Gemini OpenAI-compat) |
| `backend/app/nlp/entities.py` | Rule-based regex extractors (Vietnamese-aware) |
| `backend/app/context/alias.py` | 5-step resolver: exact → token → prefix → RAG |
| `backend/app/safety/rules.py` | The flag engine |
| `backend/app/ml/suggester.py` | Tree + rule recipient ranker |
| `backend/app/banking/recurring.py` | Month-bucket recurring-payment miner |
| `backend/app/db/{schema.sql,bootstrap.py}` | SQLite schema + seed loader |
| `docs/llm-vs-rule.md` | When to use what — pitch material |
| `backend/scripts/eval_suggester.py` | Hit@K eval, time-ordered holdout (contest data) |
| `backend/scripts/eval_suggester_banksim.py` | Hit@K eval on BankSim public merchants — honest non-circular number |
| `backend/scripts/eval_recurring_czech.py` | Recurring detector P/R/F1 vs Czech PKDD'99 `permanent_orders` ground truth |
| `backend/scripts/eval_fraud_banksim.py` | Fraud Isolation Forest P/R/F1 vs BankSim labelled fraud |
| `backend/scripts/load_contest_full.py` | Ingest 521k contest tx into SQLite |
| `backend/scripts/load_czech.py` | Ingest Czech PKDD'99 dataset + permanent-orders ground truth |
| `backend/scripts/load_banksim.py` | Ingest BankSim (594k rows) preserving `fraud` labels |
| `backend/scripts/generate_synthetic_data.py` | Pattern-rich synthetic seed |
| `backend/scripts/smoke.py` / `demo.py` | End-to-end demo scenarios |
| `docs/eval-real-data.md` | Public-dataset evaluation report (Czech recurring + BankSim fraud + BankSim Hit@K) |

## Intent reference

Current set in `app/models/schemas.py:Intent`:

| Intent | Meaning | Handler |
|--------|---------|---------|
| `transfer` | "Chuyển cho mẹ 2 triệu" | `_handle_transfer` |
| `balance` | "Số dư bao nhiêu" | `_handle_balance` |
| `history` | "Tháng trước tiêu bao nhiêu" — read-only aggregation | `_handle_history` |
| `schedule` | "Đặt lịch chuyển mẹ 2tr mùng 1 hàng tháng" — CREATE | `_handle_schedule` |
| `recurring` | "Mình có khoản nào trả đều hàng tháng?" — READ patterns mined from history | `_handle_recurring` |
| `add_contact` | "Lưu Nam STK xxx MB Bank" | `_handle_add_contact` |
| `reminder` | "Nhắc nợ X" | placeholder |
| `smalltalk` | "Chào Omni" | static or LLM phrased |
| `unknown` | anything else | safe static fallback |

`schedule` (CREATE) and `recurring` (READ) are intentionally separate —
they share Vietnamese vocabulary ("định kỳ", "hàng tháng") but the user
intent diverges. The Tier-1 keywords in `app/nlp/intent.py` disambiguate
imperative vs interrogative phrasing.

## Open questions / pending decisions

* **Real contest data is HEAVY** (520k outgoing tx, 1000 counterparties).
  Numbers from `eval_suggester.py` with `OMNI_DB_PATH=app/data/omni_contest.db`
  are the most honest measure of the suggester. Cap `EVAL_TEST_LIMIT`
  and `EVAL_MIN_TRAIN` for fast iteration.
* **OTP flow.** Mock code `123456`. Real OTP service integration is out
  of scope for the hackathon but the contract (`requires_step_up`,
  `awaiting_otp`) is wired so swapping it in is trivial.
* **Persistence of in-flight drafts.** Sessions live in process memory.
  For multi-instance, Redis with TTL — currently noted in audit, not
  built.

## When you (Claude) help

* Prefer editing existing files over creating new ones.
* If a feature can be expressed deterministically (rule, SQL filter),
  prefer that to a new LLM call.
* If you must call the LLM for response phrasing, the system prompt in
  `nlp/llm.py:_PHRASE_SYSTEM` is the safety contract — don't relax it.
* Run `python -c "from app.main import app"` after backend edits to
  catch import / Pydantic errors before restart.
* Vite has HMR; you don't need to restart for `frontend/src/**` edits.
* Don't commit `*.env` or `*.db` (see `.gitignore`).

# Omni — AI Banking Assistant

**Competition:** HACK\<CX\>TOGETHER · **Team:** One Last Token
**Slide deck source of truth:** `Bản-sao-của-hackbanking-1.pdf` at repo root.

## Objective

Build a Vietnamese natural-language banking assistant that collapses the
classic 7-step Smart Banking transfer flow into **Chat → Confirm → Done**,
with three differentiators from the slide deck:

1. **Intent over wording.** "Gửi mẹ 5 triệu như tháng trước" resolves
   to the right contact, the right amount, and the right note from
   the user's own history.
2. **Personal context.** Aliases (mẹ → Nguyễn Thị Lan), temporal
   references (như tháng trước), conversational follow-ups (đổi sang
   3 triệu, người kia, cuối, …).
3. **Safety stays on.** Rule engine flags ambiguous recipients, new
   recipient + large amount, statistical anomalies, insufficient
   balance, and an Isolation Forest fraud score. OTP step-up gates
   any warn-flag transfer.

Three extensions beyond the slide brief:

4. **RAG fuzzy contact lookup** ("cô bán bún chợ", "anh đồng nghiệp
   marketing") via local fastembed embeddings + SQLite.
5. **Tree-based next-recipient suggester** ranking contacts for "right
   now" — date features (sale days, đầu/cuối tháng, decade-of-month,
   DOW) combined with frequency and rule priors.
6. **Vector + lexical history search** so "tiêu cho ăn uống tháng
   trước" resolves semantically against descriptions, not labels.
7. **Recurring-payment detection** mining history bucketed by
   `(year, month)` so "khoản nào trả định kỳ?" works without an
   explicit schedule. Noise filter strips contest-dataset garbage
   (`ok`, `test`, `asdf`).

## Architecture

Five-layer slide model plus an "extensions" tier. The orchestrator
(`backend/app/services/orchestrator.py:handle_message`) threads
continuation paths (confirm / cancel / OTP) → NLU → modify-draft
check → intent dispatch → response composition.

| # | Layer | Tech | Files |
|---|-------|------|-------|
| 1 | Chat UI | React + Vite (TS), phone-frame mock | `frontend/src/` |
| 2 | NLU | Groq Llama 3.3 70B → Gemini fallback → rule extractors | `backend/app/nlp/` |
| 3 | Context | alias resolver, temporal resolver, in-process session, RAG | `backend/app/context/`, `backend/app/db/` |
| 4 | Safety | rule engine (ambiguous, new+large, MAD anomaly w/ structured `details`, balance, Isolation Forest fraud) + OTP step-up | `backend/app/safety/rules.py`, `backend/app/safety/fraud_model.py` |
| 5 | Banking | mock transfer / balance / history / schedule, all gated by user confirm | `backend/app/banking/`, `backend/app/store.py` |
| + | Suggester (ML) | sklearn RandomForest + rule scorer + frequency prior; auto-weighted by data size | `backend/app/ml/suggester.py` |
| + | Embeddings | fastembed (local ONNX, multilingual MiniLM 384-d), backfilled at startup | `backend/app/nlp/embeddings.py` |
| + | Recurring detector | Month-bucket pattern miner over history | `backend/app/banking/recurring.py` |
| + | Insights | MoM deltas, per-recipient z/MAD anomaly, subscription detection | `backend/app/ml/insights.py` |
| + | Amount predictor | Median-from-history fill when user omits amount; returns `{amount, confidence, rationale}` | `backend/app/ml/amount_predictor.py` |
| + | Categorizer | TF-IDF + rules (13 categories, P=0.95, <2 ms) | `backend/app/ml/categorizer.py` |
| + | A/B + Thompson bandit | 4 weight arms, online learning; winner `tree_freq` ≈ 67.55% | `backend/app/ml/abtest.py`, `backend/app/ml/bandit.py` |
| + | Voice input | Web Speech (vi-VN), browser-side, no cloud STT | `frontend/src/components/VoiceButton.tsx` |
| + | TTS replies | Browser speechSynthesis (vi-VN), opt-in toggle | `frontend/src/lib/tts.ts` |
| + | Suggestion strip | Top-N recipient chips above input with confidence bar | `frontend/src/components/SuggestionStrip.tsx` |
| + | TransactionCard | Confirm card: predicted amount chip + confidence badge + tooltip rationale, inline ✎ edit-amount, per-recipient mini-ledger, anomaly bar chart, fraud step-up banner, source-account picker, category chip, success animation | `frontend/src/components/TransactionCard.tsx` |
| + | RepeatLastCTA | "Lặp lại lần trước" + sibling "Cùng số tiền, người khác" CTA | `frontend/src/components/RepeatLastCTA.tsx` |
| + | QuickAmountChips | 100k / 500k / 1tr / 2tr / 5tr chips when user types "chuyển …" without an amount | `frontend/src/components/QuickAmountChips.tsx` |
| + | HistoryCard | Last-5 list with auto-categoriser colour tag per row | `frontend/src/components/HistoryCard.tsx` |
| + | BalanceCard | Total + 7-day spending sparkline + account list | `frontend/src/components/BalanceCard.tsx` |
| + | ScheduleCard | Cron preview pill (Vietnamese, via `lib/cron.ts`) + next-run countdown | `frontend/src/components/ScheduleCard.tsx` |
| + | InsightsCard / RecurringList / BudgetCard / GoalsCard | Sidebar widgets that re-fetch on draft confirm | `frontend/src/components/` |
| + | Slash commands + @-autocomplete | `/transfer /balance /history /repeat /insights /help` + Cmd+K/Cmd+/ + `@-mention` | `frontend/src/components/SlashPalette.tsx`, `frontend/src/hooks/useKeyboard.ts` |
| + | Toast events | 6 WS event kinds, per-user `asyncio.Queue`, 64-entry backlog | `backend/app/services/events.py`, `frontend/src/components/ToastStack.tsx` |
| + | Metrics | 7 Prometheus series via `/api/metrics`, live dashboard at `?metrics=1` | `backend/app/services/metrics.py`, `frontend/src/components/MetricsCard.tsx` |
| + | Health probes | `/health/{live,ready,version}` + lifespan shutdown + k8s hints | `backend/app/routes/health.py`, `docs/deploy/k8s-hints.yaml` |
| + | Budgets + goals | Monthly envelopes + savings tracker, 4 new intents | `backend/app/banking/budgets.py`, `backend/app/routes/budgets.py` |
| + | Privacy mode | `OMNI_PRIVACY_MODE={off,redact,local-only}` + redactor + LLM audit ring buffer | `backend/app/nlp/redactor.py`, `backend/app/nlp/privacy.py` |
| + | Exports | CSV / sao kê HTML / tax-year JSON | `backend/app/routes/exports.py`, `frontend/src/components/ExportMenu.tsx` |
| + | a11y | WCAG 2.1 AA, focus ring, `prefers-reduced-motion`, jest-axe harness | `frontend/src/lib/axe.ts`, `docs/a11y-audit.md` |
| + | Demo resilience | `OMNI_OFFLINE_DEMO=1` + telemetry overlay + scenario recorder + canonical JSONL replay | `backend/app/routes/demo.py`, `docs/offline-demo.md` |
| + | Redis sessions | `OMNI_SESSION_BACKEND={memory,redis,fake-redis}` + draft/session TTLs | `backend/app/context/session_store.py`, `docs/persistence.md` |
| + | Chat history | Durable SQLite conversation archive + left-drawer sidebar (list / reopen / new / delete), titled from first message. Separate from the ephemeral session store | `backend/app/db/chat_log.py`, `backend/app/routes/chat.py`, `frontend/src/components/ChatHistory.tsx` |
| + | ATM finder | 15-seed mock with Haversine + NLU "ATM gần nhất" | `backend/app/banking/atm.py`, `backend/app/routes/atm.py` |
| + | VietQR codec | Custom TLV encode/decode + jsQR camera scan (47 kB lazy chunk) | `backend/app/banking/qr.py`, `frontend/src/components/QrScanButton.tsx` |
| + | Onboarding | 4-step tutorial overlay + skills discovery (13 chips × 5 categories) | `frontend/src/components/TutorialOverlay.tsx` |

## TransactionDraft schema (single source of truth for the confirm card)

Defined in `backend/app/models/schemas.py:TransactionDraft`. Fields the
UI cares about:

* `recipient`, `candidates`, `amount`, `description`, `flags`,
  `requires_step_up`, `awaiting_otp` — the basics.
* `category` — auto-inferred by the categoriser; renders as a colored
  chip below the amount.
* `predicted_amount: bool` — set when the amount predictor filled in
  a missing amount.
* `amount_prediction_reason: Optional[str]` — Vietnamese rationale
  ("Median của 4 lần chuyển trong cùng dải ngày"); surfaced as the
  tooltip on the "đề xuất từ lịch sử" chip.
* `amount_prediction_confidence: Optional[float]` — [0, 1]; renders
  as an outlined "85%" pill next to the chip.
* `recent_to_recipient: Optional[list[dict]]` — last 3 completed
  transfers to the chosen recipient (`amount / created_at /
  description / category`). Renders as an inline mini-ledger and as
  the input series for the per-recipient bar chart.
* `source_account_id`, `source_accounts` — multi-account picker.
* `SafetyFlag.details: Optional[dict]` — populated for
  `amount_above_average`; carries `kind / median / p90 / n_samples /
  ratio / current_amount`, surfaced as a "why" box under the warn
  line and as the bar-chart colour switch.

## How to run

```bash
# Backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env       # paste GROQ_API_KEY / GEMINI_API_KEY if available
.venv/bin/python -m uvicorn app.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev                # http://localhost:5173
```

Live UI: <http://localhost:5173>. OpenAPI: <http://localhost:8000/docs>.

Shortcuts via the top-level `Makefile`:

* `make install / backend / frontend` — bootstrap and run.
* `make smoke / check / test / test-nlu / verify` — quality gates.
  `make verify` is the single pre-pitch green-light.
* `make reset` — pitch-day panic button (wipes runtime DB, re-seeds).
* `make docker-build / docker-run / docker-redis` — containerised paths.

## Branching policy

* `main` is what's safe to demo.
* `feat/omni-integrated` is the working integration branch — every
  feature commit lands here after `make verify` is green.
* Open PRs from `feat/<feature>` → `feat/omni-integrated` once the
  feature passes the scenarios in `backend/scripts/smoke.py`.

## Data sources

| Location | What | Used by |
|----------|------|---------|
| `backend/app/data/*.json` | Hand-curated 30-contact / 35-tx demo seed | Default bootstrap into `omni.db` |
| `data/demo/*.json` | Contest-derived 1000-contact / 1888-tx subset | `BANKING_DATA_DIR=../data/demo` |
| `generated/transactions_enriched_6m.csv` | Full 591k-row contest CSV | `scripts/load_contest_full.py` → `omni_contest.db` |
| `data/public/czech_pkdd99/*.tsv` | Czech PKDD'99 real bank data (1.05M tx + `permanent_orders` ground truth) | `scripts/load_czech.py` → `omni_czech.db` |
| `data/public/banksim/bs140513_032310.csv` | BankSim (594k tx with labelled fraud) | `scripts/load_banksim.py` → `omni_banksim.db` |
| `backend/app/data/omni.db` | Runtime SQLite (gitignored) | Bootstraps from JSON on first run |

Reset: `rm backend/app/data/omni.db` and restart uvicorn.
Public dataset download instructions: `data/public/README.md`.

## Honest results (from `docs/eval-real-data.md`)

Every headline number below is from a public real-world dataset,
evaluated by scripts in `backend/scripts/`. No synthetic-data
self-evaluation in the pitch.

* **Recurring detector** — F1 **0.74** (P=0.69, R=0.80) on Czech
  PKDD'99 `permanent_orders` ground truth (25 real orders across 5
  demo accounts / 2 488 tx). The 9 "false positives" are de-facto
  monthly patterns the customer never registered — operationally
  useful, not failure modes.
* **Suggester** — Hit@1 **0.81** / Hit@3 **0.92** / Hit@5 **0.97**
  on BankSim merchant labels (50 merchants, 50 users, 1 740-row
  held-out test). Best ablation is `tree+freq` (0.60 / 0.40); the
  VN-specific rule scorer hurts on non-VN data — keep it
  locale-gated.
* **Fraud Isolation Forest** — separates fraud (median score 0.58)
  from legit (0.22) on BankSim labelled fraud. At threshold 0.5:
  recall **0.75**, precision 0.14, FP-rate-on-legit 0.11. Usable as
  an OTP step-up signal. Current `FRAUD_RISK_THRESHOLD=0.7` default
  is mis-calibrated for recall and should be lowered.

## Conventions (rules we've committed to)

* **Vietnamese response strings everywhere user-facing.** Even safety
  messages and tooltips — this is a Vietnamese product.
* **LLM never writes a confirmed-transfer line.** "Đã chuyển
  5.000.000đ cho mẹ" is a safety contract built deterministically
  from real data; the system prompt in `nlp/llm.py:_PHRASE_SYSTEM`
  enforces this. See `docs/llm-vs-rule.md`.
* **Multi-provider LLM chain.** Groq tried first, Gemini fallback,
  rule-based extractors below that. Demo can't break on rate limits.
* **Rule + ML hybrid for the suggester.** Auto-weighted by data size
  — tiny data → frequency dominant, rich data → tree dominant.
* **Embeddings stay local.** fastembed + multilingual MiniLM. No
  cloud dependency on the embedding path. Backfilled at startup.
* **No emoji in user-facing strings unless asked.** No marketing
  flourishes in chat replies.
* **Each schema field that drives UI gets a regression test.** A
  silent drop of `recent_to_recipient` / `amount_prediction_*` /
  `recent_outflow_7d` would just hide the UI — no error on the wire
  — so we pin them at the schema layer in
  `tests/test_demo_safety_contract.py`.

## Important files

| File | Role |
|------|------|
| `backend/app/services/orchestrator.py` | Brain — `handle_message` dispatch |
| `backend/app/nlp/pipeline.py` | NLU entry; merges LLM + rule extractor |
| `backend/app/nlp/llm.py` | Multi-provider chain (Groq / Gemini OpenAI-compat) |
| `backend/app/nlp/entities.py` | Rule-based regex extractors (Vietnamese-aware) |
| `backend/app/context/alias.py` | 5-step resolver: exact → token → prefix → RAG |
| `backend/app/safety/rules.py` | Flag engine: missing/ambiguous, new+large, MAD anomaly w/ structured details, fraud_risk_high, insufficient balance |
| `backend/app/safety/fraud_model.py` | Per-user Isolation Forest + calibration; loaded at startup |
| `backend/app/ml/suggester.py` | Tree + rule + frequency recipient ranker |
| `backend/app/ml/amount_predictor.py` | Median-from-history amount fill w/ rationale + confidence |
| `backend/app/ml/categorizer.py` | TF-IDF + rules categoriser (13 categories) |
| `backend/app/ml/insights.py` | MoM + per-recipient anomaly + subscription detection |
| `backend/app/banking/recurring.py` | Month-bucket recurring-payment miner |
| `backend/app/banking/service.py` | `execute_transfer`, `get_balance` (with 7-day outflow series), `get_history` |
| `backend/app/db/{schema.sql,bootstrap.py}` | SQLite schema + seed loader |
| `backend/app/models/schemas.py` | Pydantic source of truth (TransactionDraft, SafetyFlag, OmniResponse, …) |
| `backend/scripts/smoke.py` / `demo.py` | End-to-end demo scenarios |
| `backend/scripts/check.py` | Pre-pitch green-light: 19 assertions (`make check`) |
| `backend/scripts/reset_demo.py` | Pitch-day panic button (`make reset`) |
| `backend/scripts/list_routes.py` | Print all FastAPI routes grouped by prefix |
| `backend/scripts/eval_suggester.py` | Hit@K eval, time-ordered holdout (contest data) |
| `backend/scripts/eval_suggester_banksim.py` | Hit@K eval on BankSim public merchants — non-circular |
| `backend/scripts/eval_recurring_czech.py` | Recurring P/R/F1 vs Czech PKDD'99 ground truth |
| `backend/scripts/eval_fraud_banksim.py` | Fraud Isolation Forest P/R/F1 vs BankSim |
| `backend/scripts/eval_suggester_holdout.py` | Pre-registered in-dist + cross-user Hit@K on synthetic v2 |
| `backend/scripts/load_contest_full.py` | Ingest 521k contest tx into SQLite |
| `backend/scripts/load_czech.py` | Ingest Czech PKDD'99 dataset + permanent-orders ground truth |
| `backend/scripts/load_banksim.py` | Ingest BankSim (594k rows) preserving `fraud` labels |
| `backend/scripts/gen_synthetic_users.py` | Parameterised multi-user synthetic generator |
| `backend/scripts/record_canonical_demo.py` | Generate `docs/demos/canonical-demo.jsonl` |
| `backend/scripts/categorize_backfill.py` | Re-categorise legacy tx with the categoriser |
| `frontend/src/lib/cron.ts` | Cron-to-Vietnamese formatter (used by ScheduleCard) |
| `frontend/tests/unit/` + `frontend/tests/e2e/` | Vitest unit tests + Playwright e2e |
| `Makefile` | `install backend frontend smoke check reset test-nlu test verify docker-*` |
| `docs/llm-vs-rule.md` | When to use what — pitch material |
| `docs/eval-real-data.md` | Public-dataset evaluation report |
| `docs/eval-protocol.md` | Pre-registered eval protocol for synthetic v2 |
| `docs/honest-pitch.md` | What we can vs cannot honestly claim |
| `docs/architecture.md` | End-to-end trace of one transfer (file:line pointers) |
| `docs/demo-script.md` | 4-minute live pitch flow with timing + recovery scripts |
| `docs/branch-status.md` | What's merged vs pending hand-merge |
| `PROGRESS.md` | 2-minute judges-facing summary |

## Intent reference

Current set in `app/models/schemas.py:Intent`:

| Intent | Meaning | Handler |
|--------|---------|---------|
| `transfer` | "Chuyển cho mẹ 2 triệu" | `_handle_transfer` |
| `balance` | "Số dư bao nhiêu" | `_handle_balance` |
| `history` | "Tháng trước tiêu bao nhiêu" — read-only aggregation | `_handle_history` |
| `schedule` | "Đặt lịch chuyển mẹ 2tr mùng 1 hàng tháng" — CREATE | `_handle_schedule` |
| `recurring` | "Khoản nào trả đều hàng tháng?" — READ patterns mined from history | `_handle_recurring` |
| `add_contact` | "Lưu Nam STK xxx MB Bank" | `_handle_add_contact` |
| `insights` | "Có giao dịch bất thường không?" | `_handle_insights` |
| `atm_finder` | "ATM gần nhất" | `_handle_atm_finder` |
| `set_budget / budget_status` | Monthly envelope CRUD/read | `_handle_budget*` |
| `set_goal / goal_status` | Savings goal CRUD/read | `_handle_goal*` |
| `smalltalk` | "Chào Omni" | static or LLM phrased |
| `unknown` | anything else | safe static fallback |

`schedule` (CREATE) and `recurring` (READ) are intentionally separate
— they share Vietnamese vocabulary ("định kỳ", "hàng tháng") but the
user intent diverges. The Tier-1 keywords in `app/nlp/intent.py`
disambiguate imperative vs interrogative phrasing.

## Open questions

* **Contest data is heavy** (520k outgoing tx, 1000 counterparties).
  `eval_suggester.py` with `OMNI_DB_PATH=app/data/omni_contest.db` is
  the most honest measure. Cap `EVAL_TEST_LIMIT` / `EVAL_MIN_TRAIN`
  for fast iteration.
* **OTP flow.** Mock code `123456`. The contract (`requires_step_up`,
  `awaiting_otp`) is wired so swapping in a real OTP service is
  trivial.
* **Session persistence.** In-memory or Redis (optional). Multi-
  instance Redis with TTL is configured but not load-tested.
* **Fraud threshold mis-calibration.** Default `FRAUD_RISK_THRESHOLD
  = 0.7` is too strict for our BankSim eval; recall drops to 0.13.
  Should be lowered to ~0.5 once we've re-run with the latest model.

## Notes for Claude

* Prefer editing existing files over creating new ones.
* If a feature can be expressed deterministically (rule, SQL filter),
  prefer that over a new LLM call.
* If you must call the LLM for response phrasing, the system prompt
  in `nlp/llm.py:_PHRASE_SYSTEM` is the safety contract — don't
  relax it.
* Run `python -c "from app.main import app"` after backend edits to
  catch import / Pydantic errors before restart.
* Vite has HMR — no restart needed for `frontend/src/**` edits.
* Don't commit `*.env` or `*.db` (see `.gitignore`).
* New schema field that powers UI? Add a regression test in
  `tests/test_demo_safety_contract.py` so a silent revert is caught.

# Omni — AI Assistant for Banking

> Team **One Last Token** · HACK\<CX\>TOGETHER  
> "Khách hàng không cần học cách dùng ngân hàng — ngân hàng học cách hiểu khách hàng."

A natural-language assistant that sits inside a mobile banking app. The user
types or speaks an intent ("Gửi cho mẹ 5 triệu như tháng trước") and Omni
turns it into a confirmed, auditable transaction — collapsing the 7-step
classic banking flow into Chat → Confirm → Done.

## Architecture

Five layers, exactly as proposed on slide 5:

```
   ┌────────────┐   ┌────────────┐   ┌─────────────────┐   ┌────────────┐   ┌────────────┐
   │ 1. Chat UI │──▶│ 2. NLU     │──▶│ 3. Context &    │──▶│ 4. Safety  │──▶│ 5. Banking │
   │ React +    │   │ Intent +   │   │  Personalization│   │ Rule       │   │ Mock Core  │
   │ REST/WS    │   │ Entities   │   │  Alias · Temp.  │   │ Engine     │   │ Banking    │
   └────────────┘   └────────────┘   └─────────────────┘   └────────────┘   └────────────┘
```

| Layer | Files (`backend/app/...`) | Stand-in for slide tech |
|-------|---------------------------|-------------------------|
| 1. Chat UI | `frontend/`, `routes/chat.py`, `routes/ws.py` | React Native → React Web; Socket.IO → FastAPI WebSocket |
| 2. NLU | `nlp/intent.py`, `nlp/entities.py`, `nlp/amount.py`, `nlp/llm.py`, `nlp/pipeline.py` | Gemini (optional) + spaCy-style rules + Pydantic |
| 3. Context | `context/alias.py`, `context/temporal.py`, `context/session.py` | Redis (short-term) + Postgres (long-term) + Pinecone — all in-memory for demo |
| 4. Safety | `safety/rules.py` | Rule Engine · JWT (header-based) · "AES-256" stand-in via header pass-through |
| 5. Banking | `banking/service.py`, `store.py`, `app/data/*.json` | Mock banking sandbox |

## Empirical results

Next-recipient suggester evaluated against the contest dataset and
against a synthetic proof-of-learning seed (full writeup in
[`docs/eval.md`](docs/eval.md)):

- **Hit@K on the contest-supplied 520k-tx dataset** (2,000-row
  time-ordered hold-out, 1,000 candidate contacts):
  Hit@1 = 0.002, Hit@3 = 0.005, Hit@5 = 0.007 — at uniform-random
  baseline. The contest data has no per-counterparty temporal pattern
  for the model to learn from (every contact is sampled ~equally across
  the 6 months).
- **Inflection point** — when the candidate pool is restricted to the
  user's top-20 most-frequent recipients (the realistic banking-app
  case), the tree beats the frequency baseline by ~35% relative
  (Hit@1 0.054 vs 0.040).
- **On pattern-rich synthetic (proof-of-learning)** (225 tx, 14
  contacts with weekly / monthly cadence): Hit@1 = 0.36, Hit@3 = 0.82,
  Hit@5 = 0.89 — the tree+freq hybrid clearly learns the patterns.

Eval runs in <20 s on the full 520k-row contest DB (in-memory after the
initial SELECT — no per-call SQL).

## All 6 demo scenarios pass

Run `python scripts/smoke.py` after starting the venv (`make smoke`):

| KB | Input | Behaviour |
|----|-------|-----------|
| 1 | "Chuyển cho Minh 2 triệu tiền ăn tháng này" | Ambiguous Minh → asks to pick |
| 2 | "Gửi cho mẹ 5 triệu như tháng trước" | Resolves *mẹ* → Nguyễn Thị Lan, fills description from prior tx |
| 3 | "Chuyển cho Minh 500k" | Same disambiguation, smaller amount |
| 4 | "Tháng này mình gửi mẹ bao nhiêu rồi?" | Filtered history: 2 tx, 7M total |
| 5 | "Chuyển 50 triệu cho Hùng STK 9990001234" | 3 safety flags: new recipient + 30× anomaly + insufficient balance |
| 6 | "Đặt lịch chuyển mẹ 2tr vào mùng 1 hàng tháng" | Creates monthly schedule, next run 01/06 |

## Running it

### Backend

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env       # optional — works without a Gemini key
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

API docs at <http://localhost:8000/docs>.

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>. The Vite dev server proxies `/api` and `/ws` to
`localhost:8000` (see `vite.config.ts`), so CORS is a non-issue locally.

### Optional: Gemini-backed NLU

Set `GEMINI_API_KEY` in `backend/.env`. The pipeline tries Gemini first and
falls back to rules on any failure, so the demo never breaks.

```env
GEMINI_API_KEY=ya29.xxx
GEMINI_MODEL=gemini-1.5-flash
```

## How a message flows

1. `POST /api/chat` (or WS frame on `/ws/chat`) hits `services/orchestrator.handle_message`.
2. **NLU**: `nlp.pipeline.understand` → tries Gemini, falls back to rules. Returns
   `intent`, `entities` (recipient surface form, amount in VND, temporal ref, cron).
3. **Context resolution**:
   - `context.alias.resolve_recipient` maps "mẹ" / "Minh" / "anh Minh" to contact(s).
   - `context.temporal.resolve_temporal_reference` picks the past tx for
     "như tháng trước" so amount/description can be back-filled.
4. **Safety**: `safety.rules.evaluate` runs the slide's safety checklist —
   ambiguous recipient, missing info, new-recipient-large-amount,
   statistical anomaly (≥10× average), insufficient balance.
5. **Draft** stored in the user's short-term session
   (`context.session`). The orchestrator returns an `OmniResponse` with the
   text reply plus a structured `draft` (or `history`/`balance`/`schedule`).
6. **Confirm**: `POST /api/transactions/{draft_id}/confirm` runs the safety
   gate again, executes via `banking.service.execute_transfer`, and clears
   the session draft.

## Project structure

```
contest/
├── backend/
│   ├── app/
│   │   ├── main.py              ◀ FastAPI entry
│   │   ├── config.py            ◀ env / settings
│   │   ├── store.py             ◀ in-memory JSON-backed store
│   │   ├── models/schemas.py    ◀ Pydantic models (NLUResult, TransactionDraft, …)
│   │   ├── nlp/                 ◀ Layer 2 — NLU pipeline
│   │   ├── context/             ◀ Layer 3 — alias / temporal / session
│   │   ├── safety/              ◀ Layer 4 — rule engine
│   │   ├── banking/             ◀ Layer 5 — mock banking ops
│   │   ├── routes/              ◀ chat + banking REST + WS
│   │   ├── services/            ◀ orchestrator (the brain)
│   │   └── data/                ◀ seed JSON
│   ├── scripts/smoke.py         ◀ end-to-end demo runner
│   ├── requirements.txt
│   └── .env.example
└── frontend/
    ├── src/
    │   ├── App.tsx              ◀ phone-frame chat shell
    │   ├── api/client.ts        ◀ fetch wrapper
    │   ├── components/          ◀ Message · TransactionCard · DisambiguationCard ·
    │   │                          HistoryCard · BalanceCard · ScheduleCard · …
    │   ├── styles/app.css
    │   └── types.ts             ◀ TS mirror of backend Pydantic models
    ├── package.json
    └── vite.config.ts
```

## Tech notes

- **No Postgres/Redis/Pinecone required.** Everything runs from JSON seeds in
  process — the store API is narrow on purpose so it can be swapped out later.
- **No Gemini key required.** The rule-based pipeline handles all 6 scenarios
  alone. Gemini is *additive*: when configured, it provides richer NLU and the
  rules fill in any blanks (amount span detection is rule-only).
- **Step-up auth.** When safety flags include `new_recipient_large_amount` or
  `amount_above_average`, the confirm button morphs into "Xác minh OTP & xác
  nhận" — the OTP UI itself is out of scope for the MVP but the back end
  surfaces the `requires_step_up: true` flag for it.
- **Vietnamese NFC.** All regexes target precomposed Vietnamese codepoints
  (`ử` = U+1EED), with a diacritic-stripped alias path so users can also type
  "me" / "minh" / "nhu thang truoc".

# Omni — architecture deep dive

A more concrete companion to the layer table in `CLAUDE.md` and the boundary
table in `llm-vs-rule.md`. This walks one realistic message — `Chuyển cho mẹ
5 triệu như tháng trước` — through every layer, with file:line pointers.

## The 5 layers (recap)

```
   Chat UI  →  NLU  →  Context  →  Safety  →  Banking
   React       Groq    alias/temp  rule eng   mock core
               ↓ ↓     ↓ session   ↓ flags    ↓ tx + sched
               Gemini  ↓ RAG       ↓ step-up  ↓ recurring
               ↓
               rule fallback
```

Three things shipped beyond the slide brief:
- **ML** (`app/ml/`): suggester (RandomForest + rule + freq), insights
  (MoM/anomaly/subscription mining), amount predictor (median-from-history).
- **Embeddings** (`app/nlp/embeddings.py`): fastembed local ONNX, multilingual
  MiniLM 384-d. Used for fuzzy contact lookup and history-by-meaning.
- **Audit trail** (`app/models/schemas.py:AuditEvent` + `store.add_audit_event`)
  — every decision recorded with NLU source, flags, recipient, account,
  decision outcome. Defendable forensics, not just logs.

## End-to-end trace: "Chuyển cho mẹ 5 triệu như tháng trước"

### 1. Frontend → backend

User taps Send. `frontend/src/App.tsx:send` calls `api.chat(message)`
(`frontend/src/api/client.ts:chat`) → `POST /api/chat` with header
`x-user-id: u_an`.

### 2. Orchestrator entry

`backend/app/services/orchestrator.py:handle_message`:

1. Strips text, opens the user's `Session`.
2. **OTP rule check** — if active draft is `awaiting_otp` and message is
   pure digits, route to `confirm_draft`. (Not the case here.)
3. **Continuation rule** — if there's a current draft, check for
   `_CONFIRM_RE` / `_CANCEL_RE` match. (Not here — user is starting
   fresh.)
4. **NLU pipeline call** — `nlp.pipeline.understand(text, history=...)`.

### 3. NLU pipeline

`backend/app/nlp/pipeline.py:understand`:

1. **Try Groq first** (`nlp/llm.py:llm_understand`) — Llama 3.3 70B,
   OpenAI-compatible endpoint, ~1.5–2s latency.
2. **Try Gemini if Groq fails** — gemini-2.0-flash, OpenAI-compat shim.
3. **Rule fallback** if both fail — `nlp/intent.py:classify` (3-tier
   keyword) + `nlp/entities.py:extract` (regex extractors).

Output `NLUResult(intent="transfer", entities=ExtractedEntities(
  recipient_text="mẹ", amount=5_000_000, temporal_reference="như tháng trước"
), source="llm" | "rule")`.

Rule extractors run AFTER the LLM and fill any blanks the LLM missed —
`amount` parsing in particular is deterministic (`nlp/amount.py`), the LLM
is allowed to phrase but not to claim.

### 4. Modify-draft heuristic

If the user already had an open draft and the new message looks like an
edit (changes amount, description, recipient), route to `_modify_transfer_draft`
instead of creating a new draft. The detection is in `_looks_like_modification`
(`services/orchestrator.py:204` ish).

In this case there's no active draft → continue to intent dispatch.

### 5. Intent dispatch

`_dispatch_intent` routes by `nlu.intent`:

- `transfer` → `_handle_transfer`
- `balance` → `_handle_balance`
- `history` → `_handle_history`
- `schedule` → `_handle_schedule`
- `recurring` → `_handle_recurring`
- `insights` → `_handle_insights`
- `add_contact` → `_handle_add_contact`
- `smalltalk` → LLM phrasing with strict capability list
- `unknown` → deterministic Vietnamese fallback (NEVER LLM — risk of
  hallucinated facts)

### 6. `_handle_transfer` (the meat)

```python
contacts = store.contacts_of(user_id)
txs      = store.transactions_of(user_id)
account  = store.primary_account(user_id)
e        = nlu.entities
```

Steps:

1. **Resolve recipient** via `context.alias.resolve_recipient("mẹ", contacts)`:
   - exact alias match (`mẹ` → Nguyễn Thị Lan, `via_alias="mẹ"`)
   - or token match, prefix match, RAG semantic match
   - 5-step ladder defined in `context/alias.py`
   - returns `list[ResolvedRecipient]` so the orchestrator can disambiguate
2. **Filter by account hint** if user said "từ tài khoản Vietcombank" —
   `filter_by_account_hint`
3. **One candidate?** → `chosen = candidates[0].contact`
4. **Temporal back-fill** — `"như tháng trước"` triggers
   `context.temporal.resolve_temporal_reference(...)` which finds the past
   tx and fills `description` from it. Amount stays user-specified at 5tr
   (not overwritten with past 2tr — correctness check).
5. **Predict amount** if user omitted it — `ml.amount_predictor.predict_amount`
   returns `(amount, reason)` median-from-history. Marks
   `draft.predicted_amount = True` so the UI shows a chip.
6. **Safety evaluation** — `safety.rules.evaluate(amount, recipients,
   recipient, txs, account)`:
   - missing-info checks (amount/recipient)
   - ambiguous recipient (multiple Minh's)
   - new-recipient + large-amount flag
   - per-recipient median + MAD modified-z anomaly
   - insufficient balance
   - emits `list[SafetyFlag]` with `severity in {info, warn, block}`
7. **Step-up policy** — `safety.rules.requires_step_up(flags)` returns
   True if any warn/block-but-fixable flag fires. Sets
   `draft.requires_step_up = True` so the UI morphs the confirm button.
8. **Audit log** — `_record_audit(...)` writes an `AuditEvent` row even
   for the DRAFT (not just confirm) — every decision is auditable.

### 7. Response

`OmniResponse(intent="transfer", text="Tháng trước bạn gửi 2.000.000đ cho
Nguyễn Thị Lan…", draft=draft)` returns to FastAPI. Serialized via Pydantic
to JSON, returned over HTTP.

### 8. Frontend renders

`App.tsx:resolveOmni` attaches the response to the message in state. The
`Message` component sees `response.draft` and renders `<TransactionCard>`
with the structured fields. Buttons: Confirm, Cancel, change recipient.

### 9. Confirm

User clicks Confirm. `App.tsx:onConfirm` → `api.confirm(draftId, otp, …)` →
`POST /api/transactions/<id>/confirm`. `confirm_draft` in orchestrator
re-runs safety evaluation (defence-in-depth), then `execute_transfer` in
`banking/service.py`:

1. Deduct from `account.balance`
2. Append a `Transaction(status="completed")`
3. Append a `AuditEvent(decision="execute")`
4. Backfill the new tx's embedding (lazy, non-blocking)
5. Clear `session.current_draft`
6. Return `OmniResponse(intent="transfer", text="Đã chuyển 5.000.000đ cho
   Nguyễn Thị Lan (Vietcombank)…")` — this text is built by code, never by
   the LLM. **Safety contract.**

Frontend `App.tsx:sendDraftAction` notices `intent === "transfer"` and bumps
`suggestRefresh` so the next-recipient strip re-ranks with the just-paid
contact moved up or out.

## Where the LLM is NOT in this flow

Look for these on the trace — they're conspicuously absent:
- The transfer amount (5tr) — parsed by `nlp/amount.py` regex
- The recipient match (`mẹ → Lan`) — alias lookup, then RAG
- The past-month tx that filled `description` — SQL by category/contact_id
- The safety flags — `safety/rules.py` deterministic checks
- The confirmed-transfer success text — `_compose_transfer_text` formats
  from real DB values

The LLM only:
- Classified the intent (rule fallback covers if it 429s)
- Provided the surface form "mẹ" (rule extractor also catches this)
- Optionally rephrased the empathy line ("Tháng trước bạn gửi…") — even
  this is gated against the FACTS object so it can't invent numbers

## Data flow diagram

```
   user (Vietnamese chat input)
       ↓
   POST /api/chat               (routes/chat.py)
       ↓
   orchestrator.handle_message  (services/orchestrator.py)
       ├─ session.current_draft? → continuation paths
       │
       ↓
   nlp.pipeline.understand
       ├─ Groq → Gemini → rule
       └─ merge LLM + rule extractor → NLUResult
       ↓
   modify-draft heuristic? → _modify_transfer_draft
       ↓
   _dispatch_intent → _handle_transfer
       ├─ context.alias.resolve_recipient (exact/token/prefix/RAG)
       ├─ context.temporal.resolve_temporal_reference
       ├─ ml.amount_predictor.predict_amount (if amount missing)
       ├─ safety.rules.evaluate → list[SafetyFlag]
       ├─ safety.rules.requires_step_up → bool
       ├─ store.add_audit_event (AuditEvent for the draft)
       └─ Session.set_draft(TransactionDraft)
       ↓
   OmniResponse(intent, text, draft, …) → JSON → frontend
       ↓
   <TransactionCard> renders → user confirms / cancels / edits
       ↓
   POST /api/transactions/<id>/confirm
       ↓
   confirm_draft → safety re-eval → banking.service.execute_transfer
       ↓
   "Đã chuyển 5.000.000đ cho Nguyễn Thị Lan" (composed by code, not LLM)
```

## Where Redis / persistence will plug in

`backend/app/context/session.py` is in-process today. The `feat/redis-sessions`
branch (in flight) wraps it behind a `SessionStore` interface so swapping in
Redis is a 1-line config change (`OMNI_SESSION_BACKEND=redis`). Drafts get a
5-minute TTL, full sessions get 30 minutes, fakeredis fallback when Redis is
down so the demo doesn't crash. Once that lands, the orchestrator code does
not change.

## Audit log replay

`scripts/audit_replay.py` (to be added) reads the `audit_events` table and
shows the last N decisions with NLU source, flags, decision outcome. The
audit row is written at draft-time AND at confirm-time, so reviewers can
see every step where the safety layer fired even when no money moved.

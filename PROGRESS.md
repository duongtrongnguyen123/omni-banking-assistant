# Omni — what's shipped (read in 2 minutes)

A snapshot of what the **One Last Token** team has built for HACK\<CX\>TOGETHER,
intended for judges or anyone landing on the repo fresh.

## The pitch in 30 seconds

A Vietnamese natural-language banking assistant that collapses the classic
**7-step transfer flow → Chat → Confirm → Done**, with three Vietnamese-specific
differentiators (alias / temporal / ambiguity) and a hard **safety contract**
preventing the LLM from ever asserting money facts.

**27 features shipped · all quality gates green:** 386 pytest pass / 10 xfail
in 6.2s · 200/200 NLU corpus · 19/19 KB scenario assertions · 56 backend
routes across 22 prefixes · clean tsc + vite build (218 kB / 68 kB gzipped) ·
GitHub Actions CI green on every push.

**Headline empirical numbers (public datasets, pre-registered seed = 42):**
Suggester **Hit@1 = 0.81 / Hit@5 = 0.97** on BankSim 594k tx · Recurring
detector **F1 = 0.74** on Czech PKDD'99 real bank data · Fraud Isolation
Forest **recall 0.75 at FP-rate 0.11** on BankSim labelled fraud.

Pitch package: [`docs/pitch-final.md`](docs/pitch-final.md) (5-min script) ·
[`docs/pitch-deck-content.md`](docs/pitch-deck-content.md) (8-slide content) ·
[`docs/judge-faq.md`](docs/judge-faq.md) (20 Q&A) ·
[`docs/one-pager.md`](docs/one-pager.md).

Try it: `make backend` + `make frontend` → <http://localhost:5173>.

## What's actually shipped

### Core (load-bearing for the pitch)

- ✅ **5-layer architecture** as on slide 5 — Chat UI · NLU · Context · Safety · Banking
- ✅ **Multi-provider LLM** — Groq → Gemini → rule fallback. Demo never breaks on 429.
- ✅ **Vietnamese NLU** — diacritic-aware regex extractors, intent classifier, deterministic.
- ✅ **Alias resolution** — exact → token → prefix → RAG (fastembed local). "mẹ" → Lan.
- ✅ **Temporal back-fill** — "như tháng trước" → past tx description / amount.
- ✅ **Safety rule engine** — ambiguous recipient, new+large, per-recipient MAD anomaly, balance.
- ✅ **Multi-turn modify** — "đổi sang 3 triệu" edits the draft, doesn't spawn a new one.
- ✅ **OTP step-up** — flag-driven, mock code 123456.
- ✅ **LLM safety contract** — confirmed-transfer line built deterministically from real data, never LLM.

### Extensions beyond the slide brief

- ✅ **Vector + lexical RAG** for "tiêu cho ăn uống tháng trước" semantic history search
- ✅ **Tree-based next-recipient suggester** (sklearn RF + rule + freq prior)
- ✅ **Recurring payment detector** (month-bucket pattern miner over history)
- ✅ **Insights dashboard** — MoM, anomalies, subscription detection (sidebar card)
- ✅ **Amount predictor** — median fill when user omits amount (with "from history" chip)
- ✅ **Voice input** (Web Speech vi-VN) + **TTS replies** (opt-in toggle)
- ✅ **Suggestion strip** — top-5 next-recipient chips above input
- ✅ **Animated success state** — card flip + confetti on confirm
- ✅ **Repeat-last-transfer** one-tap CTA
- ✅ **Slash commands** — `/transfer`, `/balance`, `/history`, `/repeat`, `/insights`, `/help`
- ✅ **Keyboard shortcuts** — Cmd+K focus, Cmd+Enter resend, Cmd+/ palette, ↑/↓ history
- ✅ **@-mention recipient autocomplete** in chat input
- ✅ **Redis session backend** with fakeredis fallback + 5-min draft TTL
- ✅ **Real-time toast notifications** via `/ws/events` — 6 event kinds, per-user queue
- ✅ **Smart categorizer** — auto-categorize tx from VN description (P=0.95, <2ms)
- ✅ **Exports** — CSV / sao kê HTML / tax-year JSON
- ✅ **a11y** — WCAG 2.1 AA, focus rings, reduced-motion, sr-only live region
- ✅ **Demo resilience** — offline mode, telemetry overlay, recorder, canonical demo JSONL
- ✅ **Privacy mode** — `OMNI_PRIVACY_MODE={off,redact,local-only}`, 5 PII classes, LLM audit ring buffer
- ✅ **Budgets + savings goals** — 4 new intents, monthly envelopes, progress tracking
- ✅ **Metrics + Prometheus** — 7 metric series, `/api/metrics` exposition, live dashboard
- ✅ **Health probes + lifespan** — `/health/{live,ready,version}`, graceful shutdown, k8s hints
- ✅ **A/B + Thompson bandit** — 4 weight arms, online learning, winner tree_freq at 67.55%
- ✅ **ATM/branch finder** — 15-seed mock dataset, Haversine distance, NLU "ATM gần nhất"
- ✅ **Cross-user synth eval** — pre-registered protocol, in-dist 0.54 / RAW 0.00 / mapped 0.57
- ✅ **VietQR-style generator + camera scan** — custom TLV codec, jsQR as 47kB lazy chunk
- ✅ **Onboarding wizard** — 4-step tutorial overlay + Skills discovery (13 chips × 5 categories)
- ✅ **Structured `/help`** — `HelpCard.tsx` renders sections + keyboard shortcuts
- ✅ **Performance optimizations** — SQL composite index + `_RawTx __slots__`, 500× speedup transfer

### Quality gates (all green)

| Gate | What |
|------|------|
| `make check` | 19/19 assertions pass (KB scenarios, safety contract, injection containment, toast events) |
| `make test-nlu` | 200/200 NLU corpus pass (after honoring 12 xfail) |
| `make test` | 310+ tests pass — NLU corpus, multi-turn, Redis persistence, events, exports, categorizer, redactor, LLM audit, metrics, budgets, goals |
| `make smoke` | All 8 KB demo scenarios pass with LLMs deliberately disabled |
| `make verify` | Single-command pre-pitch gate (check + tests + build, ~45s warm) |
| `make reset` | Pitch-day panic button (wipes runtime DB, re-seeds, ~10s warm) |
| GitHub Actions CI | Import sanity + smoke + NLU + frontend build on every push |
| Frontend build | Clean tsc + vite, 218 kB JS / 68 kB gzipped |
| Backend routes | **56** across 22 prefixes (chat, transactions, schedules, contacts, history, suggestions, insights, recurring, exports, demo, budgets, goals, admin, metrics, health, atm, ws/chat, ws/events, …) |
| Backend pytest | **386 pass / 10 xfailed** in 6.2s |

### Honest empirical results

Evaluated on **three** datasets:

| Eval | Dataset | Headline |
|------|---------|----------|
| Suggester Hit@K | Contest 520k (uniform) | At random baseline — dataset has no learnable signal |
| Suggester Hit@K | **BankSim 594k (real merchant labels)** | **Hit@1 = 0.81 · Hit@5 = 0.97** — headline non-circular number |
| Suggester Hit@K | Cross-user check (20 synth users, seed=42, pre-registered) | In-dist 0.54 · cross-user RAW 0.00 (no label leakage) · mapped 0.57 |
| Recurring detector F1 | **Czech PKDD'99 (real bank, ground-truth `permanent_orders`)** | **F1 = 0.74** (P=0.69, R=0.80) |
| Fraud Isolation Forest | BankSim 7 200 labelled fraud | Recall 0.75 · FP-rate 0.11 at threshold 0.5 — OTP step-up signal |

Full method: [`docs/eval.md`](docs/eval.md), [`docs/eval-real-data.md`](docs/eval-real-data.md),
[`docs/eval-protocol.md`](docs/eval-protocol.md) (pre-registered seed + hyperparameters).

## What we explicitly do NOT claim

- "Our model predicts your next recipient with 89% accuracy" — that was the synthetic
  number where we encoded the pattern. The defensible number is **0.81** on
  BankSim merchant labels with no patterns we authored.
- "We catch fraud" — we flag statistical anomalies; recall 0.75 at threshold 0.5
  with 11% FP-rate on legit is an **OTP step-up signal**, not a fraud blocker.
- "Production-ready" — sessions are in-memory or Redis (optional), OTP is mocked
  (123456), no real bank rail.

Full crib sheet: [`docs/honest-pitch.md`](docs/honest-pitch.md).

## Pending / in flight (5 background agents still running)

- 📊 Performance benchmark at contest scale + hot-path optimisation
- 🤖 Per-user Isolation Forest fraud model (already evaluated, code being merged)
- 🔍 Audit log replay UI — per-decision "why" explainer
- 🔔 Real-time WS notification toasts (transfer success, schedule fire, anomaly)
- 🧪 Cross-user synthetic eval (proves patterns are user-specific, not memorised)

Two completed branches awaiting hand-merge (cross-cut conflicts with slash-commands):
- 🏦 Multi-account picker + biometric step-up + same-bank pill
- 🌐 Bilingual VI ↔ EN toggle (58 keys, 28 EN NLU tests)

## Where to look first

| Question | File |
|----------|------|
| "How does it actually work?" | [`docs/architecture.md`](docs/architecture.md) — end-to-end trace of one transfer |
| "Where's the LLM boundary?" | [`docs/llm-vs-rule.md`](docs/llm-vs-rule.md) |
| "How honest are your numbers?" | [`docs/honest-pitch.md`](docs/honest-pitch.md) |
| "How do I demo this live?" | [`docs/demo-script.md`](docs/demo-script.md) — 4-min flow with timing |
| "What's safe to merge into main?" | [`docs/branch-status.md`](docs/branch-status.md) |

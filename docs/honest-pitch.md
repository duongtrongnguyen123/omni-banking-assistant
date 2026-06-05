# Omni — honest pitch & defendable claims

Crib sheet for what we **can** vs **cannot** honestly claim in front of
judges. Designed so anyone on the team can answer "where's the proof?"
without overselling.

## What we can fully prove on contest data (520k tx)

| Claim | Evidence |
|---|---|
| **NLU handles Vietnamese variation** — diacritics, no-diacritics, typos, English mix | `backend/tests/test_nlu_corpus.py` — 200+ utterances, rule-pipeline only (LLM mocked) |
| **System scales** — P50 < 50ms, P95 < 250ms on 520k tx | `docs/perf.md` (bench agent) — full table at 520k contest scale |
| **Vector + lexical RAG works at 1000-contact scale** — fuzzy lookup like "anh đồng nghiệp marketing" | live demo against `omni_contest.db` |
| **Safety rule engine flags real risks** — z-score anomaly, new-recipient + large amount, ambiguous candidate | `backend/app/safety/rules.py` + unit tests |
| **Recurring detector is honest** — finds nothing on uniform contest data (no false positives), finds real patterns on seed data | run intent "có khoản nào trả đều" against both DBs |
| **Multi-turn modify/confirm** — pure session state machine, works on any data | demo: "chuyển mẹ 2tr" → "đổi 3tr" → "đổi nội dung" → confirm |
| **LLM safety contract** — confirmed-transfer lines NEVER come from the model | `docs/llm-vs-rule.md` boundary table + `nlp/llm.py:_PHRASE_SYSTEM` |

## What we built but cannot honestly prove value of

| Feature | Why we can't prove it | Honest framing |
|---|---|---|
| **Tree suggester (Hit@K)** | Contest data is uniform 1000-class (no learnable signal). Our synthetic seed encodes patterns then "learns" them back — circular. | "Infrastructure ready. Hit@5=0.89 demonstrated on synthetic patterns we authored — proves model *can* learn, doesn't prove deployment value. Real lift requires accumulated user data." |
| **Amount prediction from history** | Same circularity — works on patterns we put in. | "Median-based heuristic, ungated by ML. Useful for repeat payments at unchanged amounts. Surfaced as a *chip* the user can override, not auto-applied." |
| **Fraud Isolation Forest** | Synthetic fraud injection isn't fraud. | "Per-user anomaly scorer. On contest data the base FP rate is X%; we don't claim catching real fraud." |

## How to answer judge questions

**Q: "Your Hit@K on our data is basically zero. What's the model good for?"**
A: "Honest answer — your contest data is uniform per counterparty (461–663 tx each, no DOW/DOM concentration). That's a 1000-class uniform classification, and a tree model can't beat random there. We verified this with an inflection sweep (`docs/eval.md`): the model only beats the frequency baseline once the candidate pool drops to ~20, which is the realistic "Danh bạ" size. Our pitch is the infrastructure plus the **synthetic proof it learns when patterns exist** — not a deployable Hit@K claim today."

**Q: "Why should we believe the NLU works?"**
A: "Because we test it deterministically. 200+ Vietnamese utterances with mocked LLM in `tests/test_nlu_corpus.py` — pass rate is X%. No reliance on Groq/Gemini being up. The LLM is a *quality boost*, not a dependency."

**Q: "What's actually novel?"**
A: Three things, ranked by defensibility:
1. **LLM safety contract** — boundary where the model phrases empathy but never asserts money facts. Most chat-banking demos break this.
2. **Multi-turn modify** — judges can say "đổi sang 3 triệu" or "không, người kia" mid-confirmation and the system handles it as an edit, not a new transaction.
3. **Local-first RAG** — fastembed + SQLite, no cloud embedding API. The whole stack runs offline.

**Q: "Why not deep learning for the suggester?"**
A: "Because honesty about data matters. With your dataset showing no temporal signal, a deep model would memorize harder and still fail on hold-out. A tree + rule hybrid keeps reasoning interpretable — when it says "mẹ ranks high today", we can show why (day-of-month proximity, recency, frequency)."

## What NOT to say

- ❌ "Our model predicts your next recipient with 89% accuracy" — true only on data we authored
- ❌ "We catch fraud" — we flag statistical anomalies, not fraud
- ❌ "Production-ready" — sessions are in-memory, OTP is mocked, no real bank rail
- ❌ "Better than [competitor]" — we don't know their data

## The story arc for the 5-minute pitch

1. **Pain (15s)** — show the 7-step Smart Banking flow
2. **Promise (10s)** — "Chuyển mẹ 2 triệu" + Confirm → Done
3. **Three Vietnamese-specific challenges (90s)** — alias, temporal, ambiguity. Demo each.
4. **Safety wall (45s)** — anomaly flag + OTP step-up + LLM-never-writes-money-facts contract
5. **Scale claim (30s)** — 520k tx contest dataset, P95 latency table
6. **Honest limit (15s)** — "Suggester ML is infra ready; real lift needs real user data accumulation"
7. **Roadmap (15s)** — real OTP, persistence, multi-instance — all interface-clean

Total ~3:30, leaving room for demo overruns.

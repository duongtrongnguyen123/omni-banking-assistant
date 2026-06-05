# Branch status — wave 2 & 3 agents

Snapshot of which features are merged into `feat/omni-integrated` (the
team's "what's safe to demo" branch) vs sitting on standalone branches
waiting for hand-merge.

## Merged into main / feat/omni-integrated (16 features)

| Feature | Branch | Status |
|---------|--------|--------|
| Voice input (Web Speech vi-VN) | `feat/voice-input` | ✅ merged |
| Recurring patterns UI (card + prefill) | `feat/recurring-ui` | ✅ merged |
| Spending insights + MoM/anomaly card | `feat/insights` | ✅ merged |
| Smart amount prediction (median chip) | `feat/amount-predict` | ✅ merged |
| Eval Hit@K writeup + 16s eval refactor | `feat/eval-docs` | ✅ merged |
| Adversarial NLU corpus (200 cases) | `feat/nlu-corpus` | ✅ merged |
| Verifier audit + MAD anomaly + insights bug fix | `audit/2026-06-06` | ✅ merged |
| Demo polish (confetti, vi-VN TTS, repeat CTA) | `feat/demo-polish` | ✅ merged |
| Slash commands + keyboard shortcuts + @-autocomplete | `feat/slash-commands` | ✅ merged |
| Public-data eval (Czech F1=0.74, BankSim Hit@1=0.81, fraud R=0.75) | `feat/real-data-eval` | ✅ merged |
| Playwright E2E tests + CI workflow | `feat/e2e-playwright` | ✅ cherry-picked (tests + CI) |
| Redis session backend + draft TTL | `feat/redis-sessions` | ✅ merged |
| Toast notifications via /ws/events (6 kinds) | `feat/toast-events` | ✅ merged |
| Cross-user synth eval (in-dist 0.54, RAW 0.00, mapped 0.57) | `feat/synth-v2-eval` | ✅ merged |
| Demo resilience (offline mode + telemetry + recorder + canonical JSONL) | `feat/demo-resilience` | ✅ merged |
| Exports (CSV/HTML sao kê/tax-year JSON) | `feat/exports` | ✅ cherry-picked |
| a11y (WCAG 2.1 AA, reduced-motion, jest-axe) | `feat/a11y` | ✅ merged |

## Pending — needs careful hand-merge

These touch many of the same files (App.tsx, orchestrator.py, schemas.py)
and conflict with each other if auto-merged. Merge order matters:

| Feature | Branch | Conflicts with | Suggested order |
|---------|--------|----------------|-----------------|
| Multi-account picker + biometric step-up | `feat/multi-account` | Based off pre-wave-2 main → deletes 17k lines if naively merged | Surgical cherry-pick only when needed for demo |
| Bilingual VI ↔ EN toggle | `feat/i18n` | multi-account (App.tsx, entities.py, intent.py) | After multi-account |
| Audit replay UI + per-decision explainer | `feat/audit-explain` | Needs `Store.audit_of` + `AuditEvent.auth_required/auth_completed` (multi-account adds those) | After multi-account |
| Fraud Isolation Forest per-user | `feat/fraud-ml` | Adds `fraud_risk_high` to SafetyFlag literal, `evaluate(user_id=...)` signature change | After multi-account/audit-explain |

## Still running

| Agent | Branch (when done) | What |
|-------|--------------------|------|
| Performance bench + optimization | `feat/perf-bench` | P50/P95 across endpoints on 520k tx, tune top-3 hot paths |
| Smart receipt categorization | `feat/categorizer` | Auto-categorize tx from free-text description (TF-IDF + rules) |
| Budget envelope + savings goals | `feat/budgets-goals` | Monthly budgets per category + goal tracker |

## Quality gates

`make verify` is the green-light. Currently on main:
- 19/19 `make check` pass
- 200/200 NLU corpus + 10/11 multi-turn integration + 14/14 Redis sessions + 8/8 exports tests
- Backend imports clean (28 routes — `/api/audit/*` and `/api/budgets/*` pending)
- Frontend builds clean (204 kB JS / 64 kB gzipped)
- 8 KB scenarios pass end-to-end with LLMs deliberately disabled
- Demo canonical JSONL replays in ~5-7s at 800ms cadence

## Why we're not force-merging everything

The team that wins is the team that **demos without breaking**. Splitting
high-conflict branches lets us cherry-pick the safest combination for the
pitch. multi-account, audit-explain, fraud-ml, i18n are bonuses — main has
all 16 merged features covering every load-bearing slide-deck claim.

## "Make verify" recap

Single-command pre-pitch gate. Runs in ~45s warm:
1. `make check` (18 assertions: import sanity, seed completeness, suggester
   ready, KB scenarios route correctly under rule fallback, injection contained)
2. Backend pytest with LLMs deliberately disabled (NLU corpus + multi-turn
   integration + session persistence + events + exports)
3. Frontend `npm run build` (tsc + vite)

Halts on first red. CI runs the same three steps on every push.

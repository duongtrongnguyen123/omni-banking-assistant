# Branch status — wave 2 & 3 agents

Snapshot of which features are merged into `feat/omni-integrated` (the
team's "what's safe to demo" branch) vs sitting on standalone branches
waiting for hand-merge.

## Merged into main / feat/omni-integrated

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

## Pending — needs careful hand-merge

These touch many of the same files (App.tsx, orchestrator.py, schemas.py)
and conflict with each other if auto-merged. Merge order matters:

| Feature | Branch | Conflicts with | Suggested order |
|---------|--------|----------------|-----------------|
| Multi-account picker + biometric step-up | `feat/multi-account` | slash-commands (App.tsx, TransactionCard), schemas (Account.kind) | Merge first — adds the auth_required schema other features assume |
| Bilingual VI ↔ EN toggle | `feat/i18n` | multi-account (App.tsx, entities.py, intent.py) | Merge second — pulls translation strings around the new account UI |
| Audit replay UI + per-decision explainer | `feat/audit-explain` | Needs `Store.audit_of` + `AuditEvent.auth_required/auth_completed` (multi-account adds those). Cherry-picks orchestrator+main without those will break. | Merge AFTER multi-account |
| Fraud Isolation Forest per-user | `feat/fraud-ml` | Adds `fraud_risk_high` to SafetyFlag literal, `_fraud_model` startup hook, `evaluate(user_id=...)` signature change. Numbers: demo precision/recall=1.0/0.96; contest base-rate FP=33%. | Merge AFTER multi-account/audit-explain — touches safety/rules.py + orchestrator at 4 call sites |

### Merge plan

```
git checkout main
git merge feat/multi-account --no-ff
# Resolve App.tsx by hand: keep slash-commands' useKeyboard + RecipientAutocomplete,
# add multi-account's <AccountChips> row and <BiometricOverlay>.
# Resolve TransactionCard.tsx: bring multi-account's biometric panel in,
# preserve slash-commands' /repeat success animation hooks.
# Resolve schemas.py: keep both AccountKind enum and the existing Account fields.
# Resolve entities.py: keep both _SOURCE_BANK_RE and any new patterns.
make check    # must stay green
git push origin main:feat/omni-integrated

git merge feat/i18n --no-ff
# Resolve App.tsx by adding the language toggle next to the TTS toggle.
# Resolve QuickScenarios.tsx: translate the labels via useT().
# entities.py / intent.py: ensure EN keyword lists land alongside VI.
make check && make test-nlu
git push origin main:feat/omni-integrated
```

## Still running — wave 2/3

| Agent | Branch (when done) | What |
|-------|--------------------|------|
| Performance bench + optimization | `feat/perf-bench` | P50/P95 across endpoints on 520k tx, tune top-3 hot paths |
| Fraud Isolation Forest | `feat/fraud-ml` | Per-user anomaly model, `fraud_risk_high` flag, eval on BankSim labels |
| Playwright E2E | `feat/e2e-playwright` | 9 KB scenarios in Chromium, GitHub Actions workflow |
| Public dataset eval | `feat/real-data-eval` | Czech PKDD'99 + BankSim ingestion, honest recurring/fraud/Hit@K |
| Redis sessions + draft TTL | `feat/redis-sessions` | RedisSessionStore + fakeredis fallback, 5-min draft TTL |

## Quality gates

`make check` is the green-light. Currently:
- 18/18 checks pass on main
- 200 / 200 NLU corpus cases pass
- Backend imports clean (25 routes)
- Frontend builds clean (187 kB JS / 59 kB gzipped)
- 8 KB scenarios pass end-to-end with LLMs deliberately disabled

## Why we're not force-merging everything

The team that wins is the team that **demos without breaking**. Splitting
high-conflict branches lets us cherry-pick the safest combination for the
pitch. Multi-account and i18n are both finished — but together they touch
half the front-end. Better to merge deliberately than ship a smoking ruin.

The 9 already-merged features cover the slide deck's three differentiators
end-to-end. Multi-account and i18n are bonuses, not load-bearing.

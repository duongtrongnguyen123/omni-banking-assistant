# Release notes — 2026-06-06

What landed on `main` / `feat/omni-integrated` today, in roughly the order it
shipped. Use this for the team standup / mentor update / "what's new?" demo
walkthrough.

## Headline

**16 features merged in one day**, all behind the same green `make verify`
gate (19/19 checks + 224 backend tests + frontend build). Honest empirical
results now backed by **three real datasets** (Czech PKDD'99, BankSim,
20-user synthetic with pre-registered protocol) rather than one circular
self-authored seed.

## Features (by merge order)

### NLU / safety / model layer
- **Adversarial NLU corpus** — 200 Vietnamese utterances, 8 categories, rule
  pipeline only (LLMs forced off). 100 % pass after honoring 12 `xfail`s.
- **Verifier audit** — full system pass: 4/4 build, 6/7 E2E (one stale-LLM
  miss), 4/4 safety, 4/4 frontend, 3 critical issues identified and fixed.
- **MAD anomaly** — per-recipient median + modified-z replaces global × mean.
  Resistant to one-shot fat-tail transfers ("100M to mẹ for property deposit"
  no longer poisons later checks).
- **Insights bug fix** — `Store.contacts.get(id)` → `Store.get_contact(id)`.
  `/api/insights/summary` was 500'ing on demo seed; now returns 6 MoM rows
  + 3 subscriptions.

### UX / chat
- **Voice input** — `webkitSpeechRecognition` vi-VN, hides itself in Firefox.
- **Vietnamese TTS** — opt-in toggle, picks `vi-VN` voice from
  `speechSynthesis.getVoices()`.
- **Recurring patterns UI** — structured `RecurringList` card with
  "Đặt lịch tự động" prefill.
- **Spending insights card** — MoM deltas, per-recipient anomalies,
  subscription detection. Sidebar widget.
- **Amount predictor** — median-from-history fill when user omits amount,
  chip says "đề xuất từ lịch sử".
- **Demo polish** — confetti burst on confirm, animated success state,
  4 s auto-collapse to receipt, repeat-last-transfer pill.
- **Slash commands** — `/transfer`, `/balance`, `/history`, `/repeat`,
  `/insights`, `/help`, `/lang`, `/clear` with palette UI.
- **Keyboard shortcuts** — Cmd+K, Cmd+Enter, Cmd+/, Cmd+B, ↑/↓ recall.
- **@-mention recipient autocomplete** in chat input.
- **Real-time WS event toasts** — 6 kinds (`transfer_success`,
  `transfer_failed`, `schedule_fired`, `recurring_detected`, `balance_low`,
  `anomaly_warning`). Per-user `asyncio.Queue` (no cross-user leak).
- **Demo resilience** — `OMNI_OFFLINE_DEMO=1` env, `<TelemetryOverlay>`
  behind `?dev=1`, `<DemoRecorder>` behind `?demo=1`, canonical demo JSONL.
- **Exports** — `/api/export/transactions.csv`, `/api/export/sao-ke.html`,
  `/api/export/tax-year.json` + `<ExportMenu>` header dropdown.
- **WCAG 2.1 AA a11y** — focus ring, sr-only live region, role="log" chat,
  reduced-motion overrides, high-contrast overrides, `--muted` bumped from
  `#6b6e8a` to `#585b78` so 12.5 px helper text passes 4.5:1.

### Infrastructure / scale / persistence
- **Redis session backend** — `OMNI_SESSION_BACKEND={memory,redis,fake-redis}`,
  5-min draft TTL, 30-min session TTL, graceful in-memory fallback when
  Redis dies. fakeredis backend for tests, docker-compose for real Redis.
- **GitHub Actions CI** — import sanity + NLU corpus + smoke + frontend
  build on every push.
- **Backend Dockerfile** — non-root user, healthcheck wired to `/health`,
  build-time route-count check.

### Honest empirical results
- **Public-data eval** — Czech PKDD'99 recurring detector F1 = **0.74**
  vs ground-truth `permanent_orders`. BankSim suggester Hit@1 = **0.81**
  (real merchant labels, non-circular). BankSim fraud Isolation Forest
  recall = **0.75** at threshold 0.5 with FP-rate = 0.11.
- **Cross-user synth eval** — 20 users, seed = 42, pre-registered protocol.
  In-distribution Hit@1 = 0.54; cross-user RAW = 0.00 (proves no global label
  leakage). Documented in `docs/eval-protocol.md`.
- **Eval refactor** — 333k-row DELETE/INSERT replaced with in-memory
  `train_for(txs=...)`. Total eval wall-time 16 s on 520k contest rows.

### Quality tooling
- **`make verify`** — single-command pre-pitch gate: check + tests + build.
- **`make check`** — 19/19 assertions in ~5 s.
- **`make reset`** — pitch-day panic button. Wipes runtime DB, re-seeds,
  re-trains suggester, verifies 6 KB scenarios. ~10 s warm.
- **`make docker-build` / `docker-run` / `docker-redis`** — portable image.
- **`backend/scripts/list_routes.py`** — print every FastAPI route grouped
  by prefix. Currently: 33 routes across 18 prefixes.

## Pending hand-merge (4 branches, parked deliberately)

- `feat/multi-account` — 5 500+ line delta because branched off pre-wave-2
  main. Surgical cherry-pick only when needed for demo.
- `feat/audit-explain` — needs `Store.audit_of` and `AuditEvent.auth_*`
  fields that multi-account adds. Merge order: multi-account → audit.
- `feat/fraud-ml` — Isolation Forest per-user, adds `fraud_risk_high`
  flag. Numbers: contest FP-rate 33 % (honest about that limit).
- `feat/i18n` — VI ↔ EN toggle, 58 keys × 2 langs. Merge after multi-account.

## Numbers in one line each

| Surface | Number |
|---------|--------|
| Backend routes | 33 |
| Frontend JS bundle | 204 kB (64 kB gzipped) |
| `make check` | 19 / 19 green |
| Backend pytest | 224 pass / 10 xfailed |
| NLU corpus accuracy | 100 % (after xfail) |
| BankSim suggester Hit@1 | 0.81 |
| Czech recurring detector F1 | 0.74 |
| BankSim fraud IF recall (@0.5) | 0.75 |
| `make verify` wall time | ~45 s warm |
| `make reset` wall time | ~10 s warm |
| Canonical demo replay | ~5–7 s |

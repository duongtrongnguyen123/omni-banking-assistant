# Offline demo mode

Pitch day, no wifi. This document explains the `OMNI_OFFLINE_DEMO=1`
survival switch so anyone on the team can flip it from the command line
seconds before opening the laptop.

## What it does

A single env var short-circuits every outbound network dependency the
backend has:

| Subsystem | Default | With `OMNI_OFFLINE_DEMO=1` |
|-----------|---------|-----------------------------|
| Groq / Gemini LLM | Tried for NLU + phrasing | `_enabled_providers()` returns `[]`. Rule extractor handles NLU; deterministic templates handle phrasing. |
| fastembed model download | Lazy fetch on first embed | Skipped — `OMNI_SKIP_EMBED_BACKFILL=1` is set transitively. |
| Schedule ticker | 60s background loop | Disabled — `OMNI_DISABLE_SCHEDULE_TICK=1` is set transitively. |
| `/api/insights/summary` | Real aggregation over SQLite | If the aggregation raises, fall back to `_CANNED_SUMMARY` in `routes/insights.py`. Real path still runs first. |
| WebSocket `/ws/events` | Unchanged — local only | Unchanged. |
| Chat HTTP / WS endpoints | Unchanged | Unchanged. |

The frontend is untouched. It still talks to `localhost:8000` as usual.

## How to flip it

```bash
# Backend, fresh terminal:
cd backend
OMNI_OFFLINE_DEMO=1 .venv/bin/python -m uvicorn app.main:app --port 8000
```

Or, add `OMNI_OFFLINE_DEMO=1` to `backend/.env` if you want it sticky
between restarts.

Verify with `curl localhost:8000/health` — the response includes
`"offline_demo": true` when the switch is on.

## Failure modes the offline path handles

These are the three most common ways the demo dies in front of judges,
and how offline mode insulates each one:

1. **Hotel wifi blocks the LLM provider but lets through DNS.** The
   request goes out, the TLS handshake hangs, then 20-second timeout
   kills it. With offline mode, the LLM is never tried — every turn
   runs the rule extractor and lands in <50ms.
2. **fastembed model isn't cached and needs to download ~120MB.** First
   `/api/chat` after a clean clone blocks the whole event loop. Offline
   mode skips the backfill at startup, and the embedding-dependent paths
   degrade gracefully (alias resolver falls back to lexical match).
3. **`/api/insights/summary` raises because a row is malformed in a
   contest data subset.** Default behaviour: HTTP 500 → ugly red toast.
   Offline mode: log the exception, return `_CANNED_SUMMARY` so the
   sidebar still renders.

## What still works (and what doesn't)

Works:
- All 6 knowledge bases the demo script covers (KB1–KB6).
- Safety rule flags (deterministic, no network).
- Suggester (`/api/suggestions/recipients`) — trained at startup, in-process.
- Recurring detection (`/api/chat` with "khoản nào định kỳ").
- Confirm / Cancel / OTP flow.
- WebSocket `/ws/chat` and `/ws/events` (loopback only).

Doesn't work:
- LLM phrasing — answers fall back to the deterministic templates.
  They're more telegraphic but cite the same numbers.
- "Như tháng trước" temporal references are still resolved via the
  rule path; the LLM contextual inheritance is unavailable.
- New fastembed downloads. If the model is cached (`~/.cache/fastembed`)
  it works fine; if not, embedding paths silently no-op.

## Tests

`backend/tests/test_demo_recorder.py::test_offline_demo_setting_skips_llm_providers`
locks in the contract: when `OMNI_OFFLINE_DEMO=1`, the LLM provider
list is empty even if `GROQ_API_KEY` is populated.

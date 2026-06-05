# Session persistence

How the in-flight transaction / contact / schedule drafts and the
short-term conversation history get stored. This is the piece the
hackathon audit flagged as the #1 production gap: with the original
implementation a `uvicorn --reload` mid-OTP would drop the user's
draft on the floor and they'd have to start over.

## Why it matters

The orchestrator's `Chat → Confirm → Done` loop spans 2-3 HTTP
requests:

1. user message → recipient + amount draft, `awaiting_otp` flag set
2. user types OTP → backend resolves the draft, confirms the transfer
3. backend replies "Đã chuyển 5.000.000đ cho mẹ"

If the process restarts between (1) and (2) the draft is gone. Worse,
in a multi-worker uvicorn the second request might land on a worker
that never saw the draft at all. Both classes of failure disappear
once the draft lives in a shared, durable store with a short TTL.

## Architecture

```
orchestrator.py
      │
      ▼
  session_for(user_id) ──▶ Session  ──▶ SessionBackend (abstract)
                                          ├── InMemorySessionStore   (default)
                                          ├── RedisSessionStore      (prod)
                                          └── FakeRedisSessionStore  (tests / no-docker demo)
```

* `Session` (in `backend/app/context/session.py`) is the per-user
  facade. Its API surface is the same as the historical
  `ConversationMemory` — `set_draft`, `current_draft`,
  `set_schedule_draft`, `append(role, content)`,
  `conversation_messages(...)`, etc. The orchestrator was *not*
  touched.
* `SessionBackend` (in `backend/app/context/session_store.py`)
  defines the storage contract. Drafts go in/out as JSON via
  Pydantic's `model_dump_json` / `model_validate_json`. History
  is a list of `{role, content, ts}` dicts.
* Backend selection is driven by env var, evaluated lazily on first
  use:

  | `OMNI_SESSION_BACKEND` | Backend                  | Notes                                 |
  | ---------------------- | ------------------------ | ------------------------------------- |
  | unset / `memory`       | `InMemorySessionStore`   | Current behaviour, CI default          |
  | `redis`                | `RedisSessionStore`      | Real Redis via `redis-py`              |
  | `fake-redis`           | `FakeRedisSessionStore`  | `fakeredis` in-process, judge-friendly |

## TTL defaults

Two knobs, two different jobs:

| Variable             | Default       | Justification                                                                                                                                                                                                                |
| -------------------- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `OMNI_DRAFT_TTL_S`   | 300 (5 min)   | A draft is something the user is mid-confirming. If they walk away mid-OTP, we *want* the next interaction to start clean rather than confirm an old amount. 5 min is long enough for a slow phone-paste of an OTP.          |
| `OMNI_SESSION_TTL_S` | 1800 (30 min) | Whole-session key TTL — bounds conversation history retention so the in-context window stays fresh and Redis memory doesn't grow unbounded. A 30-min coffee break shouldn't reset your chat.                                  |
| `OMNI_HISTORY_MAX`   | 20            | Hard cap on stored messages per user. Bounds payload size for the LLM context and Redis memory per session.                                                                                                                  |

Every `set_draft` resets the key's `EXPIRE`, so an active conversation
stays alive indefinitely as long as something happens within the TTL.

## Why `fakeredis` instead of just in-memory for tests?

The two backends have slightly different code paths:

* `InMemorySessionStore` stores Pydantic objects directly in a
  Python dict.
* `RedisSessionStore` round-trips them through `model_dump_json`
  and `model_validate_json` — that's where serialization bugs hide
  (datetime ISO formatting, `Optional[Contact]`, `Decimal`-like
  amounts).

`FakeRedisSessionStore` exercises the *Redis* path (HSET / HGET /
EXPIRE / JSON) without needing a real server, so we catch
serialization regressions in CI. Same wire protocol, same TTL
semantics, zero infra.

## Failure-mode matrix

| Scenario                                       | What happens                                                                                                                                                                | User-visible effect                              |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| `OMNI_SESSION_BACKEND=memory` (default)        | Drafts live in this process. Lost on restart. Multi-worker uvicorn is unsafe.                                                                                               | Demo works, single process only.                  |
| `OMNI_SESSION_BACKEND=redis`, Redis healthy    | All state in Redis. Restart-safe, multi-worker safe.                                                                                                                       | Drafts survive restart, OTP flow always resumes. |
| `OMNI_SESSION_BACKEND=redis`, Redis down at boot| `RedisSessionStore()` constructor pings; ping raises; bootstrap logs `Could not connect to Redis (...) — using in-memory` and returns `InMemorySessionStore`.              | Demo never crashes. Loses persistence silently.   |
| `OMNI_SESSION_BACKEND=redis`, Redis dies mid-flow | First failed `HSET` throws; `_RedisBackedStore._demote()` flips `_dead = True`, logs once, creates an in-memory shadow. Subsequent calls in this process are no-ops.        | Current request finishes normally with no error. |
| `OMNI_SESSION_BACKEND=fake-redis`              | `fakeredis.FakeRedis` in-process. Behaves exactly like a healthy Redis, no docker required.                                                                                | Same as healthy Redis, but state is process-local. |
| `OMNI_SESSION_BACKEND=potato` (typo)           | Bootstrap logs `Unknown OMNI_SESSION_BACKEND=...` and returns `InMemorySessionStore`.                                                                                       | Demo works, single process only.                  |

## Running with real Redis

```bash
docker compose up -d redis           # repo-root docker-compose.yml
export OMNI_SESSION_BACKEND=redis
export OMNI_REDIS_URL=redis://localhost:6379/0   # optional, this is the default
cd backend && .venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

To prove persistence, do an "Đặt lịch chuyển mẹ 2tr mùng 1 hàng tháng",
get to the OTP step, `Ctrl-C` uvicorn, restart it, then type `123456` —
the schedule still gets created.

## Demo without docker (`fakeredis`)

```bash
export OMNI_SESSION_BACKEND=fake-redis
```

Same code path as real Redis (JSON serialization, TTL semantics, hash
fields) but completely in-process. Useful when showing judges how
persistence is wired without spinning up containers. Note: `fakeredis`
state is per-process so it won't survive a uvicorn restart — for that
you need real Redis.

## Tests

`backend/tests/test_session_persistence.py` covers:

* draft round-trip through both `InMemorySessionStore` and
  `FakeRedisSessionStore`
* draft TTL expiry on both backends
* fallback to in-memory when `OMNI_SESSION_BACKEND=redis` but the URL
  is unroutable
* mid-flow demotion when Redis writes start failing
* conversation history append + truncation at `OMNI_HISTORY_MAX`
* role mapping for `conversation_messages` (LLM-compatible payload)

Run them with:

```bash
cd backend && .venv/bin/pytest tests/test_session_persistence.py -v
```

# Error handling

How Omni handles failures end-to-end — from a pydantic validation error
to a red toast in the phone frame. This is the trust contract the
"final polish" pass committed to. If you change any of the layers
below, update this doc.

## Error UX hierarchy

```
┌────────────────────────────────────────────────────────────────┐
│ 1. Validation                                                  │
│    pydantic v2 → FastAPI 422  →  custom handler → 400 Vietnamese
│    (app/main.py: _friendly_validation_error)                   │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│ 2. Frontend coalescing                                          │
│    fetch reject → ApiError(status=0,   detail=Mất kết nối…)     │
│    !res.ok      → ApiError(status=N,   detail=server text)      │
│    (frontend/src/api/client.ts: jsonFetch + friendlyApiError)   │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│ 3. Frontend toast + inline message                              │
│    failOmni() updates the in-chat bubble AND dispatches a       │
│    `omni:toast` CustomEvent → <ToastStack /> renders red toast. │
│    (frontend/src/App.tsx: failOmni)                             │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│ 4. Backend logging                                              │
│    Uncaught exceptions → counted by lifecycle.mark_request_     │
│    dropped() → exposed via /api/metrics + shutdown summary.     │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│ 5. Render-time error boundary                                   │
│    <ErrorBoundary /> wraps <App /> at the root.                 │
│    "Có lỗi xảy ra — bạn thử tải lại trang nhé" + Reload button. │
│    (frontend/src/components/ErrorBoundary.tsx)                  │
└────────────────────────────────────────────────────────────────┘
```

## Vietnamese error vocabulary (canonical)

| When | What user sees |
|------|----------------|
| Empty `POST /api/chat` body | `Bạn nhập tin nhắn rồi gửi lại nhé` (400) |
| Any other validation failure | `Yêu cầu thiếu thông tin — bạn nhập lại nhé` (400) |
| Server returns 5xx with body | the server's `detail` string (already in VN) |
| Server returns 5xx with no body | `Mạng tạm trục trặc — thử lại nhé` |
| `fetch()` rejected (offline / DNS) | `Mất kết nối — kiểm tra mạng nhé` |
| Rate-limit hit (429) | `Bạn gửi hơi nhanh — chờ chút rồi thử lại nhé` |
| Unhandled render error | `Có lỗi xảy ra` headline + `Tải lại trang` button |

## Rate-limit policy

`POST /api/chat` is rate-limited **per `x-user-id` header** using a
stdlib in-process token bucket.

- **Default**: 60 requests / minute / user.
- **Override**: `OMNI_CHAT_RATE_LIMIT` env var (integer, requests per
  minute). `0` disables the limiter entirely (test mode).
- **Algorithm**: classic token bucket — capacity = `OMNI_CHAT_RATE_LIMIT`,
  refill rate = `capacity / 60` tokens per second. Each request consumes
  one token; if the bucket is empty we return **429** with a
  `Retry-After` header set to the number of seconds until the next
  whole token becomes available.
- **Response body**: `{"detail": "Bạn gửi hơi nhanh — chờ chút rồi thử lại nhé"}`.
- **Scope**: only `/api/chat`. Confirm / cancel / select endpoints are
  *not* rate-limited — they're triggered by user clicks on visible
  cards, not by raw input, so abuse there is bounded by the UI.

Tests live in `backend/tests/test_chat_route.py` (added in this pass).

Implementation: `backend/app/routes/_ratelimit.py`. Multi-instance
deployments should replace the in-process bucket with the Redis
session backend (`OMNI_SESSION_BACKEND=redis`) — see
`docs/persistence.md` for the migration plan.

## Admin auth model

See `docs/admin-auth.md` for the full bearer-token contract. Short
version:

| `OMNI_ADMIN_TOKEN` env | `/api/admin/*` behaviour |
|------------------------|--------------------------|
| unset (default) | open access — demo mode |
| set, request matches | 200 |
| set, request mismatches | 401 with `Token không hợp lệ` |
| set, no `Authorization` header | 401 with `Thiếu Authorization Bearer token` |

Comparison is constant-time. Applies to every route in the
`admin.router` and to the standalone `POST /api/admin/embed`.

## Telemetry hooks

The orchestrator's `?dev=1` mode populates `OmniResponse.telemetry`
with per-stage latency. Failed requests *do not* populate telemetry
(the bucket only lives for successful trips) — frontend dev tools see
the failure via the toast, not via the telemetry overlay. This is
intentional: the overlay should show the happy-path waterfall, not the
exception.

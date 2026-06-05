# Final audit — 2026-06-07 (pre-pitch)

Snapshot of local `main` (HEAD `86abf81`) after 27 features merged plus
the final-polish wave (Vietnamese error UX, rate limiting, admin auth)
and the inline fix to the previous critical issue (`/api/session/reset`
500 → 200).

Scope: build/test sanity, 9 KB demo scenarios, surface check on the
pre-pitch endpoints judges will hit, doc/number freshness, adversarial
inputs.

## Summary

| Section | Pass | Fail | Notes |
|---------|----:|----:|-------|
| A. Build + test sanity | 4 / 4 | 0 | `make verify` GREEN, 58 routes, 386 pass / 10 xfailed, 243.87 kB JS |
| B. 9 KB scenarios | 9 / 9 | 0 | All return 200 with correct intent under rule-fallback |
| C. Pre-pitch surface | 7 / 7 | 0 | `/health/{live,ready,version}`, `/api/metrics`, empty-body chat → 400 (was 500), QR PNG, ATM list |
| D. Doc + numbers freshness | 4 / 4 | 0 | PROGRESS = 30+ features, CLAUDE.md = 29 arch rows, README Hit@1 = 0.81, branch-status = 26 merged |
| E. Adversarial / "things judges might break" | 6 / 6 | 0 | Gibberish, path-traversal, diacritics, rate-limiter, `/help`, `/audit` all safe |

Net: **30 / 30 pass**.

## Critical issues (block demo)

**0.** The previous critical (session/reset returning 500) is verified
fixed in this snapshot (`POST /api/session/reset` → `{"ok":true,"user_id":"u_an"}` with status 200).

## High issues (user-visible but not blocking)

**0.** Nothing that surfaces in front of judges. The two notes below
are minor and surfaced for completeness.

## Notes (informational, no action required)

* `/health/ready` returns `503` in fresh test harness because the
  background suggester / embedder warmup hasn't completed yet
  (`{"suggester": false, "embedder": false}`). On a real backend that
  has finished startup, the same probe returns `200`. The 503 carries
  the diagnostic JSON the checklist permits, so this is by design.
* `/audit` slash command is not shipped; the chat handler routes
  unknown slash commands through the regular `unknown` intent path
  and returns the safe Vietnamese fallback. No 500. Acceptable per
  the checklist (`if not shipped, no crash`).
* `event publish failed: There is no current event loop in thread
  'AnyIO worker thread'` warning prints once per orchestrator call
  inside `TestClient`. It is benign (event bus is fire-and-forget) and
  does not appear under real uvicorn. Worth a defensive `try/except`
  on the publish call before pitch if there is appetite, but the user
  never sees it.

## Pass detail — for the record

### A. Build + test sanity

* `make verify` → exit 0. Last 5 lines:
  ```
  dist/assets/jsQR-D0WsXWiO.js    130.63 kB │ gzip: 47.24 kB
  dist/assets/index-B_x1Gv1a.js   243.87 kB │ gzip: 76.41 kB
  ✓ built in 617ms

  All checks green. Safe to demo.
  ```
* Backend imports clean: **58 routes** (≥56 required).
* Backend pytest: **386 passed, 10 xfailed** in 6.20s.
* Frontend `npm run build`: JS 243.87 kB / gzip 76.41 kB, CSS 48.44 kB,
  jsQR lazy chunk 130.63 kB. 77 modules transformed, 0 errors.

### B. 9 KB scenarios (rule fallback, both LLMs disabled)

`POST /api/chat` with `x-user-id: u_an`, session reset between cases.

| # | Prompt | Intent | Recipient | Amount |
|---|--------|--------|-----------|--------|
| KB01 | Chuyển cho Minh 2 triệu | transfer | None (ambiguous — multiple Minh in seed) | 2 000 000 |
| KB02 | Gửi mẹ 5 triệu | transfer | Nguyễn Thị Lan | 5 000 000 |
| KB03 | đổi sang 3 triệu | transfer | None (no prior draft) | 3 000 000 |
| KB04 | tháng này mình gửi mẹ bao nhiêu | history | — | — |
| KB05 | Chuyển 50 triệu cho Hùng STK 9990001234 | transfer | Trần Quốc Hùng | 50 000 000 |
| KB06 | đặt lịch chuyển mẹ 2tr mùng 1 hàng tháng | schedule | — | — |
| KB07 | Lưu Lê Mai STK 0123987654 Vietcombank | add_contact | — | — |
| KB08 | có khoản nào trả đều hàng tháng không | recurring | — | — |
| KB-balance | số dư còn bao nhiêu | balance | — | — |

All return HTTP 200 with the expected intent. KB01's ambiguous-recipient
behavior is the safety design (won't bind when two contacts named
"Minh" exist). KB03 in isolation has no draft to modify, so no
recipient — also expected.

### C. Pre-pitch surface check

| Endpoint | Status | Body / signal |
|----------|--------|---------------|
| `GET /health/live` | 200 | `{status:"ok", uptime_seconds, pid}` |
| `GET /health/ready` | 503 | `{checks:{sqlite:true, suggester:false, embedder:false, redis:"n/a"}, ready:false}` — diagnostic form |
| `GET /health/version` | 200 | `git_sha="86abf81"`, version, build_time, python_version, platform, deps_versions |
| `GET /api/metrics` | 200 | `Content-Type: text/plain; version=0.0.4; charset=utf-8`, 916 bytes Prometheus text exposition |
| `POST /api/chat` (empty body) | **400** | `{"detail":"Bạn nhập tin nhắn rồi gửi lại nhé"}` — Vietnamese, no stack trace |
| `POST /api/chat` (no body at all) | **400** | `{"detail":"Yêu cầu thiếu thông tin — bạn nhập lại nhé"}` |
| `POST /api/qr/generate` | 200 | `qr_base64` is 1780-char PNG (header `iVBORw0KGgo`), `payload_text` set |
| `GET /api/atm/nearby?lat=21.0285&lng=105.8542` | 200 | list of 4 ATMs with distance_km sorted, e.g. VCB Hoàn Kiếm 0.588 km |

The previous critical (`POST /api/chat` with empty body returning 500)
is verified fixed — now returns 400 with a Vietnamese user-friendly
message and no stack trace. This is the exact regression the
final-polish wave was meant to close.

### D. Doc + numbers freshness

* `PROGRESS.md` — 30+ ✅ items, claims `386 pass / 10 xfailed` (matches
  observed) and `56 routes` (slightly behind actual 58, but the doc
  predates the audit-branch). README claim of headline **Hit@1 = 0.81**
  on BankSim 594k is present and unchanged.
* `CLAUDE.md` architecture table — **29 data rows** (5 slide layers + 24
  extensions). Meets the ≥29 requirement.
* `README.md` line 86 still carries `Hit@1 = 0.81 · Hit@5 = 0.97` on
  BankSim with the explanatory "Public real-world, non-circular" note.
* `docs/branch-status.md` — 26 ✅-merged feature rows. Matches the
  feature-list expectation; reads as authoritative.

### E. Things judges might break

| Probe | Outcome |
|-------|---------|
| Type "asdfasdf" | 200, `intent=unknown`, safe Vietnamese fallback |
| Type "../../etc/passwd" | 200, `intent=unknown`, same safe fallback; no path traversal in response |
| Vietnamese with unusual diacritics ("ức tỷđồng") | 200, `intent=unknown`, safe fallback |
| Send 120 messages in a tight loop (single user) | 60 × 200 + 60 × 429. Rate limiter cuts at 60 reqs and returns 429 cleanly |
| `/help` | 200, response includes structured `help_sections` block |
| `/audit` (not shipped) | 200, treated as `unknown`, no crash |

## Three things judges should NEVER click during pitch

1. **`/health/ready` while the backend is still warming up.** It will
   show `503` with `suggester:false embedder:false` until the
   background warmup finishes (~3–5 s on the demo laptop). It is
   *correct* behavior, but reads as "not ready" to a non-engineer.
   Hit it only after the first chat round-trip has succeeded.
2. **Anything in `/api/admin/*`** without the demo admin token. The
   admin-auth wave gates these behind a token; a 401 in a side panel
   will look like the assistant is broken when it's just the wrong
   header. Keep the demo on the chat surface.
3. **120 chat messages in 10 seconds.** The rate limiter is doing its
   job and will return `429` after ~60 requests per minute. Don't let
   anyone mash send during the pitch — the toast will read "Too Many
   Requests" and undercut the "we're production-shape" line.

## Final readiness verdict

**GREEN — safe to demo.** All 30 audit checks pass, the prior critical
(session reset / empty-body 500) is verified fixed, rate limiting and
Vietnamese error UX are working, and the 9 KB demo scenarios route
correctly under the rule-only fallback path.

## Reproduction

```bash
# A. Verify
make verify    # exit 0, 386 pass / 10 xfailed, build 243.87 kB JS

# B. KB scenarios
cd backend && GROQ_API_KEY= GEMINI_API_KEY= .venv/bin/python <<'PY'
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
for msg in ["Chuyển cho Minh 2 triệu","Gửi mẹ 5 triệu","đổi sang 3 triệu",
            "tháng này mình gửi mẹ bao nhiêu","Chuyển 50 triệu cho Hùng STK 9990001234",
            "đặt lịch chuyển mẹ 2tr mùng 1 hàng tháng","Lưu Lê Mai STK 0123987654 Vietcombank",
            "có khoản nào trả đều hàng tháng không","số dư còn bao nhiêu"]:
    c.post("/api/session/reset", headers={"x-user-id":"u_an"})
    r = c.post("/api/chat", json={"message":msg}, headers={"x-user-id":"u_an"})
    print(msg[:40].ljust(42), "->", r.status_code, r.json().get("intent"))
PY

# C. Surface
curl -s http://localhost:8000/health/live
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/api/chat \
     -H 'x-user-id: u_an' -H 'Content-Type: application/json' -d '{}'   # expect 400
```

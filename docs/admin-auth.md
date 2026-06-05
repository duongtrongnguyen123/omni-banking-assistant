# Admin auth model

Every route under `/api/admin/*` is gated by a single shared-secret
bearer token. There is no per-user RBAC in the demo build — the
hackathon scope assumes one operator wielding the same key.

## Routes covered

```
GET    /api/admin/privacy-mode
POST   /api/admin/privacy-mode
GET    /api/admin/llm-audit
GET    /api/admin/abtest/report
POST   /api/admin/abtest/reset
POST   /api/admin/embed
```

## Behavior

Set the env var `OMNI_ADMIN_TOKEN`. The dependency in
`backend/app/routes/admin.py:require_admin` then enforces it on every
admin route via FastAPI's `dependencies=[Depends(require_admin)]`
router-level hook.

| `OMNI_ADMIN_TOKEN` env | request | response |
|------------------------|---------|----------|
| unset | any | 200 (demo mode — documented "feature") |
| set | `Authorization: Bearer <correct-token>` | 200 |
| set | `Authorization: Bearer <wrong-token>` | 401 `Token không hợp lệ` |
| set | missing `Authorization` header | 401 `Thiếu Authorization Bearer token` |
| set | `Authorization: Basic …` | 401 `Thiếu Authorization Bearer token` |

The token comparison is constant-time (XOR loop) to avoid timing
oracles. Token length must match exactly before the loop runs — that's
the only short-circuit.

## Demo-mode rationale

We default to *open* admin routes when the env var is unset because:

1. The hackathon judge experience needs to inspect privacy-mode and
   A/B-arm reports without setting up a shell session.
2. The honest pitch (`docs/honest-pitch.md`) calls out auth as
   intentionally minimal — adding it gratis is misleading.
3. `OMNI_ADMIN_TOKEN` is one env var away from production-grade.

## How to enable

```bash
export OMNI_ADMIN_TOKEN="$(openssl rand -hex 32)"
make backend
# then:
curl -H "Authorization: Bearer $OMNI_ADMIN_TOKEN" \
     http://localhost:8000/api/admin/privacy-mode
```

The frontend does not call any `/api/admin/*` route — the badge in
the header reads `/health` which exposes `privacy_mode` already. So
enabling the token does not break the UI; it only locks down the
operator endpoints.

## What's NOT covered

- `/api/admin/embed` is registered on the FastAPI `app` directly, not
  via the admin router, so it has its own `Depends(require_admin)`
  declaration in `app/main.py`.
- `POST /api/session/reset` is *not* an admin endpoint — it clears the
  caller's own session and is therefore harmless to leave open.
- Health probes (`/health`, `/health/live`, `/health/ready`,
  `/health/version`) are intentionally open. k8s probes can't carry
  bearer tokens reliably.

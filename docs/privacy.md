# Privacy mode + outbound LLM payload audit

Omni is a banking assistant — every chat turn is potentially high-trust.
Two of the three NLU providers we ship (Groq Llama 3.3 70B, Gemini 2.0
Flash) are **third-party cloud LLMs** reached over HTTPS. That means
*by default* the user's chat message and conversation history leave
the host. For a hackathon demo that's fine; for an on-prem rollout the
operator needs a switch.

`OMNI_PRIVACY_MODE` is that switch.

## Three modes

| Mode | What happens to outbound LLM calls | When to use it |
|------|-----------------------------------|----------------|
| `off` (default) | Sent verbatim. Audit buffer still records `(provider, original_size)` so you can see *how often* the LLM is being asked, but the content is not rewritten. | Hackathon demo, dev work, anything where the operator has accepted the LLM provider's data-handling terms. |
| `redact` | Every user-supplied string (current message + every prior turn's `content`) goes through `app.nlp.redactor.redact()` before it leaves the process. Audit records the redaction count and per-class breakdown (`ACCT`, `AMOUNT`, `PHONE`, `EMAIL`, `NAME`). | Sensitive production traffic where the LLM is still useful but PII shouldn't cross the boundary. |
| `local-only` | LLM providers are hard-disabled. The NLU pipeline falls through to the rule-based extractor in `app.nlp.intent` + `app.nlp.entities`. The audit buffer logs `suppressed=True` so a judge can verify nothing left the device. | Air-gapped environments, dataset-collection runs, compliance reviews. |

The mode is exposed at both `/health` (read-only) and
`/api/admin/privacy-mode` (GET to read, POST to set at runtime — no
restart required).

## What gets redacted (`redact` mode)

The redactor is `app/nlp/redactor.py` — stdlib only, no spaCy, no
`phonenumbers`, no network. Strict mode: false positives (over-redaction)
are preferred to false negatives.

| Pattern | Examples | Token |
|---------|----------|-------|
| **Account numbers** | `9990001234`, `0123 4567 8901`, `STK của tôi không phải 1234567` | `[ACCT]` |
| **VND amounts** | `5tr500`, `2 triệu rưỡi`, `500k`, `1.000.000đ`, `VND 1,200,000` | `[AMOUNT]` |
| **Vietnamese phones** | `0912345678`, `+84 912 345 678`, `0987.654.321` | `[PHONE]` |
| **Email** | `nam.nguyen@example.com` | `[EMAIL]` |
| **Vietnamese names** | `Nguyễn Văn Minh`, `Vũ Quốc Bảo`, `chị Mai`, `anh Đức` | `[NAME]` |
| Bank names | `MB Bank`, `Vietcombank`, `VCB`, `Techcombank` | *preserved* — not PII |
| Categories | `ăn uống`, `cafe`, `xăng xe`, `tháng này` | *preserved* — needed for phrasing |

### Adversarial cases the redactor handles

- **Denied ownership**: `"STK của tôi không phải 1234567 đâu"` — the
  redactor masks `1234567` anyway. From the LLM's vantage point there's
  no way to tell whether a digit run is a real or fictional account
  number, so we mask every run unconditionally.
- **Mixed amount formats**: `"5tr500"` (suffix-glued), `"2 triệu rưỡi"`
  (multi-token with "half"), and `"1.000.000đ"` (currency-suffixed)
  all collapse to `[AMOUNT]`. The pattern alternation lists `triệu`
  before `tr` so the longer multiplier wins.
- **False positives on banking nouns**: `"Số điện thoại"` would naively
  look like a Title Case name (capital S, capital "Đ"). The Vietnamese
  uppercase character class is curated explicitly to exclude `đ` /
  `ă` / etc. lowercase codepoints, which Python's standard `[À-Ỹ]`
  range accidentally includes.

### Run the tests

```bash
backend/.venv/bin/python -m pytest backend/tests/test_redactor.py -v
```

20 sample sentences covering every PII class, an adversarial negation
test, a bank-name preservation test, and a 0.5ms-per-call performance
ceiling.

## The audit ring buffer

Every outbound LLM call appends one entry to a process-local FIFO ring
buffer (max 100 entries). The buffer survives across modes and is
inspectable at `/api/admin/llm-audit`.

```jsonc
{
  "mode": "redact",
  "capacity": 100,
  "count": 3,
  "entries": [
    {
      "seq": 12,
      "ts": 1735000000.42,
      "provider": "groq",
      "mode": "redact",
      "original_size": 184,
      "redacted_size": 162,
      "redaction_count": 3,
      "redaction_breakdown": { "ACCT": 1, "AMOUNT": 1, "NAME": 1, "EMAIL": 0, "PHONE": 0 },
      "suppressed": false,
      "note": null
    }
  ]
}
```

Notable fields:

- `seq` — strictly monotonic, lets a polling client diff between calls
  without trusting the wall clock.
- `original_size` / `redacted_size` — measure the **user-side payload**
  (current message + every history turn's content). The system prompt
  is excluded because it's our string, not the user's.
- `suppressed=true` — set when `local-only` mode blocked the call
  outright. The orchestrator silently falls back to the rule extractor.
- `redaction_breakdown` — populated only in `redact` mode. Empty when
  `off`, all-zero plus `suppressed=true` when `local-only`.

## Trust model — what a judge can verify

1. Set `OMNI_PRIVACY_MODE=local-only`, hit `/api/chat` with a sentence
   full of PII, then `GET /api/admin/llm-audit`. The single entry
   should have `suppressed=true` and `original_size>0` — proving the
   call was intercepted before any bytes hit the wire.
2. Set `OMNI_PRIVACY_MODE=redact`, send the same sentence, then poll
   the audit endpoint. The entry's `redaction_breakdown` will show
   non-zero counts in the matching classes; `redacted_size` will be
   smaller than `original_size`.
3. Set `OMNI_PRIVACY_MODE=off` and confirm `redaction_breakdown` is
   empty — the default behaviour is unchanged from the baseline.

The frontend surfaces the active mode in the header via a small
`PrivacyBadge` component. It hides itself when mode is `off`, and
renders a lock icon plus a Vietnamese label with a tooltip when mode
is `redact` (`chế độ bảo mật cao`) or `local-only` (`chế độ riêng tư`).

## Constraints / what this does NOT do

- The audit buffer is **in-process**. Restarting the server clears it.
  Production would push entries to a real log sink (Loki, CloudWatch,
  etc.) — `record_llm_call()` is the obvious hook point.
- The redactor is intentionally permissive on digit runs. We mask `0`
  through `9` for any contiguous 6–19 character span — yes, this
  includes things that look like dates if you squint. That's the cost
  of strict mode; if a numeric category needs to survive the redactor
  it should be tagged before the digits hit it.
- LLM **responses** are not audited or redacted. The phrasing prompt
  in `nlp/llm.py:_PHRASE_SYSTEM` already constrains the model to only
  cite numbers from `FACTS`; the redactor's job is the inbound side.
- This file does not document a way to disable the redactor for
  specific intents — every outbound call goes through the same code
  path. Hooking a per-intent allowlist would be straightforward but
  isn't shipped.

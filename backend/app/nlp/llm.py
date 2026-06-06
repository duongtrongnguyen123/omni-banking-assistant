"""LLM-backed NLU and response phrasing.

Provider abstraction: every supported provider exposes an OpenAI-compatible
chat-completions endpoint, so a single `_openai_compat` call handles all of
them. Providers are tried in priority order — if the primary returns 429 or
fails, the orchestrator silently falls through to the next.

Supported (set the matching env var to enable):
  - Groq      (gsk_…)    — fast, free tier with per-model TPD limits
  - Gemini    (AQ.…)     — Google AI Studio, OpenAI-compatible at v1beta/openai
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from ..config import get_settings
from ..models.schemas import ExtractedEntities, NLUResult
from . import privacy
from .redactor import redact

log = logging.getLogger("omni.nlu.llm")


@dataclass
class _Provider:
    name: str
    url: str
    api_key: str
    model: str


def _collect_keys(prefix: str, primary: str) -> list[str]:
    """Pool collector — ``GROQ_API_KEY`` plus ``GROQ_API_KEY_1..N``.

    Why a numbered pool: the existing 429-fallback in ``_call_llm`` walks
    providers in order. If we register each key as its own provider, a
    rate-limited key gets skipped automatically and the next one takes
    over. No code path change — just more entries in the chain.

    Reads from ``os.environ`` directly so it picks up keys loaded via
    ``--env-file`` / dotenv even when the settings dataclass doesn't
    expose them as typed fields. De-duplicates and drops empties.
    """
    seen: set[str] = set()
    out: list[str] = []
    if primary:
        out.append(primary)
        seen.add(primary)
    # Look for GROQ_API_KEY_1, _2, … up to a generous ceiling. Stops at
    # the first 5-in-a-row miss so we don't iterate 1..999 for nothing.
    misses = 0
    for n in range(1, 200):
        v = os.environ.get(f"{prefix}_{n}", "").strip()
        if not v:
            misses += 1
            if misses >= 5 and n > 5:
                break
            continue
        misses = 0
        if v not in seen:
            out.append(v)
            seen.add(v)
    return out


# Round-robin cursor over the Groq key pool. The 429-fallback walks the
# provider list in order, so without rotation every request hammers key #1
# first — burning its per-day token budget while 35 others sit idle, and
# wasting a 429 round-trip on it once it's exhausted. Advancing a start
# offset each call spreads load across the whole pool. Module-global +
# lock so concurrent requests don't fight over it.
_rr_cursor = 0
_rr_lock = threading.Lock()


def _rotate(keys: list[str]) -> list[str]:
    """Return ``keys`` rotated by a per-call offset so the pool is used
    round-robin rather than always starting at index 0."""
    global _rr_cursor
    if len(keys) <= 1:
        return keys
    with _rr_lock:
        offset = _rr_cursor % len(keys)
        _rr_cursor = (_rr_cursor + 1) % len(keys)
    return keys[offset:] + keys[:offset]


def _enabled_providers() -> list[_Provider]:
    """Priority order: Groq pool (1..N keys) → Gemini pool → done.

    Each API key registers as its own provider entry so the existing
    429 fallback in ``_call_llm`` walks through the pool transparently.
    Burn a key → next one takes over without restart. The Groq pool is
    rotated round-robin per call (see :func:`_rotate`) so load spreads
    evenly instead of always starting at key #1.

    Offline-demo and privacy local-only modes return [] so the rule
    extractor handles every request.
    """
    s = get_settings()
    if s.offline_demo:
        log.debug("offline_demo=1 — skipping LLM providers")
        return []
    if privacy.get_mode() == "local-only":
        log.debug("privacy_mode=local-only — skipping LLM providers")
        return []
    out: list[_Provider] = []
    groq_keys = _rotate(_collect_keys("GROQ_API_KEY", s.groq_api_key))
    for i, key in enumerate(groq_keys):
        out.append(
            _Provider(
                f"groq#{i + 1}" if len(groq_keys) > 1 else "groq",
                "https://api.groq.com/openai/v1/chat/completions",
                key,
                s.groq_model,
            )
        )
    gemini_keys = _collect_keys("GEMINI_API_KEY", s.gemini_api_key)
    for i, key in enumerate(gemini_keys):
        out.append(
            _Provider(
                f"gemini#{i + 1}" if len(gemini_keys) > 1 else "gemini",
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                key,
                s.gemini_model,
            )
        )
    if len(out) > 2:
        log.info("LLM provider pool: %d entries (Groq %d, Gemini %d)",
                 len(out), len(groq_keys), len(gemini_keys))
    return out


# ---------------------------------------------------------------------------
# NLU
# ---------------------------------------------------------------------------

_NLU_SYSTEM = """You are the NLU layer of a Vietnamese banking assistant called Omni.
Return STRICT JSON only, no prose.

Schema:
{
  "intent": "transfer|balance|history|schedule|recurring|reminder|add_contact|atm_finder|smalltalk|unknown",
  "confidence": 0..1,
  "entities": {
    "recipient_text": string|null,     // person's name or alias e.g. "mẹ", "Minh"
    "amount": integer|null,            // VND, integer (no separators)
    "amount_text": string|null,        // raw span e.g., "5 triệu"
    "description": string|null,        // e.g., "Tiền sinh hoạt"
    "temporal_reference": string|null, // e.g., "như tháng trước"
    "account_hint": string|null,       // digits — partial OR full account number
    "schedule_cron": string|null,      // cron expression if recurring
    "bank_name": string|null,          // e.g., "MB Bank", "Vietcombank", "VCB"
    "alias": string|null               // pet name to save under, e.g. "anh Nam"
  }
}

Rules:
- "k" = ×1,000;  "tr"/"triệu" = ×1,000,000;  "tỷ"/"ty" = ×1,000,000,000.
- "rưỡi" after a unit = + 0.5 of that unit ("5 triệu rưỡi" → 5500000).
- "đặt lịch / hàng tháng / mỗi tháng / mùng X" → schedule (CREATE).
- "khoản nào định kỳ / khoản tự động / có khoản nào trả đều / liệt kê lịch
   tự động" → recurring (READ — show patterns inferred from history).
- "nhắc nợ / nhắc trả" → reminder.
- RECIPIENT — emit `recipient_text` ONLY when the user explicitly named
  a recipient surface form (a name, nickname, alias, "mẹ"/"sếp", "Minh",
  "anh Tuấn", "grabfood", "bạn thân"). Copy the EXACT surface the user
  typed. Do NOT invent, paraphrase, or substitute — if the user says
  "bạn thân", do NOT output "Bố"/"Lê Văn Hùng" even if context suggests
  it. The contact-resolver downstream handles fuzzy matching; the LLM's
  job is to surface what the user said, not to guess who they meant.
  When the user did NOT name a recipient at all (e.g. "chuyển 2 triệu"),
  emit recipient_text=null.
- "ATM gần nhất / cây ATM / tìm ATM <bank>" → atm_finder; put the bank
  name (if mentioned) in entities.atm_bank.
- FOLLOW-UPS: when the current message looks like a continuation
  ("còn tháng trước?", "đổi sang Minh", "mà thôi đổi sang X", "cái nào nhiều
   nhất?", "vậy còn ...?"), INHERIT unmentioned fields from the previous turn
  rather than emitting null. Especially: inherit intent (history stays history,
  transfer stays transfer) and recipient.

Examples:
INPUT: "Gửi cho mẹ 5 triệu như tháng trước"
{"intent":"transfer","confidence":0.95,"entities":{"recipient_text":"mẹ","amount":5000000,"amount_text":"5 triệu","temporal_reference":"như tháng trước"}}

INPUT: "Chuyển cho Minh 500k"
{"intent":"transfer","confidence":0.9,"entities":{"recipient_text":"Minh","amount":500000,"amount_text":"500k"}}

INPUT: "Tháng này mình gửi mẹ bao nhiêu rồi?"
{"intent":"history","confidence":0.9,"entities":{"recipient_text":"mẹ"}}

INPUT: "Tháng này mình tiêu bao nhiêu rồi?"
{"intent":"history","confidence":0.9,"entities":{}}

INPUT: "Số dư còn bao nhiêu?"
{"intent":"balance","confidence":0.95,"entities":{}}

NOTE: "tiêu/chi/đã gửi/đã chuyển bao nhiêu" → history (spending question).
"Số dư/còn bao nhiêu trong tài khoản" → balance.

HISTORY-SPECIFIC EXAMPLES (cover specific month, all-time, limit, top-N,
description fuzzy filter):

INPUT: "Tháng 4 mình gửi mẹ bao nhiêu?"
{"intent":"history","confidence":0.95,"entities":{"recipient_text":"mẹ","specific_month":4}}

INPUT: "Tháng 11 năm ngoái mình đã chi bao nhiêu?"
{"intent":"history","confidence":0.9,"entities":{"specific_month":11,"specific_year":2025}}

INPUT: "Tổng cộng từ trước đến giờ mình gửi mẹ bao nhiêu?"
{"intent":"history","confidence":0.95,"entities":{"recipient_text":"mẹ","all_time":true}}

INPUT: "Cho mình xem 5 giao dịch gần nhất"
{"intent":"history","confidence":0.95,"entities":{"limit":5}}

INPUT: "Lần cuối mình gửi mẹ là bao nhiêu?"
{"intent":"history","confidence":0.95,"entities":{"recipient_text":"mẹ","limit":1}}

INPUT: "3 giao dịch gần nhất với chị Thảo"
{"intent":"history","confidence":0.95,"entities":{"recipient_text":"chị Thảo","limit":3}}

INPUT: "Tháng trước ai nhận nhiều tiền nhất từ tôi?"
{"intent":"history","confidence":0.95,"entities":{"temporal_reference":"tháng trước","top_recipient":true}}

INPUT: "Tháng này chủ đề nào tôi tiêu nhiều nhất?"
{"intent":"history","confidence":0.9,"entities":{"top_category":true}}

INPUT: "Tôi đã tiêu cho ăn uống bao nhiêu tháng trước?"
{"intent":"history","confidence":0.95,"entities":{"temporal_reference":"tháng trước","semantic_filter":"ăn uống"}}

INPUT: "Khoản chi nào liên quan đến sức khoẻ"
{"intent":"history","confidence":0.9,"entities":{"semantic_filter":"sức khoẻ"}}

INPUT: "Đặt lịch chuyển mẹ 2tr vào mùng 1 hàng tháng"
{"intent":"schedule","confidence":0.95,"entities":{"recipient_text":"mẹ","amount":2000000,"amount_text":"2tr","schedule_cron":"0 9 1 * *"}}

RECURRING (read-only — detect patterns from history):

INPUT: "Mình có khoản nào trả đều hàng tháng không?"
{"intent":"recurring","confidence":0.95,"entities":{}}

INPUT: "Liệt kê các khoản định kỳ của mình"
{"intent":"recurring","confidence":0.95,"entities":{}}

INPUT: "Có khoản nào tự động trả không?"
{"intent":"recurring","confidence":0.9,"entities":{}}

INPUT: "Khoản định kỳ với mẹ"
{"intent":"recurring","confidence":0.9,"entities":{"recipient_text":"mẹ"}}

INPUT: "Lưu Nam STK 9990001234 MB Bank tên là anh Nam"
{"intent":"add_contact","confidence":0.95,"entities":{"recipient_text":"Nam","account_hint":"9990001234","bank_name":"MB Bank","alias":"anh Nam"}}

FOLLOW-UPS (assume conversation history shows the prior turn):

PRIOR: history "mẹ" tháng này. INPUT: "Còn tháng trước?"
{"intent":"history","confidence":0.9,"entities":{"recipient_text":"mẹ","temporal_reference":"tháng trước"}}

PRIOR: transfer draft to mẹ 5tr. INPUT: "Đổi sang 2 triệu thôi"
{"intent":"transfer","confidence":0.95,"entities":{"amount":2000000,"amount_text":"2 triệu"}}

PRIOR: transfer draft to mẹ 1tr. INPUT: "Mà thôi, đổi sang chị Thảo đi"
{"intent":"transfer","confidence":0.9,"entities":{"recipient_text":"chị Thảo"}}

PRIOR: balance with both accounts. INPUT: "Còn tài khoản phụ thì sao?"
{"intent":"balance","confidence":0.9,"entities":{"account_hint":"phụ"}}

PRIOR: history by category. INPUT: "Cái nào nhiều nhất?"
{"intent":"history","confidence":0.9,"entities":{}}

INPUT: "Tháng này mình tiêu vào những chủ đề nào?"
{"intent":"history","confidence":0.9,"entities":{}}
"""


def llm_understand(
    text: str, history: Optional[list[dict]] = None
) -> Optional[NLUResult]:
    providers = _enabled_providers()
    if not providers and privacy.get_mode() == "local-only":
        # Make the suppression visible in the audit log so a judge can verify
        # nothing went out. ``original_size`` reflects the message we would
        # have sent had providers been allowed.
        privacy.record_llm_call(
            provider="(none)",
            mode="local-only",
            original_size=len(text or ""),
            redacted_size=0,
            redaction_count=0,
            suppressed=True,
            note="llm_understand suppressed by local-only mode",
        )
        return None
    for p in providers:
        data = _openai_compat(
            provider=p,
            system_prompt=_NLU_SYSTEM,
            history=history,
            user_message=text,
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=300,
        )
        if data is None:
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        return NLUResult(
            intent=obj.get("intent") or "unknown",
            confidence=float(obj.get("confidence", 0.7)),
            entities=ExtractedEntities(**(obj.get("entities") or {})),
            raw_text=text,
        )
    return None


# ---------------------------------------------------------------------------
# Draft-action interpreter — what does the user want to do with the
# transfer they are currently reviewing? LLM-first so novel phrasings work
# ("khoan, đổi người nhận thành sếp", "gấp đôi lên", "chuyển hết số dư"),
# with the rule-based continuation as the offline fallback.
# ---------------------------------------------------------------------------

_DRAFT_ACTION_SYSTEM = """You interpret what a Vietnamese banking user wants to do
with the PENDING money transfer they are currently reviewing on screen.

You are given the pending transfer (current recipient, current amount, source
balance) and the user's latest message. Return STRICT JSON only:

{
  "action": "confirm" | "cancel" | "otp" | "edit" | "restart" | "unclear" | "other",
  "otp_code": string|null,
  "recipient_text": string|null,
  "amount_vnd": integer|null,
  "amount_op": "add"|"subtract"|"multiply"|"fraction"|"all_balance"|null,
  "amount_operand": number|null,
  "account_hint": string|null
}

Decide the action:
- "confirm": user approves AS-IS — "xác nhận", "đồng ý", "ok", "được rồi", "chuyển đi", "ừ".
- "cancel": user calls the whole thing off WITHOUT naming a new recipient or amount —
  "huỷ", "thôi", "thôi khỏi", "đừng chuyển nữa", "bỏ đi", "không làm nữa".
- "otp": the message is just a 4-6 digit code → put it in otp_code.
- "edit": user CORRECTS the pending transfer — keep the fields they didn't touch.
  A correction has a cue: "đổi sang…", "à (thôi)…", "không, …", "mà thôi…", "sửa…",
  "thay bằng…", "ý tôi là…", or changes only the amount/account.
- "restart": user issues a FRESH, standalone transfer command ("chuyển cho mẹ",
  "gửi sếp 2 triệu") with NO correction cue — they're starting over, so the pending
  transfer's amount must NOT carry. Use this for a plain "chuyển/gửi cho X [số tiền]"
  that reads like a brand-new request, especially when NO amount is given.
- "unclear": user is talking about the transfer but you cannot tell what they want —
  ask them. Do NOT guess.
- "other": the message is NOT about this transfer at all (e.g. "số dư bao nhiêu?",
  "tháng trước tiêu gì?", smalltalk) → let the normal flow handle it.

For "edit", fill ONLY the fields the user actually changed THIS message:
- New recipient → recipient_text = the EXACT surface they used ("bố", "chú Ba",
  "sếp", "Minh", "dì Tư"). Copy it verbatim; do NOT resolve or guess who it is.
- Absolute amount they stated ("3 triệu", "500k", "2tr5", "1 tỷ") → amount_vnd in VND
  (k=1e3, tr/triệu=1e6, tỷ=1e9; "rưỡi"=+0.5 unit).
- Relative amount change:
    "thêm/cộng 500k"      → amount_op="add",      amount_operand=500000
    "bớt/trừ/giảm 200k"   → amount_op="subtract", amount_operand=200000
    "gấp đôi / x2 / nhân 2"→ amount_op="multiply", amount_operand=2
    "gấp ba"              → amount_op="multiply", amount_operand=3
    "một nửa / giảm nửa / phân nửa" → amount_op="fraction", amount_operand=0.5
    "chuyển hết / tất cả / toàn bộ số dư" → amount_op="all_balance"
- Source account ("dùng tài khoản phụ", "từ tài khoản chính", "tài khoản tiết kiệm",
  last 4 digits) → account_hint.

CRITICAL RULES:
- NEVER invent an amount or recipient the user did not mention in THIS message.
  If they mentioned none, leave those fields null. Do not copy values from the
  pending transfer or from history.
- A leading discourse particle ("thôi", "à", "à mà thôi", "khoan", "không", "ý tôi là")
  FOLLOWED BY a real instruction is an EDIT or redirect, NOT a cancel:
    "thôi chuyển cho bố"            → edit, recipient_text="bố"
    "khoan, đổi người nhận thành sếp" → edit, recipient_text="sếp"
    "không, ý tôi là dì Tư"          → edit, recipient_text="dì Tư"
  Only treat it as cancel when NOTHING actionable follows.

Examples (PENDING shown for context):
PENDING recipient=mẹ amount=2.000.000đ. MSG: "đổi sang chị Thảo 3 triệu"
{"action":"edit","recipient_text":"chị Thảo","amount_vnd":3000000}
PENDING recipient=mẹ amount=2.000.000đ. MSG: "gấp đôi lên đi"
{"action":"edit","amount_op":"multiply","amount_operand":2}
PENDING recipient=mẹ amount=2.000.000đ. MSG: "bớt còn một nửa thôi"
{"action":"edit","amount_op":"fraction","amount_operand":0.5}
PENDING recipient=mẹ amount=2.000.000đ. MSG: "chuyển hết số dư cho mẹ luôn"
{"action":"edit","amount_op":"all_balance"}
PENDING recipient=mẹ amount=2.000.000đ. MSG: "à thôi chuyển cho bố"
{"action":"edit","recipient_text":"bố"}
PENDING recipient=Cường amount=4.000.000đ. MSG: "chuyển cho mẹ"
{"action":"restart","recipient_text":"mẹ"}
PENDING recipient=Cường amount=4.000.000đ. MSG: "gửi sếp 2 triệu"
{"action":"restart","recipient_text":"sếp","amount_vnd":2000000}
PENDING recipient=mẹ amount=2.000.000đ. MSG: "dùng tài khoản phụ nhé"
{"action":"edit","account_hint":"phụ"}
PENDING recipient=mẹ amount=2.000.000đ. MSG: "thôi không chuyển nữa"
{"action":"cancel"}
PENDING recipient=mẹ amount=2.000.000đ. MSG: "xác nhận"
{"action":"confirm"}
PENDING recipient=mẹ amount=12.000.000đ. MSG: "123456"
{"action":"otp","otp_code":"123456"}
PENDING recipient=mẹ amount=2.000.000đ. MSG: "số dư còn bao nhiêu?"
{"action":"other"}
PENDING recipient=mẹ amount=2.000.000đ. MSG: "ờ cái kia"
{"action":"unclear"}
"""


def llm_draft_action(
    text: str, draft_ctx: dict, history: Optional[list[dict]] = None
) -> Optional[dict]:
    """Interpret the user's message against the pending transfer.

    ``draft_ctx`` carries human-readable ``recipient`` / ``amount`` /
    ``balance`` strings for the prompt. Returns the parsed decision dict, or
    ``None`` when no LLM provider is reachable (caller then falls back to the
    deterministic rule-based continuation)."""
    providers = _enabled_providers()
    if not providers:
        return None
    user_content = (
        "GIAO DỊCH ĐANG CHỜ XÁC NHẬN:\n"
        f"- Người nhận hiện tại: {draft_ctx.get('recipient')}\n"
        f"- Số tiền hiện tại: {draft_ctx.get('amount')}\n"
        f"- Số dư tài khoản nguồn: {draft_ctx.get('balance')}\n\n"
        f'CÂU NGƯỜI DÙNG VỪA NÓI: "{text}"'
    )
    for p in providers:
        data = _openai_compat(
            provider=p,
            system_prompt=_DRAFT_ACTION_SYSTEM,
            history=history,
            user_message=user_content,
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=200,
        )
        if data is None:
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("action"):
            return obj
    return None


# ---------------------------------------------------------------------------
# Response phrasing
# ---------------------------------------------------------------------------

_PHRASE_SYSTEM = """Bạn là Omni — trợ lý ngân hàng tiếng Việt thân thiện.

Bạn sẽ nhận:
- Lịch sử hội thoại (có thể có)
- USER_QUESTION: câu hỏi mới nhất của khách
- FACTS: JSON với dữ liệu CÓ THẬT, đã tổng hợp sẵn

Quy tắc cứng (KHÔNG được vi phạm):
1. CHỈ dùng số/sự kiện trong FACTS. Lịch sử hội thoại có thể nhắc tới các
   con số khác — KHÔNG được dùng/sao chép số từ lịch sử để bịa câu trả lời.
2. Nếu FACTS không có thông tin để trả lời câu hỏi: trả lời
   "Mình chưa có đủ thông tin cho câu này, bạn nói rõ hơn giúp mình."
   TUYỆT ĐỐI không suy diễn số tiền hay tên người.
3. Định dạng tiền VND có dấu chấm phân tách: 5.000.000đ.
4. KHÔNG đưa lời khuyên tài chính. KHÔNG đề nghị chuyển tiền.

Style:
- ≤ 60 từ, giọng tự nhiên, ấm. Văn bản trần — không markdown, không bullet.
- Nếu user hỏi theo "chủ đề / danh mục / vào gì": tổng hợp theo trường
  `by_category` hoặc `descriptions` trong FACTS.
- Nếu user hỏi follow-up: vẫn chỉ trích số từ FACTS hiện tại.
"""


def llm_phrase(
    user_question: str,
    facts: dict,
    history: Optional[list[dict]] = None,
    temperature: float = 0.4,
) -> Optional[str]:
    user_content = (
        f"USER_QUESTION: {user_question}\n\nFACTS: "
        f"{json.dumps(facts, ensure_ascii=False)}"
    )
    providers = _enabled_providers()
    if not providers and privacy.get_mode() == "local-only":
        privacy.record_llm_call(
            provider="(none)",
            mode="local-only",
            original_size=len(user_content),
            redacted_size=0,
            redaction_count=0,
            suppressed=True,
            note="llm_phrase suppressed by local-only mode",
        )
        return None
    for p in providers:
        text = _openai_compat(
            provider=p,
            system_prompt=_PHRASE_SYSTEM,
            history=history,
            user_message=user_content,
            temperature=temperature,
            response_format=None,
            max_tokens=220,
        )
        if text:
            return text.strip()
    return None


# ---------------------------------------------------------------------------
# Generic OpenAI-compatible call
# ---------------------------------------------------------------------------


def _openai_compat(
    *,
    provider: _Provider,
    system_prompt: str,
    history: Optional[list[dict]],
    user_message: str,
    temperature: float,
    response_format: Optional[dict],
    max_tokens: int,
) -> Optional[str]:
    mode = privacy.get_mode()

    # Compose the wire payload. In ``redact`` mode every user-supplied
    # string (the current message AND each prior turn's content) is
    # routed through the redactor before it leaves the process. The
    # system prompt is OUR string (no PII) so it's never rewritten.
    total_breakdown: dict[str, int] = {
        "ACCT": 0, "AMOUNT": 0, "PHONE": 0, "EMAIL": 0, "NAME": 0,
    }
    # Collect the original (pre-redact) user-side content lengths first,
    # then redact and collect the post-redact lengths. This avoids the
    # arithmetic error of double-counting that an inline sum invites.
    original_user_contents: list[str] = []
    wire_user_contents: list[str] = []

    if history:
        for turn in history:
            content = turn.get("content", "") if isinstance(turn, dict) else ""
            original_user_contents.append(content)
            if mode == "redact" and content:
                content, found = redact(content)
                for k, v in found.items():
                    total_breakdown[k] = total_breakdown.get(k, 0) + v
            wire_user_contents.append(content)

    original_user_contents.append(user_message)
    wire_user_message = user_message
    if mode == "redact":
        wire_user_message, found = redact(user_message)
        for k, v in found.items():
            total_breakdown[k] = total_breakdown.get(k, 0) + v
    wire_user_contents.append(wire_user_message)

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    if history:
        for turn, wire in zip(history, wire_user_contents[:-1]):
            messages.append({**turn, "content": wire})
    messages.append({"role": "user", "content": wire_user_message})

    body: dict = {
        "model": provider.model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if response_format is not None:
        body["response_format"] = response_format

    # Audit: record what we're about to send. ``original_size`` /
    # ``redacted_size`` measure ONLY the user-side content (excluding
    # the system prompt) so a judge can see the boundary that matters.
    original_size = sum(len(c) for c in original_user_contents)
    redacted_size = sum(len(c) for c in wire_user_contents) if mode == "redact" else original_size
    redaction_count = sum(total_breakdown.values())
    privacy.record_llm_call(
        provider=provider.name,
        mode=mode,
        original_size=original_size,
        redacted_size=redacted_size,
        redaction_count=redaction_count,
        redaction_breakdown=total_breakdown if mode == "redact" else {},
    )

    req = urllib.request.Request(
        provider.url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
            # Cloudflare in front of Groq blocks the default Python-urllib UA.
            "User-Agent": "omni-nlu/0.1 (+banking-assistant)",
            "Accept": "application/json",
        },
        method="POST",
    )
    # ``status`` is one of: ``ok`` (2xx), ``429`` (rate-limited — the
    # most operationally important error path because it drives our
    # provider fallback), ``http_4xx``, ``http_5xx``, ``network``,
    # ``parse``. The metric label space stays small.
    status = "ok"
    t0 = time.perf_counter()
    try:
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            choices = payload.get("choices") or []
            # Gemini may return a top-level list rather than a dict when erroring,
            # but on success the shape matches OpenAI's.
            if not choices:
                status = "empty"
                return None
            return choices[0]["message"]["content"]
        except urllib.error.HTTPError as e:
            try:
                body_text = e.read().decode("utf-8", "ignore")[:240]
            except Exception:
                body_text = ""
            log.warning("%s HTTP %s: %s", provider.name, e.code, body_text)
            if e.code == 429:
                status = "429"
            elif 400 <= e.code < 500:
                status = "http_4xx"
            else:
                status = "http_5xx"
            # 4xx/5xx → fall through so caller can try the next provider.
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.warning("%s network error: %s", provider.name, e)
            status = "network"
            return None
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            log.warning("%s parse error: %s", provider.name, e)
            status = "parse"
            return None
    finally:
        try:
            from ..services import metrics as _m

            _m.llm_call_total.inc(provider=provider.name, status=status)
            _m.llm_latency_seconds.observe(
                time.perf_counter() - t0, provider=provider.name
            )
        except Exception:
            pass

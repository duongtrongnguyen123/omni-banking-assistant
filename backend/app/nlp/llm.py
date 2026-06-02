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
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from ..config import get_settings
from ..models.schemas import ExtractedEntities, NLUResult

log = logging.getLogger("omni.nlu.llm")


@dataclass
class _Provider:
    name: str
    url: str
    api_key: str
    model: str


def _enabled_providers() -> list[_Provider]:
    """Priority order: Groq first (fastest), Gemini fallback if Groq is down."""
    s = get_settings()
    out: list[_Provider] = []
    if s.groq_api_key:
        out.append(
            _Provider(
                "groq",
                "https://api.groq.com/openai/v1/chat/completions",
                s.groq_api_key,
                s.groq_model,
            )
        )
    if s.gemini_api_key:
        out.append(
            _Provider(
                "gemini",
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                s.gemini_api_key,
                s.gemini_model,
            )
        )
    return out


# ---------------------------------------------------------------------------
# NLU
# ---------------------------------------------------------------------------

_NLU_SYSTEM = """You are the NLU layer of a Vietnamese banking assistant called Omni.
Return STRICT JSON only, no prose.

Schema:
{
  "intent": "transfer|balance|history|schedule|reminder|add_contact|smalltalk|unknown",
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
- "đặt lịch / hàng tháng / mỗi tháng / mùng X" → schedule.
- "nhắc nợ / nhắc trả" → reminder.
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

INPUT: "Đặt lịch chuyển mẹ 2tr vào mùng 1 hàng tháng"
{"intent":"schedule","confidence":0.95,"entities":{"recipient_text":"mẹ","amount":2000000,"amount_text":"2tr","schedule_cron":"0 9 1 * *"}}

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
    for p in _enabled_providers():
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
    for p in _enabled_providers():
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
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    body: dict = {
        "model": provider.model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if response_format is not None:
        body["response_format"] = response_format

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
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        choices = payload.get("choices") or []
        # Gemini may return a top-level list rather than a dict when erroring,
        # but on success the shape matches OpenAI's.
        if not choices:
            return None
        return choices[0]["message"]["content"]
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", "ignore")[:240]
        except Exception:
            body_text = ""
        log.warning("%s HTTP %s: %s", provider.name, e.code, body_text)
        # 4xx/5xx → fall through so caller can try the next provider.
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("%s network error: %s", provider.name, e)
        return None
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        log.warning("%s parse error: %s", provider.name, e)
        return None

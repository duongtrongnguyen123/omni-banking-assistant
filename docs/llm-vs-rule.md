# LLM vs Rule — biên giới rõ ràng

Omni tách 2 nguồn quyết định: **rule-based code** xử lý mọi thứ cần xác định
chính xác và defendable, **LLM** xử lý mọi thứ cần linh hoạt và tự nhiên.
Nguyên tắc: *khi nào có thể là rule thì là rule — bạn không muốn LLM tự "sáng
tác" một số tiền hay quyết định một giao dịch có an toàn hay không.*

## Bảng phân quyền

| Việc cụ thể | Source | Lý do |
|-------------|--------|-------|
| **Parse số tiền** ("5 triệu", "2tr500", "500k") | Rule (`nlp/amount.py`) | Phải chính xác đến từng đồng. LLM thỉnh thoảng làm tròn — không chấp nhận được cho banking. |
| **Trích STK** (regex 6-12 chữ số sau "STK" / "số tài khoản") | Rule (`nlp/entities.py:_ACCOUNT_HINT_RE`) | Số tài khoản không bao giờ được nhầm. |
| **Detect "xác nhận" / "huỷ"** | Rule (`_CONFIRM_RE`, `_CANCEL_RE`) | Câu lệnh tới-hạn — không cho LLM diễn giải lại. |
| **Detect OTP code (4-6 chữ số)** | Rule (`_OTP_RE`) | Lý do tương tự. |
| **Safety rule engine** (anomaly, balance, ambiguous) | Rule (`safety/rules.py`) | Phải defendable trước hội đồng kiểm toán. |
| **Execute transfer** (deduct balance, record tx) | Rule (`banking/service.py:execute_transfer`) | Side-effect không được LLM gần — write path là code thuần. |
| **Compose câu xác nhận giao dịch** ("Đã chuyển 5tr cho mẹ") | Rule (`_compose_transfer_text`, `_execute_and_record`) | Là *safety contract* — không cho LLM "diễn lại" số tiền/tên. |
| **Intent classification** (transfer/balance/history/…) | LLM (`nlp/llm.py:llm_understand`) với rule fallback | Câu nói tự nhiên đa dạng — rule sẽ phải maintain hàng trăm pattern. LLM bao quát tốt hơn, có thể fallback xuống rule khi 429. |
| **Entity surface form** ("mẹ", "anh Minh", "như tháng trước") | LLM trước, rule extractor sau (merge fill blanks) | Cùng lý do. Rule extractor handle các case ngắn cụ thể (như "2tr500") tốt hơn LLM nên giữ lại làm augment. |
| **Follow-up understanding** ("Đổi sang 3 triệu", "Còn tháng trước?") | LLM với conversation history | Rule không thể infer "field thừa kế từ turn trước". |
| **Viết câu trả lời cho history/balance/smalltalk** | LLM (`nlp/llm.py:llm_phrase`) | Cần giọng tự nhiên, biến tấu theo cách user hỏi. Cấm bằng prompt: "chỉ dùng số trong FACTS, không bịa". |
| **Viết câu trả lời cho intent=unknown** | Rule (static fallback) | Không có FACTS để LLM dựa vào — sẽ bịa. Đã có bằng chứng (audit C8). |

## Pipeline visualisation

```
   user message
       │
       ▼
   ┌──────────────────┐
   │  OTP rule check  │  ──▶ if digits & awaiting_otp → execute
   └──────────────────┘
       │ no
       ▼
   ┌─────────────────────────┐
   │  Confirm/cancel rule    │  ──▶ if "xác nhận"/"huỷ" → confirm/cancel draft
   └─────────────────────────┘
       │ no
       ▼
   ┌─────────────────────────┐
   │  NLU (LLM → rule)       │  ──▶ NLUResult.source = "llm" | "rule"
   └─────────────────────────┘
       │
       ▼
   ┌─────────────────────────┐
   │  Modify-draft heuristic │  ──▶ if active draft + new entities → modify
   └─────────────────────────┘
       │
       ▼
   ┌─────────────────────────┐
   │  Dispatch by intent     │  ──▶ transfer/schedule/contact: build draft
   │                         │      history/balance/smalltalk: LLM phrase
   └─────────────────────────┘
       │
       ▼
   ┌─────────────────────────┐
   │  Compose response       │
   │   - transactional: rule │
   │   - informational: LLM  │
   └─────────────────────────┘
```

## NLU source tracking

`NLUResult.source: Literal["llm", "rule"]` cho mỗi turn. Có thể log để biết
tỉ lệ LLM call thành công vs fallback xuống rule. Dùng để:

- Đo cost (chỉ LLM mới tốn tiền)
- Đo độ ổn định của provider
- Debug khi LLM cho output sai (so với rule baseline)

## Prompt safety contracts

Trong `nlp/llm.py:_PHRASE_SYSTEM`:

```
1. CHỈ dùng số/sự kiện trong FACTS. Lịch sử hội thoại có thể nhắc tới các
   con số khác — KHÔNG được dùng/sao chép số từ lịch sử để bịa câu trả lời.
2. Nếu FACTS không có thông tin để trả lời câu hỏi:
   trả lời "Mình chưa có đủ thông tin cho câu này, bạn nói rõ hơn giúp mình."
   TUYỆT ĐỐI không suy diễn số tiền hay tên người.
3. Định dạng tiền VND có dấu chấm phân tách: 5.000.000đ.
4. KHÔNG đưa lời khuyên tài chính. KHÔNG đề nghị chuyển tiền.
```

Đã chứng minh cần thiết qua bằng chứng audit (LLM từng bịa "Tài khoản phụ
1.200.000đ" và "Trần Thị Thảo / Vietinbank" trước khi prompt được siết).

## Khi nào chuyển 1 việc từ LLM sang rule (hoặc ngược lại)?

- LLM → rule: khi cùng câu input gây ra output không nhất quán giữa 2 lần
  gọi (LLM bị non-deterministic), VÀ đầu ra phải chính xác.
- Rule → LLM: khi pattern phải maintain quá nhiều case (ví dụ: nếu rule
  intent classifier bắt đầu phải có 50+ keyword pattern, đến lúc đẩy lên LLM).

## Provider fallback chain

`nlp/llm.py:_enabled_providers()` thử theo thứ tự:

1. **Groq** — Llama 3.3 70B, ~1.5-2s latency, OpenAI-compat endpoint
2. **Gemini** — gemini-2.0-flash, fallback nếu Groq 429/timeout

Nếu cả hai fail → `understand()` rơi xuống rule classifier (`nlp/intent.py`)
và rule extractor (`nlp/entities.py`). Demo không bao giờ vỡ vì lý do
network, chỉ giảm độ thông minh.

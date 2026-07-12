# Omni — Trợ lý AI Ngân hàng bằng tiếng Việt

Trợ lý đối thoại tiếng Việt giúp khách hàng thực hiện giao dịch ngân hàng
bằng ngôn ngữ tự nhiên. Người dùng gõ (hoặc nói) một câu như
*"gửi cho mẹ 5 triệu như tháng trước"*, hệ thống hiểu ý định, phân giải
"mẹ" → đúng người nhận trong danh bạ, lấy số tiền + nội dung từ giao dịch
tháng trước, chạy đầy đủ kiểm tra an toàn, rồi trình 1 thẻ **Xác nhận** duy
nhất. Rút gọn 7 bước Smart Banking cổ điển xuống còn **Chat → Confirm → Done**.

---

## Kiến trúc

Năm tầng, mỗi tầng có thể swap độc lập:

```
┌────────────┐   ┌────────────┐   ┌──────────────┐   ┌──────────┐   ┌────────────┐
│ 1. Chat UI │──▶│ 2. NLU     │──▶│ 3. Context   │──▶│ 4. Safety│──▶│ 5. Banking │
│ React + WS │   │ LLM + Rule │   │ Alias · Time │   │ Rules +  │   │ Mock Core  │
│ Voice · TTS│   │ failover   │   │ RAG (local)  │   │ Fraud ML │   │ (JSON seed)│
└────────────┘   └────────────┘   └──────────────┘   └──────────┘   └────────────┘
```

| Tầng | File chính | Công nghệ |
|---|---|---|
| **1. Chat UI** | `frontend/src/` | React + Vite + TypeScript. Voice input (Web Speech API vi-VN), TTS phản hồi tiếng Việt, WebSocket toast events |
| **2. NLU** | `backend/app/nlp/` | Provider-agnostic LLM chain với automatic failover (Groq Llama 3.3 70B → Google Gemini → OpenRouter). Circuit breaker per-provider + rule-based extractor bên dưới đảm bảo NLU không bao giờ mất khả năng phục vụ, kể cả khi mọi upstream đều down |
| **3. Context** | `backend/app/context/`, `backend/app/db/` | Alias resolver 5 bước (exact → token → prefix → RAG), temporal resolver, RAG contact lookup dùng `fastembed` multilingual MiniLM 384-d chạy hoàn toàn on-device |
| **4. Safety** | `backend/app/safety/` | Rule engine (missing/ambiguous slot, new+large, MAD anomaly, insufficient balance), **Isolation Forest** cho fraud score, OTP + sinh trắc (face-scan 8D) step-up |
| **5. Banking** | `backend/app/banking/`, `backend/app/store.py` | Mock transfer / balance / history / schedule. Có sẵn adapter Postgres (RDS omni schema) khi cần data thật |

Chi tiết trace end-to-end 1 giao dịch: [`docs/architecture.md`](docs/architecture.md).

---

## Tính năng

### Hiểu ngôn ngữ tự nhiên tiếng Việt

- **Alias resolution 5 bước**: exact → token → prefix → embedding RAG. "mẹ" → Nguyễn Thị Lan, "quán bún quen gần nhà" → contact đúng dù tên trong danh bạ dài dòng
- **Temporal reference**: "như tháng trước", "hôm qua", "tuần này", "năm ngoái" → mỗi cụm map sang window riêng
- **Amount parsing**: "5 triệu", "1tr5", "500k", "hai mươi lăm nghìn", "một củ", "5 chai" — cover 12+ cách gõ số tiền
- **Colloquial banking**: "còn bao nhiêu tiền", "cạn ví chưa", "lương về chưa" → intent balance đúng, không leak sang history

### An toàn nhiều lớp

- **Kiểm tra trước khi chuyển**: hệ thống nhận diện 5 tình huống cần cảnh báo — người nhận chưa rõ, thiếu thông tin, người mới + số tiền lớn, số tiền lệch quá xa thói quen, tài khoản không đủ. Mỗi cảnh báo đi kèm lý do cụ thể để giao diện có thể hiển thị rõ *tại sao lại cảnh báo*, không chỉ một icon đỏ vô nghĩa.
- **Phát hiện giao dịch bất thường**: mô hình Isolation Forest huấn luyện riêng theo từng người dùng, so sánh giao dịch hiện tại với thói quen chi tiêu cá nhân. Nếu bất thường → tự động yêu cầu xác thực OTP thêm.
- **Chống race điều khiển**: người dùng bấm Xác nhận rồi lỡ tay bấm Huỷ ngay sau đó không thể phá vỡ trạng thái. Cả frontend lẫn backend đều khoá luồng: khi confirm đang xử lý thì cancel bị từ chối lịch sự. Loại bỏ tình huống *"tiền vẫn chuyển mà UI báo đã huỷ"*.
- **Xác thực khuôn mặt**: các giao dịch nguy cơ cao (số tiền lớn, người nhận mới) yêu cầu quét khuôn mặt 8 hướng trực tiếp trên máy. Ảnh khuôn mặt **không rời khỏi máy người dùng** — chỉ signature (vector đặc trưng đã mã hoá) được gửi lên server để đối chiếu.

### LLM chain — provider-agnostic với automatic failover

Ngân hàng không thể để chat visible-fail khi 1 vendor down. Chain được thiết kế
theo pattern **circuit breaker + graceful degradation**, không bị khoá vào một
provider duy nhất:

```
Groq pool (N keys, round-robin)  →  Gemini pool  →  OpenRouter pool  →  Rule extractor
   ↓ 429/401/403                       ↓ same           ↓ same             (always available)
   Circuit-open 60 min, downprioritise xuống cuối chain
   Chain có wall-time deadline 5s: quá ngưỡng → giao ngay cho rule
```

**Reliability primitives:**

- **Per-provider circuit breaker** — 429 (quota) / 401/403 (auth) đều mark provider dead 60 phút, chuyển xuống cuối pool. Không waste thêm walk-tax cho request tiếp theo.
- **Round-robin trong pool** — nhiều key cùng provider được rotate mỗi call, load spread đều thay vì hammer key #1 rồi sập.
- **Wall-time deadline 5s** — nếu chain walk vượt budget, abandon LLM path, giao thẳng cho rule. Chat turn không bao giờ chờ >5s cho LLM.
- **Rule-based fallback** — khi mọi LLM provider chết, rule extractor phủ **~85% intent** (transfer / balance / history / schedule / atm / smalltalk / help). App vẫn hoạt động, không hiện error.
- **Offline mode** — env `OMNI_OFFLINE_DEMO=1` skip toàn bộ outbound call. Rule engine take over. Dành cho on-prem / air-gapped scenario.

Đo trực tiếp trên production-like setup: P95 latency chat turn **<300 ms** ở happy path, **<1.2 s** khi tier đầu bị 429 (fail-fast + fallback), **<50 ms** ở rule-only path.

Chi tiết: [`docs/llm-vs-rule.md`](docs/llm-vs-rule.md), [`docs/perf.md`](docs/perf.md).

### ML/analytics phụ trợ

| Thành phần | Mô tả | File |
|---|---|---|
| **Recipient suggester** | RandomForest + rule scorer + frequency prior, auto-weighted theo data size, A/B + Thompson bandit chọn trọng số online | `ml/suggester.py`, `ml/bandit.py` |
| **Amount predictor** | Median từ history + rationale + confidence | `ml/amount_predictor.py` |
| **Categorizer** | TF-IDF + rule (13 category), <2ms, precision 0.95 | `ml/categorizer.py` |
| **Insights** | MoM delta, per-recipient z/MAD anomaly, subscription detection | `ml/insights.py` |
| **Recurring miner** | Bucket-by-month pattern miner, không cần schedule config | `banking/recurring.py` |

---

## Kết quả benchmark

Các thành phần ML/NLU ở trên được eval trên **3 dataset công khai** (không dùng seed tự tạo) để tránh circular scoring. Method chi tiết: [`docs/eval-real-data.md`](docs/eval-real-data.md).

### Suggester — dự đoán người nhận tiếp theo

| Dataset | Hit@1 | Hit@3 | Hit@5 |
|---|---|---|---|
| **BankSim 594k giao dịch (labelled merchant)** | **0.81** | **0.92** | **0.97** |

Best ablation: `tree + freq` (RandomForest + frequency prior). Rule scorer chỉ giúp trên dataset VN — locale-gated.

### Phát hiện thanh toán định kỳ

| Dataset | Precision | Recall | F1 |
|---|---|---|---|
| **Czech PKDD'99** (dataset ngân hàng thật, có ground-truth `permanent_orders`) | **0.69** | **0.80** | **0.74** |

20/25 lệnh chuyển định kỳ được phát hiện chỉ từ luồng giao dịch, không cần bất cứ metadata nào. 9 "false positive" thực chất là các khoản khách hàng chuyển định kỳ nhưng chưa đăng ký hệ thống — hữu ích, không phải lỗi.

### Fraud Isolation Forest

Trên **BankSim 7200 giao dịch fraud có label**:
- Median anomaly score: fraud **0.58** vs legit **0.22**
- Ở threshold 0.5: **recall 0.75** · precision 0.14 · FP-rate-on-legit 0.11
- Đủ mạnh để làm signal **OTP step-up**, chưa đủ để autoblock

Toàn bộ eval chạy <20s trên 520k-row contest DB (in-memory sau initial SELECT).

---

## Cách chạy

### Backend

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env       # tuỳ chọn — có thể chạy không cần LLM key
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

API docs: <http://localhost:8000/docs>

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Mở <http://localhost:5173>. Vite proxy `/api` và `/ws` sang `localhost:8000`.

### Bật LLM (tuỳ chọn)

Thêm vào `backend/.env`:

```env
GROQ_API_KEY=gsk_xxx
GEMINI_API_KEY=xxx
OPENROUTER_API_KEY=sk-or-v1-xxx
```

Chain tự động fallback theo thứ tự Groq → Gemini → OpenRouter → rule. Không cần key nào cũng chạy được — rule engine phủ ~85% intent.

### Chế độ offline (demo không mạng)

```bash
OMNI_OFFLINE_DEMO=1 uvicorn app.main:app --port 8000
```

Skip toàn bộ outbound call. Xem [`docs/offline-demo.md`](docs/offline-demo.md).

### Docker

```bash
make docker-build && make docker-run    # single image, frontend bundled
```

### Deploy lên Render

Có sẵn `render.yaml` blueprint — click Deploy tại <https://dashboard.render.com/blueprints/new>, point vào repo này. Xem chi tiết trong file config.

---

## Cấu trúc thư mục

```
.
├── backend/
│   ├── app/
│   │   ├── main.py                ◀ FastAPI entry
│   │   ├── config.py              ◀ Settings (pydantic-settings)
│   │   ├── nlp/                   ◀ Tầng 2 — NLU pipeline
│   │   │   ├── llm.py             ◀ Multi-provider chain, backoff, deadline
│   │   │   ├── intent.py          ◀ Tier-1/2/3 keyword classifier
│   │   │   ├── entities.py        ◀ Rule extractors (VN-aware)
│   │   │   └── embeddings.py      ◀ fastembed multilingual MiniLM
│   │   ├── context/               ◀ Tầng 3 — alias, temporal, session
│   │   ├── safety/                ◀ Tầng 4 — rule engine + fraud model
│   │   ├── banking/               ◀ Tầng 5 — transfer, balance, history…
│   │   ├── ml/                    ◀ Suggester, predictor, categoriser, insights
│   │   ├── services/orchestrator.py  ◀ Brain — handle_message dispatch
│   │   ├── routes/                ◀ REST endpoints
│   │   └── db/                    ◀ SQLite schema + chat history archive
│   ├── tests/                     ◀ 570+ test, 3 pre-existing failure documented
│   └── scripts/                   ◀ smoke, eval_*, load_*, seed generators
├── frontend/
│   └── src/
│       ├── App.tsx                ◀ Phone-frame chat shell
│       ├── components/            ◀ Message · TransactionCard · BalanceCard · …
│       └── api/client.ts
├── docs/                          ◀ Architecture, eval, perf, deploy, …
└── Dockerfile                     ◀ Multi-stage: bundled frontend + backend
```

---

## Data sources

| Nguồn | Dùng làm gì | File |
|---|---|---|
| `backend/app/data/*.json` | Seed demo 30-contact / 35-tx | Mặc định bootstrap vào `omni.db` SQLite |
| `data/demo/*.json` | Subset 1000-contact / 1888-tx từ dataset thi | `BANKING_DATA_DIR=../data/demo` |
| `generated/transactions_enriched_6m.csv` | Full 591k-row dataset thi | `scripts/load_contest_full.py` → `omni_contest.db` |
| `data/public/czech_pkdd99/*.tsv` | Ngân hàng Czech thật (1.05M tx + ground truth) | `scripts/load_czech.py` |
| `data/public/banksim/*.csv` | BankSim (594k tx + label fraud) | `scripts/load_banksim.py` |

---

## Chạy test / eval

```bash
make check         # 19 assertion pre-pitch smoke test
make test          # Full pytest suite (500+ test)
make test-nlu      # NLU-focused subset

# Eval trên public dataset:
python backend/scripts/eval_suggester_banksim.py
python backend/scripts/eval_recurring_czech.py
python backend/scripts/eval_fraud_banksim.py
```

---

## Docs

- [`docs/architecture.md`](docs/architecture.md) — trace end-to-end 1 giao dịch qua 5 tầng
- [`docs/eval-real-data.md`](docs/eval-real-data.md) — eval chi tiết trên public dataset + pre-registered protocol (appendix)
- [`docs/perf.md`](docs/perf.md) — latency budget + chain fast-fail
- [`docs/llm-vs-rule.md`](docs/llm-vs-rule.md) — biên giới quyết định LLM vs rule
- [`docs/privacy.md`](docs/privacy.md) — privacy mode + LLM payload audit
- [`docs/persistence.md`](docs/persistence.md) — session store, Redis, SQLite
- [`docs/error-handling.md`](docs/error-handling.md) — error taxonomy + retry
- [`docs/a11y-audit.md`](docs/a11y-audit.md) — WCAG 2.1 AA audit
- [`docs/offline-demo.md`](docs/offline-demo.md) — chế độ không cần mạng
- [`docs/admin-auth.md`](docs/admin-auth.md) — admin route auth model
- [`docs/deploy-tunnel.md`](docs/deploy-tunnel.md) — expose local demo via Cloudflare tunnel
- [`docs/eval-contest-dataset.md`](docs/eval-contest-dataset.md) — original suggester writeup trên contest dataset

---

## Ghi chú kỹ thuật

- **Không cần Postgres/Redis** cho demo. Store là JSON in-memory → SQLite. Có sẵn adapter Postgres khi swap.
- **LLM không phải bắt buộc**. Rule extractor phủ ~85% intent (transfer / balance / history / schedule / atm / smalltalk / help). LLM giúp hiểu câu tự nhiên hơn — không có key vẫn demo được.
- **Embedding local**. `fastembed` + MiniLM 384-d ONNX. Không phụ thuộc cloud embedding — quan trọng cho on-prem banking.
- **Vietnamese NFC**. Regex target codepoint dựng sẵn (`ử` = U+1EED); alias path chấp nhận cả không dấu ("me", "minh", "nhu thang truoc").
- **Race-safe confirm**. `_INFLIGHT_CONFIRMS` set + frontend `inFlightDraftIds` khoá đường race giữa OTP-submit và cancel.
- **Chat history persisted**. SQLite `chat_sessions` + `chat_messages` archived tất cả conversation. UI có left drawer list/reopen/delete.

---

## License

MIT.

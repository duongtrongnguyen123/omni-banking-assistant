# Omni — live demo script

Step-by-step guide for the live pitch. Practice timings until you can do the
full flow in **4:00**, leaving 1 minute for Q&A buffer.

Before opening the laptop:
- Frontend running on `localhost:5173` (`make frontend`)
- Backend on `localhost:8000` (`make backend`)
- DB freshly bootstrapped — if anything weird shows in chat, `rm backend/app/data/omni.db` and restart uvicorn.
- Browser zoom 110%, dev tools closed, mic permission already granted.
- Network: have a hotspot backup. LLM calls are graceful-degrading but the
  WS connect needs *some* network.

## Slide 1 (0:00–0:15) — The pain

> "Đặt một lệnh chuyển tiền trên app ngân hàng Việt Nam hiện tại tốn **7 bước**:
> mở app → chọn chuyển khoản → chọn người nhận → nhập tài khoản → nhập số tiền
> → nhập nội dung → xác nhận OTP. Mỗi bước là một chỗ user có thể bỏ cuộc."

Hold up phone with the chat UI visible.

## Slide 2 (0:15–0:30) — The promise

> "Omni rút gọn còn 2 bước: **Chat → Confirm**. Không phải vì ta cắt safety —
> safety vẫn nguyên — mà vì model hiểu đủ ý để tự điền cả 5 bước giữa."

Type: `Chuyển cho mẹ 2 triệu`

Wait for the TransactionCard. Point at:
- Recipient resolved: "mẹ" → Nguyễn Thị Lan
- Amount: 2.000.000đ
- Source account auto-picked

Click **Xác nhận**. Highlight the success state.

## Slide 3 (0:30–2:00) — Three Vietnamese-specific differentiators

### A. Alias resolution (0:30–0:50)

> "Người Việt không gọi tên ai trong ngân hàng cả. Họ gọi *mẹ*, *bố*, *bestie*,
> *ny*, *cô bán bún*. Omni resolve được tất cả."

Quick chip → KB2: `Gửi cho mẹ 5 triệu như tháng trước`

Point at the response:
- *mẹ* resolved to Lan
- **Description filled from past tx** (the "như tháng trước" part)
- Amount kept user-specified 5tr, NOT the past 2tr (correctness check)

### B. Ambiguity handling (0:50–1:15)

> "Nhưng nếu bạn có hai người tên Minh trong danh bạ thì sao?"

Quick chip → KB3: `Chuyển cho Minh 500k`

DisambiguationCard appears with both Minhs. Click "Nguyễn Văn Minh".

> "User chỉ nói một câu, hệ thống hỏi lại đúng câu hỏi cần hỏi, không hỏi
> lại những thứ nó đã biết."

### C. Multi-turn modify (1:15–1:35)

Type: `đổi sang 3 triệu thôi`

Card updates IN PLACE. Don't create a new draft.

> "Đây là điểm hầu hết chat-banking demo bị sai. User nói tiếp `đổi sang 3 triệu`,
> hệ thống phải hiểu đó là **edit**, không phải lệnh mới. State machine của Omni
> phân biệt được — và LLM hoàn toàn KHÔNG được phép viết câu xác nhận chuyển tiền."

Click **Huỷ** to clear before the next demo.

### D. Voice input (1:35–2:00) [skip if mic permission is finicky]

Click the mic button. Say: *"Chuyển cho ny ba trăm nghìn ăn tối"*.

Live transcription in input. Send.

> "Voice là Web Speech API native — chạy local, không phụ thuộc cloud STT."

## Slide 4 (2:00–2:45) — Safety wall

Type: `Chuyển 50 triệu cho Hùng STK 9990001234`

The TransactionCard shows **three** flags:
1. `new_recipient_large_amount` — warn
2. `amount_above_average` — "cao gấp ~36×"
3. `insufficient_balance` — block

> "Đây không phải LLM phán đoán. Đây là **rule engine** — `safety/rules.py`, 
> deterministic, audit-able. LLM viết được câu thông cảm `Khoan đã, số tiền 
> này cao hơn thường lệ nhé` — nhưng nó **KHÔNG được viết** dòng `Đã chuyển 
> 50 triệu` — dòng đó chỉ do code build sau khi transfer thực sự xảy ra."

Highlight: this is the **LLM safety contract** — see `docs/llm-vs-rule.md`.

## Slide 5 (2:45–3:15) — Scale claim

Open a second tab → API docs at `localhost:8000/docs` → run
`GET /api/suggestions/recipients?limit=5`.

Point at the response time in the response (or open dev tools).

> "Backend đang chạy trên dataset 520k giao dịch của BTC — P95 dưới 250ms,
> P50 dưới 50ms. Embedding + RAG search **chạy local** bằng fastembed
> (multilingual MiniLM 384-d). Không gửi data ra cloud cho phần embedding."

Open `docs/perf.md` if time allows.

## Slide 6 (3:15–3:30) — Honest about ML

> "Một điều quan trọng: trên dataset BTC cấp, Hit@K của suggester ≈ random
> baseline. Không phải lỗi model — dataset BTC simulate 1000 counterparties
> uniform, không có pattern thật để học. Trên dataset Czech PKDD'99 (real
> bank data, 1M tx, có ground-truth permanent_orders), recurring detector F1 = X%.
> Chúng tôi không nói model dự đoán giỏi — chúng tôi nói infrastructure ready
> để swap data thật vào."

[Numbers fill in after agent #7 finishes Czech eval.]

## Slide 7 (3:30–3:50) — What we built that's novel

Three things, in priority order:

1. **LLM safety contract** — *"LLM được phép phrase, không được phép assert money facts."*
2. **Multi-turn modify** — *"User nói tiếp được, hệ thống không reset state."*
3. **Local-first RAG + embedding** — *"Toàn bộ NLU stack chạy offline được nếu cần."*

## Q&A buffer (3:50–5:00)

Likely judge questions — answers in [`docs/honest-pitch.md`](honest-pitch.md).

Common ones:
- *Why not deep learning?* → "Interpretability. Tree + rule lets us explain why."
- *Production-ready?* → "Session is in-memory, OTP is mocked. Real OTP service and Redis swap are interface-clean — ~half a day each."
- *Cost?* → "fastembed runs local. LLM only called for response *phrasing*, with rule fallback when rate-limited (we hit 100k token/day rate limit during testing — system kept working)."

---

## Demo data reference

The seed has these recipients available for live testing:

| Alias | Real name | Bank |
|-------|-----------|------|
| mẹ, me | Nguyễn Thị Lan | Vietcombank |
| bố, bo, bố Hùng | Trần Quốc Hùng (different from KB5 Hùng!) | BIDV |
| Minh, anh Minh | Nguyễn Văn Minh **AND** Trần Hoàng Minh | VCB / TCB |
| bestie, ny | Lê Thị Hương | MB Bank |
| sếp | Phạm Quốc Anh | VPBank |

Past transactions exist for *mẹ* (multiple), *bố*, *PT* — these are what
KB2 ("như tháng trước") and KB4 ("tháng này gửi bao nhiêu") read from.

## Recovery scripts if things break

**Chat returns 500**:
```bash
# Probably embedding/LLM choked. Skip embeddings:
OMNI_SKIP_EMBED_BACKFILL=1 .venv/bin/python -m uvicorn app.main:app --port 8000
```

**Frontend shows old state**:
- Hard refresh (Cmd+Shift+R)
- Clear localStorage in dev tools (`omni.tts.enabled` key etc.)

**Demo seed got modified mid-demo**:
```bash
rm backend/app/data/omni.db
# Restart uvicorn — it auto-reseeds from JSON.
```

**LLM rate-limited**:
- The rule fallback handles all 6 KBs alone. **This is actually a feature**
  — point it out: *"Notice we hit Groq quota mid-demo — system kept working
  because LLM is additive, not a dependency."*

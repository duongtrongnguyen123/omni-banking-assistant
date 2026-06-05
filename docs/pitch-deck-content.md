# Omni — Slide deck content (8 slides)

Hướng dẫn cho designer dựng deck. Mỗi slide có **tiêu đề tiếng Việt**, **tối đa 3 bullet**,
và mô tả **visual** (screenshot, snippet hoặc sơ đồ) đi kèm.

Phong cách: tối giản, dark / hi-contrast cho phòng chiếu, **không emoji** trong slide chính.
Font: SF Pro / Inter. Accent: xanh ngân hàng (#0EA5E9).

---

## Slide 1 — Nỗi đau (Hook)

**Tiêu đề:** Chuyển tiền hôm nay vẫn cần **7 bước**

- Mở app → Chuyển khoản → Chọn người → Nhập STK → Số tiền → Nội dung → OTP
- Mỗi bước là một cơ hội bỏ cuộc
- 78% thao tác chuyển tiền là **người quen lặp lại** *(insight từ slide gốc)*

**Visual:** 7 ô vuông xếp ngang, gạch chéo 5 ô giữa, mũi tên thu vào 2 ô "Chat" + "Confirm".

---

## Slide 2 — Lời hứa (Promise)

**Tiêu đề:** Omni — **Chat → Confirm → Done**

- Nhập một câu tiếng Việt tự nhiên
- Xác nhận một lần
- Safety vẫn nguyên — chỉ bỏ phần thao tác thủ công

**Visual:** Screenshot phone-frame: bubble user "Chuyển cho mẹ 2 triệu" + `TransactionCard` với nút "Xác nhận" / "Huỷ".

---

## Slide 3 — Ba điểm khác biệt tiếng Việt

**Tiêu đề:** Hiểu **ý định**, không phải câu chữ

- **Alias:** "mẹ", "bố", "ny", "cô bán bún chợ" → đúng contact
- **Temporal:** "như tháng trước" → tự fill nội dung + số tiền từ lịch sử
- **Ambiguity:** 2 người tên Minh → hỏi đúng một câu cần hỏi

**Visual:** 3 chat bubble screenshot ngắn, mỗi bubble một differentiator.

---

## Slide 4 — Bức tường an toàn (Safety Wall)

**Tiêu đề:** An toàn ngân hàng vẫn được đảm bảo

- **Rule engine** deterministic: ambiguous / new + large / anomaly / balance
- **OTP step-up** khi cờ warn — interface sẵn cho real OTP service
- **LLM safety contract:** LLM phrase được, nhưng **không bao giờ** viết "Đã chuyển X cho Y"

**Visual:** Screenshot `TransactionCard` 50 triệu cho Hùng — 3 badge đỏ. Bên dưới snippet:

```python
# nlp/llm.py:_PHRASE_SYSTEM
# "You may NOT assert that a transfer happened.
#  Confirmed-transfer lines are produced deterministically by code."
```

---

## Slide 5 — Bằng chứng thực nghiệm trung thực

**Tiêu đề:** Số đo trên **dữ liệu công khai**, không phải pattern tự gắn

| Metric | Dataset | Số |
|---|---|---|
| Suggester Hit@1 | BankSim 594k tx | **0.81** |
| Suggester Hit@5 | BankSim 594k tx | **0.97** |
| Recurring F1 | Czech PKDD'99 | **0.74** |
| Fraud recall @ FP 0.11 | BankSim 7.2k fraud | **0.75** |

- Pre-registered seed = 42 trong `docs/eval-protocol.md`
- Trên dataset BTC (uniform 1000 counterparty) → Hit@K ≈ random — chúng tôi **xác nhận**, không che giấu

**Visual:** Bar chart 4 cột BankSim Hit@1 / Hit@3 / Hit@5 + Czech F1.

---

## Slide 6 — Kiến trúc 5 lớp

**Tiêu đề:** Stack NLU ngân hàng tiếng Việt, local-first

- **5 lớp:** Chat UI · NLU · Context · Safety · Banking
- **Multi-provider LLM:** Groq → Gemini → rule fallback (demo không vỡ khi rate-limit)
- **fastembed local** (MiniLM multilingual 384-d) — embedding **không gửi cloud**

**Visual:** Sơ đồ 5 hộp xếp dọc, mũi tên xuống, ghi tech stack mỗi hộp. Highlight đường "rule fallback" bypass LLM.

---

## Slide 7 — Quy mô & chất lượng

**Tiêu đề:** Production-aware, không chỉ pitch-ready

- **56 backend routes** trên 22 prefixes · **386 pytest** pass / 10 xfail / 6.2s
- **200/200 NLU corpus** pass với LLM mock — chứng minh rule layer đứng vững
- k8s `/health/{live,ready,version}` · Prometheus `/api/metrics` · Privacy mode 5 lớp PII

**Visual:** Terminal screenshot `make verify` → toàn bộ checkmark xanh. Bên cạnh: bảng route count theo prefix.

---

## Slide 8 — Đóng (Call to action)

**Tiêu đề:** Omni — 27 tính năng, tất cả gate đang xanh

- Không phải lớp chat phủ lên Smart Banking — là **stack NLU ngân hàng tiếng Việt safety-first**
- Defendable trên 3 dataset công khai · Local-first · LLM-bounded
- **Đang sẵn sàng** swap OTP thật + Redis multi-instance (interface clean)

**Visual:** Logo Omni + 3 số lớn: **0.81 · 0.74 · 386** + QR code repo GitHub. Tagline cuối: *"Hiểu tiếng Việt. Giữ an toàn. Không bịa số."*

---

## Phụ lục — Screenshot cần chuẩn bị

1. Phone-frame chat: user "Chuyển cho mẹ 2 triệu" + TransactionCard confirmed (slide 2)
2. Chat: "Gửi mẹ 5 triệu như tháng trước" + temporal back-fill chip (slide 3)
3. Chat: DisambiguationCard 2 Minh (slide 3)
4. Chat: 50 triệu cho Hùng + 3 badge đỏ (slide 4)
5. Terminal: `make verify` toàn xanh (slide 7)
6. (optional) Insights dashboard — MoM + anomaly card (Q&A buffer)

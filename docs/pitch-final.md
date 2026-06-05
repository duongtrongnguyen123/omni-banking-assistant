# Omni — Pitch script (5 phút)

**Đội:** One Last Token · **Sản phẩm:** Omni — AI Banking Assistant tiếng Việt
**Mục tiêu:** Chat → Confirm → Done. Bỏ 5 bước giữa của luồng chuyển tiền truyền thống.

> Quy ước thời gian: mỗi mốc dưới đây là **mốc bắt đầu** trên đồng hồ stopwatch.
> Người demo nói ý chính, người dẫn slide đổi slide đúng mốc.

---

## 0:00 – 0:15 — Hook (Nỗi đau)

> "Đặt một lệnh chuyển tiền trên Smart Banking hiện tại tốn **7 bước**:
> mở app → chọn chuyển khoản → chọn người nhận → nhập STK → nhập số tiền
> → nhập nội dung → OTP. Mỗi bước là một cơ hội user bỏ cuộc.
> Omni rút còn **2 bước**: nhập một câu, xác nhận một lần."

*(Slide 1 — 7 ô vuông xếp ngang, gạch chéo 5 ô giữa.)*

---

## 0:15 – 0:45 — Live demo cơ bản

Mở phone-frame chat. Gõ:

```
Chuyển cho mẹ 2 triệu
```

Chỉ vào `TransactionCard`:

- "mẹ" → **Nguyễn Thị Lan** (alias resolver)
- Số tiền **2.000.000đ** đã parse
- Tài khoản nguồn **tự chọn**

Bấm **Xác nhận**. Card flip + confetti.

> "Đó là Chat → Confirm → Done. 4 giây. Không một lần nào chạm STK."

---

## 0:45 – 1:30 — Ba điểm khác biệt tiếng Việt

### A. Alias resolution (0:45 – 1:00)

> "Người Việt không gọi tên trong ngân hàng. Họ gọi **mẹ, bố, ny, sếp, cô bán bún chợ**."

Gõ: `Gửi cho mẹ 5 triệu như tháng trước`

Chỉ:
- "mẹ" → Lan (giống lần trước)
- **Nội dung tự fill từ tx tháng trước** ("như tháng trước")
- Số tiền giữ 5tr user nói (KHÔNG bị ghi đè bằng amount cũ)

### B. Ambiguity (1:00 – 1:15)

Gõ: `Chuyển cho Minh 500k`

`DisambiguationCard` hiện 2 Minh trong danh bạ. Bấm "Nguyễn Văn Minh".

> "Hệ thống hỏi đúng câu cần hỏi. Không hỏi lại số tiền vì đã có."

### C. Multi-turn modify (1:15 – 1:30)

Gõ tiếp: `đổi sang 3 triệu thôi`

Card cập nhật **tại chỗ**. Không tạo draft mới.

> "Đây là chỗ hầu hết chat-banking demo sai. Câu 2 là **edit**, không phải lệnh mới.
> State machine của Omni phân biệt được."

---

## 1:30 – 2:15 — Safety wall

Gõ: `Chuyển 50 triệu cho Hùng STK 9990001234`

Card hiện **3 cờ**:
1. `new_recipient_large_amount` — cảnh báo
2. `amount_above_average` — "cao gấp ~36×"
3. `insufficient_balance` — chặn

> "Đây không phải LLM phán đoán. Đây là **rule engine** trong `safety/rules.py` —
> deterministic, audit-able.
> LLM được viết câu thông cảm `Khoan đã, số tiền này cao hơn thường lệ nhé` —
> nhưng **không bao giờ** được phép viết dòng `Đã chuyển 50 triệu cho Hùng`.
> Dòng đó chỉ do code build, sau khi transfer thực sự xảy ra. Đây là **LLM safety contract** —
> hợp đồng cốt lõi của Omni."

---

## 2:15 – 3:00 — Bằng chứng thực nghiệm trung thực

> "Chúng tôi không bịa số. Đây là kết quả trên **dữ liệu công khai có ground truth**,
> không phải pattern do chúng tôi tự gắn vào."

| Metric | Dataset | Số |
|---|---|---|
| Suggester Hit@1 | BankSim 594k tx (merchant labels) | **0.81** |
| Suggester Hit@5 | BankSim 594k tx | **0.97** |
| Recurring detector F1 | Czech PKDD'99 (real bank) | **0.74** (P 0.69 / R 0.80) |
| Fraud Isolation Forest | BankSim 7.2k fraud labels | recall 0.75 @ FP 0.11 |

> "Trên dataset cuộc thi (520k tx uniform per counterparty) thì Hit@K ≈ random —
> chúng tôi **xác nhận** điều đó trong `docs/eval.md` và không che giấu.
> Dataset BTC không có signal thời gian để học. Eval đã pre-register seed = 42."

---

## 3:00 – 3:45 — Bonus differentiator: VietQR

> "Một thứ vượt brief slide: **VietQR-style generator + camera scan**."

Bấm chip `/qr`. Camera mở, scan QR test → form chuyển tiền tự fill (recipient + STK + amount).

> "Codec TLV viết tay theo chuẩn VietQR mock. jsQR lazy-load **47kB chunk** —
> không nặng bundle chính. Đây là ví dụ ta xử được cả **input đa phương thức**:
> text, voice (Web Speech vi-VN), và QR."

---

## 3:45 – 4:30 — Scale + reliability

Mở `localhost:8000/docs` thoáng qua, rồi tab `Insights`:

- **56 backend routes** trên 22 prefixes
- **386 pytest passing** (+10 xfail có chủ đích) trong 6.2s
- **200/200 NLU corpus** pass với LLM mock
- **k8s probes** `/health/{live,ready,version}` + graceful shutdown
- **Prometheus** `/api/metrics` — 7 series live
- **Privacy mode** `OMNI_PRIVACY_MODE={off, redact, local-only}` — 5 lớp PII redactor
- **Rate-limit + audit ring buffer** trên mọi LLM call

> "Một sản phẩm pitch được. Một codebase production-aware."

---

## 4:30 – 5:00 — Close

> "Omni không phải một lớp chat phủ lên Smart Banking.
> Omni là một **stack NLU ngân hàng tiếng Việt, safety-first, 27 tính năng**,
> tất cả quality gate đang xanh — 386 pytest pass, 200/200 NLU corpus, 19/19 KB scenario.
>
> Chúng tôi viết tiếng Việt cho người Việt, có local-first RAG để không phụ thuộc cloud embedding,
> có LLM safety contract để không bao giờ LLM được tự xưng đã chuyển tiền,
> và có evidence thực nghiệm trên dữ liệu công khai để defend từng con số.
>
> Cảm ơn ban giám khảo."

*(Slide cuối: logo Omni + 3 số 0.81 / 0.74 / 386 + QR repo.)*

---

## Cheat sheet 1 dòng (cho người dẫn slide bấm chuyển)

| Mốc | Slide | Câu khoá |
|---|---|---|
| 0:00 | Hook | "7 bước → 2 bước" |
| 0:15 | Demo cơ bản | "Chuyển cho mẹ 2 triệu" |
| 0:45 | 3 differentiator | "alias / temporal / ambiguity" |
| 1:30 | Safety | "3 cờ, deterministic, LLM không viết money facts" |
| 2:15 | Evidence | "BankSim 0.81 · Czech 0.74 · pre-registered" |
| 3:00 | Bonus | "VietQR scan + voice" |
| 3:45 | Scale | "56 routes / 386 tests / k8s probes" |
| 4:30 | Close | "27 features, all green, defendable" |

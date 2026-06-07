# Omni — Kịch bản DEMO 4 phút (lời nói sẵn, đã verify live)

> Tổng slot 8 phút = ~4 phút thuyết trình + **4 phút demo** (phần dưới).
> Mọi câu lệnh & output đã test trực tiếp trên container đang chạy. Cột
> **NÓI** là lời đọc thẳng. Người gõ + người nói tách vai, tập 2 lượt là mượt.

---

## Trước khi lên (30 giây chuẩn bị)

```powershell
docker rm -f omni-banking; docker run -d --name omni-banking --env-file backend/.env -p 8000:8000 omni-banking   # reset DB sạch
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:8000   # giữ chạy → URL chia sẻ
```
Mở URL trên máy demo **và** chiếu QR/link để giám khảo tự vào điện thoại. Cấp sẵn quyền **camera + mic**.

**Luật vàng:** sau mỗi lệnh chuyển phải **Xác nhận** hoặc gõ **`huỷ`** mới làm lệnh mới (Omni giữ 1 giao dịch đang chờ).

---

## KỊCH BẢN 4 PHÚT (5 nhịp — thứ tự đã tối ưu)

### ① 0:00–0:35 — Lời hứa: Chat → Confirm → Done
**Gõ:** `Chuyển cho mẹ 2 triệu`  → bấm **Xác nhận** → (nếu hỏi OTP) `123456`

> **NÓI:** "App ngân hàng hiện tại mất 7 bước để chuyển tiền. Omni còn 1 câu.
> Chỉ nói *'chuyển cho mẹ 2 triệu'* — nó tự hiểu **mẹ là Nguyễn Thị Lan**, tự
> điền số tiền, tự chọn tài khoản nguồn. Mình bấm xác nhận là xong."

*(Sau khi xong, gõ nhanh)* `đổi sang 3 triệu` *(làm trước khi confirm ở lần khác)*
> **NÓI:** "Và nói tiếp được — *'đổi sang 3 triệu'* — nó **sửa tại chỗ**, không tạo lệnh mới."

### ② 0:35–1:05 — Hiểu người Việt: trùng tên + không cần gõ dấu
**Mở Danh bạ (icon) →** chỉ vào dòng **"Xếp theo gợi ý hôm nay"**.

> **NÓI:** "Danh bạ tự **xếp hạng ai bạn hay chuyển *hôm nay*** — và **giải thích
> được**: *'thường chuyển ngày ~5 hàng tháng'*, *'4/6 lần trước vào cuối tuần'*.
> Đây là model cây học từ lịch sử, không phải xếp theo bảng chữ cái."

**Gõ:** `chuyen cho Minh 500k` *(cố tình không dấu)*

> **NÓI:** "Em gõ **không dấu** — vẫn hiểu. Và có **3 người tên Minh**, nó không
> đoán bừa mà **hỏi đúng người cần hỏi**." → tap chọn **Trần Hoàng Minh** → gõ `huỷ`.

### ③ 1:05–2:00 — ⭐ Sinh trắc học theo Quyết định 2345 (điểm nhấn)
**Gõ:** `Chuyển cho mẹ 10 triệu` → **Xác nhận** → OTP `123456` → **camera mở → quét mặt** (xoay đầu theo khung oval)

> **NÓI:** "Theo **Quyết định 2345 của Ngân hàng Nhà nước**, giao dịch từ 10
> triệu bắt buộc xác thực **sinh trắc học**. Omni làm đúng luật: dưới 10 triệu chỉ
> cần OTP, **từ 10 triệu tự bật quét khuôn mặt** — liveness chống ảnh chụp, nhận
> diện chạy **ngay trong trình duyệt**, không gửi mặt lên server. Tụi em còn cộng
> dồn các lệnh nhỏ trong ngày: chạm mốc 20 triệu cũng bắt quét mặt — đúng tinh
> thần nghị định."

### ④ 2:00–2:45 — ⭐ Bức tường an toàn (nhanh mà KHÔNG ẩu)
**Gõ:** `Chuyển 50 triệu cho Hùng STK 9990001234`

> **NÓI:** "Người lạ + số tiền lớn + vượt số dư → Omni dựng **3 lớp cờ** cùng lúc:
> người nhận mới, số tiền cao gấp ~27× thường ngày, và **không đủ số dư → chặn
> hẳn**. Đây là **rule engine deterministic**, audit được — không phải AI đoán.
> Và quan trọng: **AI được phép an ủi, nhưng KHÔNG bao giờ được tự viết dòng 'đã
> chuyển tiền'** — dòng đó chỉ do hệ thống sinh sau khi giao dịch thật xảy ra."

### ⑤ 2:45–3:20 — Trí nhớ chi tiêu: tự đào khoản định kỳ
**Gõ:** `Mình có khoản nào trả đều hàng tháng không?`

> **NÓI:** "Người dùng chưa khai báo lịch nào. Omni **tự đào** ra từ lịch sử: thuê
> nhà, gym, Spotify, Netflix. Và đây là số **thật** — trên dữ liệu ngân hàng công
> khai Czech 1 triệu giao dịch, detector này đạt **F1 = 0.74**. Tụi em không bịa số."

### ⑥ 3:20–4:00 — Bắn nhanh độ rộng + chốt
**Gõ lần lượt (mỗi câu ~8s):**
- `STK của tôi là gì?` → khoe tài khoản
- `Tạo QR nhận 500k` → hiện **mã QR nhận tiền**
- `Tháng này tôi tiêu ăn uống bao nhiêu?` → lọc theo **danh mục**

> **NÓI (chốt):** "Số dư, QR nhận tiền, chi tiêu theo danh mục, ngân sách, mục
> tiêu tiết kiệm, tìm ATM, đặt lịch — tất cả trong một khung chat tiếng Việt. Và
> ngay bây giờ, **mọi người cầm điện thoại đều vào được link này để tự thử**."

---

## 🔫 Kho bắn nhanh (nếu dư thời gian / giám khảo yêu cầu) — đã verify

| Gõ | Ra gì |
|---|---|
| `Số dư còn bao nhiêu?` | Số dư chính + tổng các tài khoản |
| `Tạo QR nhận 500k` | Ảnh QR (VietQR-style) + STK + tên chủ |
| `STK của tôi là gì?` | Liệt kê tài khoản + chủ thể |
| `Tháng này tiêu ăn uống bao nhiêu?` | Lọc lịch sử theo danh mục |
| `Đặt ngân sách ăn uống 3 triệu` | Tạo ngân sách hàng tháng |
| `Mình muốn tiết kiệm 10 triệu cho Tết` | Mục tiêu tiết kiệm + tiến độ |
| `ATM gần nhất` | 5 cây ATM (Haversine) |
| `Đặt lịch chuyển mẹ 2tr mùng 1 hàng tháng` | Cron tiếng Việt + lần chạy kế |
| `Có giao dịch nào bất thường không?` | Cảnh báo anomaly theo từng người |
| `Lúc nãy tôi chuyển cho ai?` | Recap giao dịch gần nhất |

---

## 🎨 Tính năng TRỰC QUAN (UI/UX) — demo bằng cách CHỈ TAY, không cần gõ

Đây là phần em sót lần trước. Mỗi cái chỉ cần 5 giây chỉ vào màn hình:

| Tính năng | Ở đâu / làm gì | Câu chỉ tay |
|---|---|---|
| **Xếp theo gợi ý hôm nay** | Danh bạ tự xếp hạng người-nhận-hôm-nay + **lý do giải thích được** | "Model cây gợi ý ai bạn hay chuyển hôm nay, có giải thích." |
| **Chip gợi ý trên ô nhập** | `SuggestionStrip` — top người nhận + thanh độ tin cậy | "Chưa gõ gì đã gợi ý sẵn." |
| **Chip số tiền nhanh** | Gõ "chuyển mẹ" → hiện **100k/500k/1tr/2tr/5tr** | "Gõ thiếu số tiền, nó đưa chip chọn nhanh." |
| **Lặp lại lần trước** | `RepeatLastCTA` — "Lặp lại lần trước" + "Cùng số tiền, người khác" | "Một chạm lặp giao dịch quen." |
| **Thẻ giao dịch thông minh** | Chip **số tiền dự đoán** + % tin cậy + tooltip lý do, ✎ sửa tiền tại chỗ, **mini sổ phụ** 3 lần gần nhất, **biểu đồ cột bất thường**, chip danh mục, animation thành công | "Mọi thứ để quyết định nằm trên 1 thẻ." |
| **Sổ phụ theo người** | Trong thẻ: 3 giao dịch gần nhất tới đúng người này | "Bối cảnh ngay tại chỗ chuyển." |
| **Thẻ số dư** | `BalanceCard` — tổng + **sparkline chi tiêu 7 ngày** | — |
| **Thẻ lịch sử** | `HistoryCard` — 5 dòng + **tag màu danh mục** tự phân loại | — |
| **Thẻ lịch định kỳ** | `ScheduleCard` — **cron tiếng Việt** + đếm ngược lần chạy kế | — |
| **Widget sidebar** | Insights / Recurring / Budget / Goals — **tự refresh khi xác nhận** | — |
| **Lịch sử hội thoại** | Drawer trái: liệt kê / mở lại / xoá cuộc trò chuyện cũ (lưu bền) | — |
| **Giọng nói + Đọc trả lời** | Nút mic (vi-VN) nhập bằng giọng; bật TTS đọc câu trả lời | "Nói được, nghe được — rảnh tay." |
| **Quét QR bằng camera** | `QrScanButton` — quét QR người nhận điền sẵn lệnh chuyển | — |
| **Slash + @-mention** | Gõ `/` ra bảng lệnh; `@` chọn người; Cmd+K / Cmd+/ | "Phím tắt như app pro." |
| **Toast realtime** | WebSocket bắn thông báo sự kiện sống | — |
| **Onboarding + khám phá kỹ năng** | Overlay 4 bước + 13 chip kỹ năng × 5 nhóm | "Người mới mở lên là biết hỏi gì." |
| **Xuất sao kê** | `ExportMenu` — CSV / sao kê HTML / JSON khai thuế | — |
| **Dashboard số liệu** | thêm `?metrics=1` vào URL → 7 chỉ số Prometheus sống | "Quan trắc sẵn sàng vận hành." |
| **Lớp telemetry dev** | thêm `?dev=1` → xem pipeline NLU chạy bên trong | — |
| **Tiếp cận (a11y)** | WCAG 2.1 AA, focus ring, tôn trọng `prefers-reduced-motion` | — |

> **Mẹo 4 phút:** chỉ cần chỉ tay vào **Danh bạ "gợi ý hôm nay"**, **chip gợi ý**,
> và **một thẻ giao dịch** — ba cái này gói được "sản phẩm có chiều sâu UX", phần
> còn lại để dành Q&A.

---

## 🧠 "Ngoại lệ khó đã xử lý" — thả vào lời nói hoặc Q&A để ghi điểm

- **Không dấu / sai chính tả:** "chuyen cho me 2 trieu" vẫn ra đúng.
- **Trùng tên:** 3 người "Minh" → hỏi lại, không đoán bừa.
- **Thứ tự ngược:** "gửi 300k sếp" (tiền trước, người sau) vẫn tách đúng.
- **Sửa giữa chừng:** "đổi sang 3 triệu" → sửa, không tạo lệnh mới.
- **Khoá khi chờ OTP:** đang nhập OTP mà gõ tên người khác → từ chối sửa, tránh đổi lén người nhận.
- **Chống giả định:** "thử chuyển mẹ 1k xem" → **không** mở lệnh chuyển (chặn câu phỏng đoán).
- **STK lạ (NAPAS):** chuyển vào STK chưa lưu → hỏi ngân hàng → tra tên người nhận rồi mới xác nhận.
- **Tuân thủ NHNN:** Quyết định 2345 (sinh trắc ≥10tr & dồn ngày ≥20tr) + hạn mức ngày theo eKYC/KYC + chống chuyển dồn dập (velocity) → bắt OTP.
- **An toàn ngôn ngữ:** LLM chỉ *diễn đạt*, **không** được khẳng định "đã chuyển X" — dòng tiền do code dựng.

---

## 🆘 Sự cố giữa demo

| Lỗi | Xử lý |
|---|---|
| Kẹt "giao dịch chưa hoàn tất" | Gõ `huỷ` hoặc bấm **Huỷ** |
| Số liệu lạ / số dư sai | Reset container (lệnh đầu trang) |
| Camera không khớp mặt | Người **đã đăng ký mặt** demo, hoặc thay 3 ảnh `frontend/public/face_profiles/u_an_*.jpg` trước |
| Link tunnel chết | Mở lại cloudflared → URL mới |
| LLM hết quota | "Coi là tính năng — rule fallback giữ hệ thống chạy, AI chỉ là cộng thêm" |

---

## Dữ liệu demo (đã verify)
- Số dư: TK chính **12.000.000đ**, tiết kiệm **50.000.000đ**.
- Alias: **mẹ**→Nguyễn Thị Lan (VCB) · **sếp**→Nguyễn Quốc Cường (BIDV) · **Minh**→3 người (gây disambiguation) · **Hùng**→Trần Quốc Hùng.
- Khoản định kỳ seed: thuê nhà 5tr · gym 1.2tr · Spotify 89k · Netflix 260k.

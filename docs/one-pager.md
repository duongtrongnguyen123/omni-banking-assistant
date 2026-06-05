# Omni — One-pager

**HACK\<CX\>TOGETHER · Đội: One Last Token**
*AI Banking Assistant tiếng Việt — Chat → Confirm → Done.*

---

## Pain

Smart Banking hôm nay cần **7 bước** để chuyển tiền: mở app → chọn chuyển khoản
→ chọn người → STK → số tiền → nội dung → OTP. Mỗi bước là một chỗ user bỏ
cuộc. Mà ~78% giao dịch là người quen lặp lại.

## Promise

Omni rút 7 bước còn **2**: nhập một câu tiếng Việt tự nhiên ("Chuyển mẹ 2 triệu"),
xác nhận một lần. Safety nguyên — không cắt — vì model hiểu đủ ý để **tự điền 5
bước giữa**.

## Ba điểm khác biệt tiếng Việt

1. **Alias** — "mẹ / bố / ny / sếp / cô bán bún chợ" → đúng contact bằng 5-bước
   resolver (exact → token → prefix → fastembed RAG).
2. **Temporal** — "như tháng trước" → tự fill nội dung và số tiền từ history;
   số tiền user nói luôn override.
3. **Ambiguity** — 2 Minh trong danh bạ → hỏi đúng một câu, không hỏi lại số tiền.

Tất cả gắn với một **LLM safety contract** cốt lõi: LLM được phrase empathy,
**không bao giờ** được tự xưng "đã chuyển X cho Y" — dòng đó chỉ do code build
sau khi transfer thực sự xảy ra.

## Ba con số defendable

| Số | Ý nghĩa | Nguồn |
|---|---|---|
| **Hit@1 = 0.81** | Suggester predict đúng recipient kế tiếp | BankSim 594k tx (real merchant labels) |
| **F1 = 0.74** | Recurring payment detector | Czech PKDD'99 (real bank, ground-truth permanent_orders) |
| **386 / 0** | pytest pass / fail (+10 xfail có chủ đích) | `make verify` — 6.2s |

Pre-registered seed = 42, n_users = 20 (`docs/eval-protocol.md`). Không cherry-pick.

## Call to action

- Live demo: <http://localhost:5173> (`make backend` + `make frontend`)
- Pitch script 5 phút: [`docs/pitch-final.md`](pitch-final.md)
- Crib sheet trung thực: [`docs/honest-pitch.md`](honest-pitch.md)
- Q&A buffer: [`docs/judge-faq.md`](judge-faq.md)

> *Hiểu tiếng Việt. Giữ an toàn. Không bịa số.*

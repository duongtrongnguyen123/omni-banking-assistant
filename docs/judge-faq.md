# Omni — Judge FAQ (20 câu hỏi · 20 câu trả lời sắc)

Mỗi câu trả lời **dưới 60 từ**. Mục tiêu là trả lời gọn, có số, không né.

---

## 1. "Tại sao không dùng deep learning cho suggester?"

Vì honesty quan trọng hơn flex. Dataset BTC không có tín hiệu thời gian học được — deep model sẽ memorize chặt hơn rồi fail trên hold-out. Tree + rule giúp **giải thích được** ("mẹ rank cao hôm nay vì gần ngày mùng 1 + tần suất + recency"). Trên BankSim ta vẫn đạt Hit@1 = 0.81.

## 2. "Điểm thực sự novel của các bạn là gì?"

Ba thứ, xếp theo độ defend được:
1. **LLM safety contract** — LLM được phrase, không được assert money facts. Hầu hết chat-banking demo vi phạm điểm này.
2. **Multi-turn modify** — "đổi sang 3 triệu" là edit, không phải lệnh mới.
3. **Local-first RAG** — fastembed + SQLite, embedding không gửi cloud.

## 3. "Chi phí vận hành bao nhiêu?"

Embedding **local**, RAG **local**, suggester **local** (sklearn RF). LLM chỉ gọi cho phrasing và có rule fallback. Trong test, hit rate-limit 100k token/ngày của Groq — hệ thống vẫn chạy. Chi phí biên ≈ 0 cho mỗi user nếu tắt LLM phrasing.

## 4. "Nếu Groq down thì sao?"

Hệ thống có **3 tầng**: Groq → Gemini (OpenAI-compat) → rule-based extractor. Suite NLU 200 utterance pass **không cần LLM** — chứng minh rule layer đứng vững một mình. LLM là quality boost, không phải dependency.

## 5. "Production-ready chưa?"

Chưa, và chúng tôi nói thẳng. Session in-memory hoặc Redis (optional), OTP mock `123456`, không có rail ngân hàng thật. **Nhưng** interface (`requires_step_up`, `awaiting_otp`, Redis session backend) đã clean — swap real service ≈ nửa ngày mỗi cái.

## 6. "Sao làm được 27 feature trong 1 ngày?"

Worktree song song + dispatching agents. Mỗi feature có owner branch + smoke test riêng. Merge gate là `make verify` (45s). 354 test pass + 19/19 KB assertion + 200/200 NLU corpus — tất cả gate xanh trước khi pitch. Chi tiết: `docs/branch-status.md`.

## 7. "Hệ thống chịu được 1M users không?"

Hiện tại P50 < 50ms / P95 < 250ms ở 520k tx single-instance (`docs/perf.md`). Để scale: Redis session backend đã sẵn (multi-instance), k8s probes đã sẵn, suggester có composite SQL index (500× speedup). Bottleneck cuối là Postgres write thay vì SQLite — chuyển driver, không đổi kiến trúc.

## 8. "OTP thật tích hợp như nào?"

Hợp đồng đã wire: `requires_step_up: bool` + `awaiting_otp` state. Khi safety rule warn → `awaiting_otp=true`, user nhập code → handler so sánh. Swap mock `123456` bằng Twilio Verify / nhà mạng VN ≈ 4 giờ. Không đụng business logic.

## 9. "Xử lý PII thế nào?"

`OMNI_PRIVACY_MODE` 3 mức: `off / redact / local-only`. Redactor 5 lớp (phone, STK, email, CMND, fullname). LLM audit ring buffer log mọi prompt/response. Ở mức `local-only`, không request nào ra ngoài máy. Detail: `docs/privacy.md`.

## 10. "Sao lại làm tiếng Việt specific?"

Vì tiếng Việt có 3 đặc thù tạo pain thật: (a) diacritic / no-diacritic / typo (extractor phải tolerant), (b) alias xã hội thay vì tên ("mẹ", "ny", "cô bán bún"), (c) số tiền dạng "5 chai", "2tr", "ba trăm nghìn". Một stack EN-first không xử được những thứ này native.

## 11. "Train trên dataset nào?"

Suggester sklearn RF — train **per-user** trên history của chính user đó (không cross-user leakage). Eval headline trên **BankSim 594k tx** (merchant labels công khai, không phải pattern của chúng tôi). Recurring detector eval trên **Czech PKDD'99** (real bank, ground-truth `permanent_orders`). Synthetic chỉ dùng cho sanity check.

## 12. "Sao phải pre-register eval?"

Để tránh hindsight tuning. Seed = 42, n_users = 20, no hyperparameter search — pin trong `docs/eval-protocol.md` **trước khi** chạy. Nếu sau này có dataset thật user hơn, ta chạy lại đúng protocol đó. Đây là khoa học cơ bản, không phải performance theatre.

## 13. "Catch lớn nhất của project là gì?"

Hai cái, nói thẳng:
1. Suggester Hit@K trên dataset BTC ≈ random — dataset không có signal thời gian. Defendable number là **BankSim 0.81**, không phải synthetic 0.89.
2. Recall fraud 0.75 ở FP-rate 11% — đủ làm **OTP step-up signal**, **không đủ** auto-block. Chúng tôi pitch đúng vậy.

## 14. "RAG fuzzy contact lookup hoạt động sao?"

5 bước: exact match → token match → prefix → embedding (fastembed MiniLM 384-d) → fallback. Embedding backfill ở startup, lưu blob SQLite. Khi user nói "cô bán bún chợ" — token + embedding kết hợp, không cần exact name. Local hoàn toàn, không cloud.

## 15. "Multi-turn modify khác continuation thông thường ra sao?"

Không có concept "lệnh mới khi user nói tiếp". Orchestrator (`services/orchestrator.py:handle_message`) check **continuation path** trước — nếu có `pending_transfer` trong session + utterance có modify keyword ("đổi", "thay", "không, …"), patch draft thay vì spawn intent mới. State machine, không phải LLM judgment.

## 16. "Voice input dùng cloud STT không?"

Không. Web Speech API native (`vi-VN`) — chạy trên browser, không gửi audio ra cloud. TTS reply cũng native (`speechSynthesis`). Đây là một phần của triết lý local-first: NLU + RAG + STT + TTS đều có path không cloud.

## 17. "Recurring detector hoạt động kiểu gì? Sao F1 = 0.74?"

Mine history bucket theo `(year, month)`, group theo (counterparty, amount band). Pattern xuất hiện ≥ N tháng liên tiếp → đề xuất "định kỳ". Filter noise (`ok`, `test`, `asdf` trong contest data). Trên Czech PKDD'99 real `permanent_orders`: P=0.69 / R=0.80 / F1=0.74 trên 25 ground-truth order.

## 18. "Sao cờ ambiguity lại block một cách dứt khoát?"

Vì 2 người tên "Minh" mà guess sai = chuyển nhầm tiền = mất tiền user. Block + hỏi lại an toàn hơn guess + apologize. Đây là design choice — **safety > UX shortcut**. Nếu user dùng full name hoặc bank account hint thì rule bypass disambiguation tự động.

## 19. "Demo offline được không?"

Được. `docs/offline-demo.md` mô tả mode tắt LLM (`OMNI_LLM_DISABLE=1`) + skip embedding backfill (`OMNI_SKIP_EMBED_BACKFILL=1`). 8 KB scenario trong `backend/scripts/smoke.py` chạy hết với LLM tắt. Demo recorder + telemetry overlay cũng có sẵn cho video pre-record nếu mạng lỗi tại venue.

## 20. "Roadmap tiếp theo?"

Ngắn hạn (1 tuần): swap Redis session full + OTP service thật + persistent draft TTL. Trung hạn (1 tháng): real user history → re-run pre-registered eval với data thật, fine-tune Vietnamese intent classifier. Dài hạn: per-user Isolation Forest fraud (đã train xong, đang merge), bilingual VI/EN toggle (branch sẵn).

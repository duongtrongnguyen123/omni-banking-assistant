# Omni MVP - Phan Tich 11 Van De Mentor Gop Y

## Tong Ket Nhanh

Kien truc hien tai khop kha sat voi so do 5 lop:

```text
Frontend React
-> HTTP/WebSocket
-> Backend FastAPI
-> Orchestrator: services/orchestrator.py
-> NLU / Context / Safety / Banking
-> Store in-memory + JSON seed
```

Khac biet voi production:

- Redis chua co, hien dang dung session in-memory.
- PostgreSQL chua co, hien dang dung JSON seed + in-memory store.
- RAG chua co.
- LLM Groq/Gemini co module nhung optional; khong co API key thi rule-based chay chinh.
- Safety da co rule engine, OTP, chon tai khoan nguon.
- Banking layer la mock banking service.

Cau nen noi voi mentor/BGK:

```text
Kien truc MVP hien tai bam theo 5 lop trong proposal. Cac thanh phan production nhu Redis/PostgreSQL/RAG/core banking API dang duoc thay bang in-memory store va mock banking service de phuc vu hackathon, nhung module boundary da tach ro de thay the sau.
```

---

## 1. UI/UX Chuyen Khoan Con Luong Cuong

### Danh gia

Phu hop va rat nen lam. San pham khong thang chi vi co NLP, ma phai lam nguoi dung cam thay de hon app ngan hang truyen thong.

### Van de hien tai

- Card giao dich co nhieu thong tin nhung chua ke mot luong ro rang.
- Khi co risk/block, user co the khong biet can lam gi tiep.
- Chua hien ro Omni da kiem tra nhung gi.

### Huong giai quyet kha thi

Sap xep card theo thu tu:

```text
Nguoi nhan
Tai khoan nguon
So tien
Noi dung
Canh bao/rui ro
Xac thuc can thiet
Nut hanh dong
```

Them audit mini trong UI:

```text
Omni da kiem tra:
- Nguoi nhan: hop le
- Tai khoan nguon: du/khong du so du
- So tien: binh thuong/lon
- Rui ro: can OTP/can sinh trac hoc
```

### Muc uu tien

P0 - nen lam ngay.

---

## 2. Xac Nhan Theo Rui Ro Giao Dich

### Danh gia

Phu hop va rat quan trong voi banking.

### Flow de xuat

```text
Giao dich thuong:
OTP hoac sinh trac hoc

Giao dich rui ro/cao tien:
OTP + sinh trac hoc
```

### Trang thai MVP hien tai

- Da co OTP demo `123456`.
- Da co flag `large_amount`, `new_recipient_large_amount`, `insufficient_balance`.
- Chua co sinh trac hoc that.

### Huong giai quyet kha thi

Lam mock biometric:

```text
User bam "Quet khuon mat"
-> Modal hien "Dang xac minh..."
-> "Xac minh thanh cong"
-> Sau do moi cho chuyen
```

Khong nen tich hop camera/sinh trac hoc that neu thoi gian gap.

### Muc uu tien

P0 neu lam mock biometric. Sinh trac hoc that de P2.

---

## 3. Kiem Soat AI Voi Nhieu "Me", Nhieu "Em"

### Danh gia

Phu hop. Hien da co ambiguous recipient voi `Minh`, nhung can mo rong alias mo ho.

### Rule dung

```text
Neu alias/name match 1 contact -> tao draft.
Neu alias/name match nhieu contact -> hoi lai.
Neu khong match contact nao -> hoi them thong tin.
```

### Vi du can demo

```text
Chuyen cho em 2 trieu
```

Neu co nhieu contact alias `em`, Omni hoi:

```text
Ban muon chuyen cho em nao?
- Le Thi Thao
- Nguyen Minh Anh
```

### Huong giai quyet kha thi

- Them mock data co nhieu alias mo ho: `em`, `anh`, `me`.
- Nang resolver trong `context/alias.py`.
- Khong de LLM tu chon nguoi nhan khi co ambiguity.

### Muc uu tien

P0/P1 tuy thoi gian. Neu demo voi mentor ngan, uu tien Minh truoc.

---

## 4. Khi AI Chua Hieu Thi Tuong Tac Lai Nhu Nao

### Danh gia

Rat phu hop. Day la diem phan biet san pham an toan voi chatbot biet bia.

### Nguyen tac

```text
Khong bia.
Thieu slot nao hoi dung slot do.
Mo ho thi hoi lai.
Khong chac thi khong execute.
```

### Vi du

User:

```text
Chuyen cho me
```

Omni:

```text
Ban muon chuyen bao nhieu cho Nguyen Thi Lan?
```

User:

```text
Chuyen 2 trieu
```

Omni:

```text
Ban muon chuyen 2.000.000d cho ai?
```

User:

```text
Chuyen cho Minh
```

Omni:

```text
Ban muon chuyen bao nhieu va cho Minh nao?
```

### Huong giai quyet kha thi

- Them missing-slot response trong orchestrator.
- Luu draft dang thieu thong tin trong session.
- Khi user tra loi tiep, merge thong tin vao draft cu.

### Muc uu tien

P0, vi day la core banking safety.

---

## 5. Risk-Based Confirmation Va Hanh Dong Khi Bi Canh Bao

### Danh gia

Phu hop. Khi chi co warning thi van nen cho user tiep tuc voi xac thuc manh hon. Khi co block that su thi phai bat user sua/huy.

### Phan loai

```text
WARN:
- So tien lon
- Nguoi nhan moi
- Amount anomaly
=> Van cho tiep tuc neu xac thuc du.

BLOCK:
- Thieu nguoi nhan
- Thieu so tien
- Nguoi nhan mo ho
- Tai khoan nguon khong du so du
=> Khong execute, phai sua/chon lai/huy.
```

### Trang thai MVP hien tai

- Da co `warn` va `block`.
- Da co chon tai khoan nguon.
- Neu tai khoan chinh khong du, user co the chon tai khoan phu.

### Huong giai quyet kha thi

- UI phan biet ro `Can xac thuc them` voi `Khong the thuc hien`.
- Warning van hien nut xac thuc.
- Block hien action cu the: "Chon tai khoan khac" / "Huy".

### Muc uu tien

P0.

---

## 6. Tach Biet LLM Va Rule

### Danh gia

Rat phu hop. Day la diem can noi ro khi pitch.

### Rule dung cho

- Parse tien.
- Parse STK.
- Confirm/cancel/OTP.
- Safety rules.
- So du.
- Nguoi nhan mo ho.
- Execute transaction.
- Audit log.

### LLM dung cho

- Hieu cau noi tu nhien da dang.
- Follow-up phuc tap:
  - "the con thang truoc?"
  - "doi sang nguoi kia"
  - "it hon chut"
- Dien dat cau tra loi tu nhien.
- Normalize transcript speech.

### Nguyen tac

```text
LLM khong duoc quyen tu chuyen tien. LLM chi de xuat intent/entities. Rule engine va banking service moi quyet dinh giao dich co duoc di tiep khong.
```

### Muc uu tien

P0 ve mat trinh bay. P1 neu them UI/audit hien `NLU source: rule|llm`.

---

## 7. RAG, DB, Redis, Memory

### Danh gia

Phu hop, nhung khong nen ap dung RAG sai cho core transfer.

### RAG nen dung o dau

- FAQ san pham ngan hang.
- Bieu phi.
- Han muc.
- Chinh sach xac thuc.
- Dieu khoan san pham.
- Huong dan nghiep vu.
- Compliance/policy explanation.

### RAG khong nen dung de quyet dinh

- Co duoc chuyen tien khong.
- Nguoi nhan la ai.
- So tien bao nhieu.
- Co du so du khong.

### Production storage

```text
PostgreSQL:
- users
- accounts
- contacts
- transactions
- schedules
- audit logs

Redis:
- session ngan han
- current draft
- conversation state

Object storage:
- audio file
- voice transcript
- audit artifact neu can

Vector DB/RAG:
- tai lieu san pham
- policy
- FAQ
- huong dan nghiep vu
```

### Muc uu tien

P2 implement. P0 trinh bay ro trong architecture.

---

## 8. Speech Support

### Danh gia

Phu hop, nhung khong nen de speech thanh rui ro live demo neu chua on dinh.

### Flow tot

```text
User bam mic
-> noi cau lenh
-> speech-to-text
-> hien transcript cho user xem
-> user bam gui/xac nhan
-> Omni xu ly nhu text
-> tra text + optional voice
```

### Cong cu can benchmark

- Whisper/OpenAI.
- Google Speech-to-Text.
- Azure Speech.
- FPT.AI/Zalo AI neu muon tieng Viet local hon.
- Browser Web Speech API cho demo nhanh.

### Huong MVP

- Neu kip: dung browser Web Speech API.
- Neu khong kip: demo concept bang transcript text.
- Khong de speech lam hong demo core transfer.

### Muc uu tien

P1/P2. Lam sau UX, safety, audit.

---

## 9. Ho Tro Nguoi Lon Tuoi

### Danh gia

Phu hop. Khong chi la UI don gian, ma la giam tai thao tac va tang do tin cay.

### Huong giai quyet

- It thao tac hon: noi tu nhien thay vi di menu.
- Chu lon, card ro, nut it.
- Hoi lai khi mo ho, khong bat nho thuat ngu banking.
- Doc lai giao dich truoc khi xac nhan.
- OTP/sinh trac hoc quen thuoc.
- Voice response neu lam speech.

### Flow xac thuc de xuat

```text
Giao dich thuong:
OTP hoac sinh trac hoc

Giao dich nhieu tien/rui ro:
OTP + sinh trac hoc
```

### Muc uu tien

P1 cho UI. P2 cho voice/biometric that.

---

## 10. Audit, Logs, Observability

### Danh gia

Rat quan trong voi banking. Can tra loi duoc: con AI nay da lam gi, dua vao dau, co an toan khong.

### Audit event nen co

```json
{
  "message": "...",
  "nlu_source": "rule|llm",
  "intent": "transfer",
  "entities": {},
  "resolved_recipient": "...",
  "safety_flags": [],
  "selected_account": "...",
  "auth_required": ["otp", "biometric"],
  "decision": "draft_created|blocked|executed|needs_clarification"
}
```

### Huong MVP

- UI mini: "Omni da kiem tra".
- Backend luu audit event in-memory/JSON.
- Co endpoint `/api/audit` neu kip.
- Admin view co the de sau.

### Muc uu tien

P0/P1. Rat dang lam vi tang do tin cay.

---

## 11. Can Bang Hieu Nang Va Do Chinh Xac

### Danh gia

Phu hop. Day la cau chuyen he thong tot.

### Policy de xuat

```text
Tac vu rui ro thap:
Rule-based nhanh.

Tac vu chuyen tien:
Rule + safety bat buoc.

Cau mo ho:
Hoi lai, khong doan.

Cau follow-up phuc tap:
Dung LLM neu co.

Thong tin policy/san pham:
Dung RAG neu trien khai.
```

### Cau pitch

```text
Team khong dung LLM cho moi thu. Team phan tang theo risk: cang gan tien that thi cang nhieu rule, validation, audit va xac thuc.
```

### Muc uu tien

P0 ve mat trinh bay; P1 neu co audit hien duoc decision path.

---

## Uu Tien Thuc Hien

### P0 - Nen Lam Ngay

1. UI giao dich de hieu hon.
2. Clarification khi thieu/mo ho.
3. Risk-based auth: thuong OTP, rui ro OTP + mock biometric.
4. Audit mini: Omni da hieu gi, da kiem tra gi, vi sao canh bao/chan.
5. Alias ambiguity: nhieu me/nhieu em/nhieu Minh.

### P1 - Neu Kip

6. LLM/rule indicator trong audit.
7. Admin/audit endpoint don gian.
8. Speech demo bang browser Web Speech hoac transcript mock.
9. Reset demo data button/script.

### P2 - Chi Noi Trong Pitch

10. RAG day du.
11. PostgreSQL/Redis that.
12. Sinh trac hoc that.
13. Benchmark speech day du.

---

## Cau Chot Nen Dung

```text
Omni khong phai chatbot. Omni la natural language transaction layer cho mobile banking: LLM chi la mot phan cua NLU, con gia tri chinh la orchestration co kiem soat giua context, safety, xac thuc va banking execution.
```

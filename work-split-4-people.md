# Omni MVP - Chia Viec Cho 4 Nguoi

## Nguyen Tac Chia Viec

Muc tieu la cho 4 nguoi lam song song, it dam chan nhau nhat co the.

Chia theo 4 truc:

```text
Nguoi 1: Frontend UI/UX + Demo Flow
Nguoi 2: Safety/Auth/Audit Backend
Nguoi 3: NLU/Context/Clarification + LLM Boundary
Nguoi 4: Data/Infra/Architecture/Pitch Support
```

---

## Nguoi 1 - Frontend UI/UX + Demo Flow

### Muc tieu

Lam trai nghiem demo de hieu, de dung, nhin ro rang hon app ngan hang truyen thong.

### Pham vi

- Giao dien chat.
- Transaction card.
- Schedule card.
- Warning/risk card.
- OTP modal.
- Mock biometric modal.
- UI clarification khi Omni hoi lai.
- Script demo thao tac tren frontend.

### Viec Cu The

#### 1. Polish Transaction Card

Sap xep lai thu tu hien thi:

```text
Nguoi nhan
Tai khoan nguon
So tien
Noi dung
Canh bao/rui ro
Xac thuc can thiet
Nut hanh dong
```

Can lam ro:

- Tai khoan nao dang duoc chon.
- Tai khoan co du tien khong.
- Can OTP hay biometric.
- Neu bi block thi user phai lam gi tiep.

#### 2. Polish Risk UI

Phan biet 2 loai:

```text
WARN:
Van duoc tiep tuc neu xac thuc du.

BLOCK:
Khong duoc execute, phai sua/chon lai/huy.
```

Vi du:

- `large_amount`: mau vang, van co nut xac thuc.
- `insufficient_balance`: mau do, yeu cau chon tai khoan khac.

#### 3. OTP Modal

Flow:

```text
Bam Xac nhan
-> hien OTP
-> nhap 123456
-> bam Xac minh & chuyen
```

#### 4. Mock Biometric Modal

Flow:

```text
Bam Xac minh sinh trac hoc
-> modal "Dang quet khuon mat..."
-> 1-2 giay sau "Xac minh thanh cong"
-> cho phep tiep tuc
```

Khong can camera that.

#### 5. Clarification UI

Khi Omni hoi lai:

```text
Ban muon chuyen bao nhieu?
Ban muon chuyen cho Minh nao?
```

Nen hien thanh card/nut chon neu co candidates.

### Deliverables

- UI demo muot cho 4-5 case.
- Transaction card de doc.
- Risk flow khong gay luong cuong.
- OTP + mock biometric co the demo.
- Danh sach cau lenh demo de copy/paste.

### Phu Thuoc

- Can backend tra flags/auth_required ro tu Nguoi 2.
- Can clarification response tu Nguoi 3.

---

## Nguoi 2 - Safety/Auth/Audit Backend

### Muc tieu

Dam bao giao dich banking co rule an toan, xac thuc theo rui ro, va audit duoc qua trinh quyet dinh.

### Pham vi

- `backend/app/safety/rules.py`
- auth policy
- OTP/biometric mock backend
- audit event
- block/warn decision
- account selection validation

### Viec Cu The

#### 1. Risk-Based Auth Policy

De xuat policy:

```text
Giao dich thuong:
- OTP hoac biometric

Giao dich > 10tr:
- OTP + biometric

Nguoi nhan moi + so tien lon:
- OTP + biometric

Tai khoan khong du so du:
- BLOCK, khong cho execute
```

#### 2. Safety Rules

Can co:

- `missing_recipient`
- `missing_amount`
- `ambiguous_recipient`
- `large_amount`
- `new_recipient_large_amount`
- `amount_above_average`
- `insufficient_balance`

Logic:

```text
WARN -> co the tiep tuc neu xac thuc du.
BLOCK -> phai sua thong tin/chon lai/huy.
```

#### 3. Account Source Validation

Khi user chon tai khoan nguon:

- Confirm account thuoc user.
- Check amount <= balance cua account do.
- Execute thi tru tien dung account.

#### 4. Audit Log

Tao event moi lan handle message/confirm:

```json
{
  "message": "...",
  "nlu_source": "rule|llm",
  "intent": "transfer",
  "entities": {},
  "resolved_recipient": "...",
  "selected_account": "...",
  "safety_flags": [],
  "auth_required": ["otp"],
  "decision": "draft_created"
}
```

#### 5. Endpoint Audit Neu Kip

Co the them:

```text
GET /api/audit
```

Hoac chi log trong memory/console cho MVP.

### Deliverables

- Safety rules dung va de giai thich.
- Risk auth policy chay duoc.
- Audit event toi thieu.
- Khong execute khi thieu/mo ho/khong du so du/chua du xac thuc.

### Phu Thuoc

- Nguoi 1 can flags/auth_required de render UI.
- Nguoi 4 can audit/policy story cho pitch.

---

## Nguoi 3 - NLU/Context/Clarification + LLM Boundary

### Muc tieu

Lam Omni hieu va hoi lai dung cach khi cau noi mo ho/thieu thong tin. Tach ro khi nao dung rule, khi nao dung LLM.

### Pham vi

- `backend/app/nlp/pipeline.py`
- `backend/app/nlp/intent.py`
- `backend/app/nlp/entities.py`
- `backend/app/context/alias.py`
- `backend/app/context/temporal.py`
- session/draft continuation
- clarification strategy

### Viec Cu The

#### 1. Clarification Theo Missing Slot

Vi du:

```text
User: Chuyen cho me
Omni: Ban muon chuyen bao nhieu cho Nguyen Thi Lan?
```

```text
User: Chuyen 2 trieu
Omni: Ban muon chuyen 2.000.000d cho ai?
```

```text
User: Chuyen cho Minh
Omni: Ban muon chuyen bao nhieu va cho Minh nao?
```

#### 2. Ambiguous Alias

Mo rong ngoai Minh:

- `me`
- `em`
- `anh`
- `chi`

Rule:

```text
1 match -> proceed
multiple matches -> ask user
0 match -> ask for recipient/account
```

#### 3. Follow-Up Handling

Ho tro:

```text
Doi sang 3 trieu
Chon nguoi kia
Huy giao dich
Xac nhan
123456
```

#### 4. LLM vs Rule Boundary

Lam ro trong code/comment/pitch:

Rule dung cho:

- amount
- STK
- confirm/cancel
- safety
- execute

LLM dung cho:

- cau noi da dang
- follow-up phuc tap
- phrasing
- speech transcript normalization

#### 5. NLU Source Tracking Neu Kip

Neu `llm_understand` tra ket qua:

```text
nlu_source = "llm"
```

Neu fallback:

```text
nlu_source = "rule"
```

### Deliverables

- Hoi lai dung khi thieu slot.
- Alias ambiguity chuan hon.
- Follow-up khong bi vo flow.
- Tai lieu ngan: khi nao dung LLM, khi nao dung rule.

### Phu Thuoc

- Nguoi 2 can output NLU/context de audit.
- Nguoi 1 can response/card de render clarification.

---

## Nguoi 4 - Data/Infra/Architecture/Pitch Support

### Muc tieu

Lam demo data sach, architecture story ro, va chuan bi cau tra loi cho mentor/BGK.

### Pham vi

- JSON seed data.
- Architecture diagram.
- MVP vs Production mapping.
- RAG/DB/Redis story.
- Speech benchmark plan.
- Pitch/Q&A notes.

### Viec Cu The

#### 1. Clean Demo Data

Kiem tra:

- User co 2 tai khoan:
  - main khong du cho 50tr
  - savings du cho 50tr
- Contacts co:
  - me
  - Minh MB
  - Minh TCB
  - Hung moi/it giao dich
  - alias mo ho neu can
- Transactions co:
  - giao dich voi me
  - giao dich voi Minh TCB de chung minh da tung giao dich
  - giao dich voi Minh MB

#### 2. Them Data Cho Ambiguous Alias Neu Can

Vi du:

```text
Le Thi Thao alias: em
Nguyen Minh Anh alias: em
```

De demo:

```text
Chuyen cho em 1 trieu
```

#### 3. Architecture Diagram Update

So do nen co:

```text
Frontend React
Backend FastAPI
Orchestrator
NLU
Context
Safety/Auth
Banking Service
Store
Audit
```

Danh dau:

```text
MVP: JSON/in-memory
Production: PostgreSQL/Redis/Core banking API/RAG
```

#### 4. MVP vs Production Table

Vi du:

| Layer | MVP | Production |
|---|---|---|
| Session | in-memory | Redis |
| Data | JSON seed | PostgreSQL |
| Banking | mock service | core banking API/sandbox |
| NLU | rule + optional LLM | monitored hybrid NLU |
| Knowledge | none/manual | RAG over product/policy docs |
| Auth | OTP demo/mock biometric | OTP/soft token/biometric service |

#### 5. RAG Strategy

Noi ro:

```text
RAG dung cho FAQ, bieu phi, han muc, policy.
RAG khong dung de quyet dinh giao dich hay so du.
```

#### 6. Speech Benchmark Plan

Lap bang benchmark:

| Tool | Diem manh | Rui ro | Dung cho MVP? |
|---|---|---|---|
| Browser Web Speech | nhanh demo | phu thuoc browser | co the |
| Whisper/OpenAI | tot | can API/network | de sau |
| Google STT | on dinh | setup | de sau |
| Azure Speech | enterprise | setup | de sau |
| FPT/Zalo AI | tieng Viet | can key | can danh gia |

### Deliverables

- Data demo sach.
- Architecture diagram.
- MVP vs Production table.
- Rule vs LLM vs RAG table.
- Q&A notes cho mentor/BGK.

### Phu Thuoc

- Can Nguoi 2 cho safety/audit story.
- Can Nguoi 3 cho LLM/rule story.
- Can Nguoi 1 cho final demo script.

---

## Lich Lam Song Song De Xuat

### Dot 1 - 2 Gio Dau

- Nguoi 1: polish card + OTP/biometric UI.
- Nguoi 2: safety/auth policy + audit skeleton.
- Nguoi 3: clarification + alias ambiguity.
- Nguoi 4: clean data + architecture doc.

### Dot 2 - 2 Gio Tiep Theo

- Nguoi 1: lap demo flow + fix UI bug.
- Nguoi 2: test risk cases.
- Nguoi 3: test NLU/follow-up cases.
- Nguoi 4: update pitch/Q&A.

### Dot 3 - Final Integration

Ca team test 5 demo case:

```text
1. Chuyen cho me 2 trieu tien sinh hoat
2. Gui cho me 5 trieu nhu thang truoc
3. Chuyen cho Minh 500k
4. Chuyen 50 trieu cho Hung STK 9990001234
5. Dat lich chuyen me 2tr vao mung 1 hang thang
```

Neu kip them:

```text
6. Chuyen cho em 1 trieu
7. Chuyen cho me
8. Chuyen 2 trieu
```

---

## Definition Of Done

San pham san sang demo khi:

- Backend khong dung `--reload` trong luc demo.
- Frontend refresh khong bi loi.
- 5 demo case chay duoc.
- Giao dich rui ro co warning va xac thuc hop ly.
- Giao dich thieu/mo ho khong execute.
- Chon tai khoan nguon hoat dong.
- OTP demo hoat dong.
- Co cau tra loi ro cho:
  - LLM dung o dau?
  - Rule dung o dau?
  - RAG dung o dau?
  - Vi sao mock data van hop ly?
  - AI co tu chuyen tien khong?

---

## Cau Chot Cho Team

```text
Uu tien cua chung ta khong phai them that nhieu feature. Uu tien la lam cho BGK thay ro: Omni de dung hon flow truyen thong, an toan hon chatbot binh thuong, va co kien truc du de mo rong thanh he thong ngan hang that.
```

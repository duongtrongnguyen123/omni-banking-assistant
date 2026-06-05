# Omni MVP - Ma Trận Gap Compliance Ngân Hàng

> Mục tiêu file này: giúp team bám sát yêu cầu ngân hàng, nhưng không nhầm MVP hackathon với hệ thống production đã đạt compliance. Đây là tài liệu định hướng sản phẩm/kỹ thuật, không phải tư vấn pháp lý.

## Hiểu Đúng Vấn Đề

Không phải là mình không bám được flow compliance bạn gửi.

Mình bám theo hướng này:

```text
MVP: chứng minh kiến trúc có boundary đúng, AI không tự chuyển tiền.
Production: cần thêm compliance controls, governance, evidence, audit bất biến, KYC/AML, mã hóa, vận hành ngân hàng thật.
```

Nói cách khác:

```text
MVP không cần implement hết PCI/SOC2/ISO/KYC/AML/SBV.
Nhưng MVP phải biết phần nào đang thiếu, vì sao thiếu, và production sẽ thay thế bằng gì.
```

## Câu Chốt Cho Pitch

```text
Omni MVP không claim đã đạt chuẩn compliance ngân hàng. MVP chứng minh luồng kiểm soát: AI chỉ hiểu ý định và tạo draft; các lớp Safety, Auth, Audit và Banking Service mới quyết định giao dịch có được đi tiếp hay không. Khi triển khai thật, các lớp mock/in-memory sẽ được thay bằng core banking API, KYC/AML engine, audit bất biến, mã hóa/KMS và quy trình compliance/evidence đầy đủ.
```

## Ma Trận Tổng Quan

| Mảng | MVP Hiện Tại | Production Cần Có | Kết Luận |
|---|---|---|---|
| PCI DSS | Chưa xử lý thẻ/PAN/card payment thật. | Nếu thêm thẻ/PAN: xác định PCI scope, CDE, segmentation, tokenization/PAN masking, vulnerability scanning, ASV scan, pentest, MFA, FIM, log review, SAQ/ROC/AOC. | Hiện chưa phải trọng tâm MVP. Cần nói rõ “out of current scope”. |
| SOC 2 | Có audit demo nhưng chưa có evidence/control program. | Control matrix, evidence collection, change management, access review, incident response, vendor risk, backup/restore test, SLO/monitoring, retention/deletion policy. | MVP chưa đạt SOC 2. Có thể pitch roadmap. |
| ISO 27001 | Chưa có ISMS. | ISMS scope, risk assessment, risk treatment plan, Statement of Applicability, asset inventory, data classification, access control policy, supplier policy, BCP/DR, internal audit, management review. | MVP chưa cần làm hết, nhưng phải biết gap. |
| KYC/AML/Sanctions | Chưa có KYC/AML thật. Safety rules chỉ là demo transaction safety. | eKYC/CCCD/NFC/face matching/liveness, customer risk rating, CDD/EDD, PEP screening, sanctions/watchlist, adverse media, transaction monitoring, alert/case management, STR workflow, AML retention. | Không gọi safety rule là AML engine. |
| Audit bất biến | Có audit in-memory qua `GET /api/audit`. | Append-only storage, hash chain/signature, WORM/S3 Object Lock, correlation ID, actor/IP/device/session, SIEM forwarding, retention policy. | MVP có explainability, chưa có immutable audit. |
| Encryption/KMS/HSM | Dữ liệu JSON/in-memory, chưa mã hóa thật. | Encryption at rest, envelope encryption, KMS/HSM, key rotation, secret rotation, service decrypt permission, PII masking/tokenization, audit read access. | MVP mock data nên chưa làm; production bắt buộc. |
| SBV Việt Nam | Chưa production-ready. Có mock auth/mock transfer. | Phân loại hệ thống, quản lý rủi ro CNTT, kiểm soát truy cập production, giám sát ATTT, vá lỗi/lỗ hổng, DR RTO/RPO, xác thực khách hàng chuẩn ngân hàng, đối soát/tra soát/khiếu nại, kiểm soát eKYC/biometric. | Cần nói là phải đi qua bank partner/core banking/compliance. |

## MVP Đang Có Gì Để Bám Theo Banking

| Chủ đề | MVP Đã Có | Giới Hạn |
|---|---|---|
| AI boundary | NLU chỉ tạo intent/entities/draft. | Chưa có model risk management/monitoring production. |
| Safety rules | `backend/app/safety/rules.py` có WARN/BLOCK. | Đây là transaction safety demo, không phải AML engine. |
| Risk-based auth | Giao dịch thường cần OTP; giao dịch rủi ro cần OTP + mock biometric. | OTP/biometric đều là mock. |
| Account validation | Tài khoản nguồn phải thuộc user, re-check số dư trước execute. | Dữ liệu vẫn là JSON/in-memory. |
| Recipient/STK validation | Nếu user nhập STK không khớp người nhận thì block `account_hint_mismatch`. | Chưa có beneficiary name enquiry/core banking validation thật. |
| Audit | Có audit event: message, intent, flags, auth, decision. | Audit mất khi restart, chưa append-only/chưa chống sửa. |

## Trả Lời Theo Từng Compliance Topic

### 1. PCI DSS

Nên nói:

```text
Hiện Omni MVP không xử lý số thẻ/PAN/cardholder data, nên PCI DSS chưa nằm trong scope hiện tại. Nếu mở rộng sang card payment hoặc lưu/xử lý PAN, team sẽ phải xác định PCI scope, cô lập Cardholder Data Environment, tokenization/PAN masking, scanning, pentest, MFA, log review và bằng chứng SAQ/ROC/AOC.
```

Không nên nói:

```text
App đã compliant PCI.
```

## 2. SOC 2

Nên nói:

```text
MVP chưa có SOC 2 control evidence program. Hiện team mới demo được audit decision path. Production sẽ cần control matrix, change management, access review, incident response, vendor risk, backup/restore test, uptime/SLO/monitoring evidence và retention policy.
```

## 3. ISO 27001

Nên nói:

```text
MVP chưa phải ISMS. Để đạt ISO 27001, tổ chức triển khai phải có scope ISMS, risk assessment, risk treatment plan, Statement of Applicability, asset inventory, data classification, access control policy, supplier security, BCP/DR, internal audit và management review.
```

## 4. KYC/AML/Sanctions

Nên nói:

```text
Safety engine hiện tại không phải AML engine. Nó chỉ giúp chặn giao dịch thiếu thông tin, mơ hồ, số dư không đủ hoặc rủi ro demo. Production cần KYC/eKYC, CDD/EDD, customer risk rating, PEP/sanctions/adverse media screening, transaction monitoring, alert/case management, suspicious transaction report workflow và AML retention.
```

## 5. Audit Log Bất Biến

Nên nói:

```text
MVP có audit để giải thích quyết định: Omni hiểu gì, flag nào bật, cần auth gì, quyết định là blocked/auth_partial/executed. Nhưng audit hiện là in-memory. Production cần append-only log, hash chain/signature, WORM/Object Lock, request/correlation ID, actor/IP/device/session metadata, SIEM forwarding và retention policy.
```

## 6. Encryption/KMS/HSM

Nên nói:

```text
MVP dùng mock JSON/in-memory nên chưa có encryption at rest thật. Production cần database/file encryption, envelope encryption, KMS/HSM, key rotation, secret rotation, phân quyền decrypt theo service, masking/tokenization PII và audit việc đọc dữ liệu nhạy cảm.
```

## 7. SBV Việt Nam

Nên nói:

```text
Nếu triển khai thật ở ngân hàng Việt Nam, Omni phải nằm trong framework kiểm soát của ngân hàng: quản lý rủi ro CNTT, phân loại hệ thống, kiểm soát truy cập production, giám sát an toàn thông tin, quản lý lỗ hổng/vá lỗi, DR RTO/RPO, xác thực khách hàng, đối soát/tra soát/khiếu nại và kiểm soát eKYC/biometric theo quy định áp dụng.
```

## Bản Đồ MVP -> Production

| MVP | Production |
|---|---|
| JSON/in-memory store | PostgreSQL/Redis + mã hóa dữ liệu |
| Mock banking service | Core banking API/sandbox |
| Mock OTP/biometric | OTP/soft-token/biometric provider của ngân hàng |
| In-memory audit | Append-only immutable audit + SIEM |
| Safety rules demo | Policy engine + AML transaction monitoring |
| Contact JSON | Verified beneficiary service/name enquiry |
| Không có RAG | RAG chỉ dùng cho FAQ/phí/hạn mức/chính sách, không dùng để quyết định chuyển tiền |

## Việc Nên Làm Cho MVP

P0 - nên có trong demo:

- Safety `WARN/BLOCK`.
- Risk-based auth: OTP, OTP + biometric.
- Account source validation.
- STK mismatch block.
- Audit decision path.
- Câu pitch rõ: AI không tự chuyển tiền.

P1 - nếu còn thời gian:

- Audit event có thêm request/session id.
- Log ra file thay vì chỉ memory.
- Mini compliance panel trong pitch deck.

P2 - chỉ nói trong roadmap:

- PCI/SOC2/ISO evidence.
- AML/KYC engine.
- Immutable audit store.
- KMS/HSM.
- Core banking integration.
- SBV production compliance.

## Nguồn Tham Khảo

- PCI SSC: PCI DSS là baseline technical/operational requirements để bảo vệ payment account data; scope là entity lưu/xử lý/truyền cardholder data/sensitive authentication data: https://www.pcisecuritystandards.org/standards/pci-dss/
- PCI SSC FAQ: PCI DSS áp dụng cho cardholder data/PAN; bank account number thường không phải payment card data, trừ khi chứa PAN: https://www.pcisecuritystandards.org/faq/articles/Frequently_Asked_Question/does-pci-dss-apply-to-bank-account-data/
- ISO/IEC 27001: chuẩn giúp tổ chức thiết lập ISMS và áp dụng risk management: https://www.iso.org/standard/27001
- Luật AML Việt Nam 2022 tham khảo bản tiếng Anh: https://english.luatvietnam.vn/law-on-anti-money-laundering-no-14-2022-qh15-of-the-national-assembly-237800-doc1.html
- Thông tư 17/2024/TT-NHNN tham khảo bản tiếng Anh: https://english.luatvietnam.vn/tai-chinh/circular-17-2024-tt-nhnn-opening-and-use-of-payment-accounts-at-payment-service-providers-358811-d1.html

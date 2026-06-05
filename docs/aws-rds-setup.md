# Hướng Dẫn Cấu Hình Amazon RDS PostgreSQL Trên AWS (Free Tier)

Tài liệu này hướng dẫn từng bước khởi tạo cơ sở dữ liệu PostgreSQL trên nền tảng đám mây **AWS RDS (Relational Database Service)** thuộc gói miễn phí (Free Tier) và mở cổng tường lửa để kết nối từ máy tính cá nhân.

---

## Bước 1: Khởi Tạo Database Trên AWS Console

1. Đăng nhập vào [AWS Management Console](https://aws.amazon.com/console/).
2. Trên thanh tìm kiếm ở đỉnh màn hình, gõ **RDS** và chọn dịch vụ **RDS**.
3. Tại bảng điều khiển RDS, nhấn nút **Create database** (Tạo cơ sở dữ liệu).
4. Cấu hình các thông số tạo cơ sở dữ liệu như sau:
   * **Choose a database creation method:** Chọn **Standard create** (Tạo chuẩn).
   * **Engine options:** Chọn **PostgreSQL**.
   * **Templates:** Chọn **Free tier** (Gói miễn phí - *Rất quan trọng để không bị tính phí phát sinh*).
   * **Settings (Cài đặt định danh):**
     * **DB instance identifier:** Đặt tên định danh, ví dụ: `omni-banking-db`.
     * **Master username:** Mặc định là `postgres`.
     * **Master password:** Nhập mật khẩu của bạn (ví dụ: `Omni123456`). Hãy ghi nhớ mật khẩu này.
   * **Connectivity (Kết nối):**
     * **Public access:** Chọn **Yes** (*Bắt buộc chọn Yes trong cuộc thi để máy tính của bạn và đồng đội có thể kết nối từ xa vào database*).
     * **VPC security group:** Chọn **Create new** (Tạo mới) và đặt tên nhóm bảo mật là `omni-db-sg`.
5. Cuộn xuống cuối trang và nhấn nút **Create database**.
   * *Quá trình khởi tạo database sẽ mất khoảng 5 - 10 phút. Trạng thái (Status) của database sẽ chuyển từ "Creating" sang "Available".*

---

## Bước 2: Cấu Hình Mở Cổng Tường Lửa (Security Group Rules)

Theo mặc định, AWS chặn toàn bộ các kết nối từ bên ngoài vào database vì lý do bảo mật. Bạn cần cấu hình mở cổng kết nối cho phép máy tính cá nhân truy cập:

1. Tại trang chi tiết database `omni-banking-db` vừa tạo, tìm mục **Connectivity & security**.
2. Click vào liên kết dưới mục **VPC security groups** (ví dụ: `omni-db-sg`).
3. Chọn nhóm bảo mật đó, cuộn xuống dưới chọn tab **Inbound rules** (Quy tắc đi vào) và nhấn **Edit inbound rules**.
4. Thêm một quy tắc mới với cấu hình như sau:
   * **Type:** Chọn **PostgreSQL** (cổng mặc định `5432` sẽ tự điền).
   * **Source:** Chọn **Anywhere-IPv4** (`0.0.0.0/0`) để cho phép tất cả thành viên trong nhóm code chung kết nối, hoặc chọn **My IP** để chỉ cho phép máy bạn truy cập.
5. Nhấn **Save rules** để lưu lại.

---

## Bước 3: Lấy Địa Chỉ Endpoint & Tạo Chuỗi Kết Nối (Connection String)

1. Quay trở lại trang chi tiết database `omni-banking-db` trên RDS.
2. Tại tab **Connectivity & security**, copy giá trị tại ô **Endpoint**. Giá trị sẽ có dạng tương tự như thế này:
   ```text
   omni-banking-db.c123456789.ap-southeast-1.rds.amazonaws.com
   ```
3. Tạo chuỗi kết nối PostgreSQL tương ứng theo định dạng sau:
   ```text
   postgresql://<username>:<password>@<endpoint>:<port>/<database_name>
   ```
   **Ví dụ thực tế:**
   ```text
   postgresql://postgres:Omni123456@omni-banking-db.c123456789.ap-southeast-1.rds.amazonaws.com:5432/postgres
   ```
   *(Mặc định khi khởi tạo AWS RDS Free Tier, cơ sở dữ liệu mặc định ban đầu là `postgres`).*

---

## Bước 4: Kiểm Tra Và Chạy Script Nạp Dữ Liệu

Khi đã có chuỗi kết nối từ Bước 3, bạn có thể thực hiện chạy script nạp toàn bộ dữ liệu giao dịch 6 tháng và danh bạ lên AWS RDS bằng lệnh:

```bash
python backend/scripts/push_to_postgres.py --db-url "postgresql://postgres:Omni123456@omni-banking-db.c123456789.ap-southeast-1.rds.amazonaws.com:5432/postgres"
```

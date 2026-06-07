# Đưa Omni lên web để mọi người test (Cloudflare Quick Tunnel)

Cách nhẹ nhất để có một URL public `https://xxx.trycloudflare.com` cho ai cũng
vào test — không cần deploy cloud, không cần tài khoản. App chạy trên máy bạn,
cloudflared mở một đường hầm ra Internet.

## Một lần (đã làm xong)

- Build image full-stack (frontend + FastAPI trong 1 container):
  ```powershell
  docker build -t omni-banking .
  ```
- Cài cloudflared: `winget install --id Cloudflare.cloudflared`
  (đường dẫn exe: `C:\Program Files (x86)\cloudflared\cloudflared.exe`)

## Mỗi lần demo

```powershell
# 1. Chạy app (nạp Groq keys từ backend/.env để LLM hoạt động)
docker rm -f omni-banking 2>$null
docker run -d --name omni-banking --env-file backend/.env -p 8000:8000 omni-banking

# 2. Đợi ~12s, kiểm tra khỏe
curl http://localhost:8000/health    # -> {"status":"ok",...}

# 3. Mở tunnel — DÒNG NÀY PHẢI GIỮ CHẠY suốt buổi demo
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:8000
```

cloudflared in ra URL dạng `https://<random>.trycloudflare.com` → copy gửi mọi người.

## Lưu ý quan trọng

- **URL là tạm thời.** Sống chỉ khi container + cửa sổ cloudflared còn chạy.
  Tắt máy/sleep → link chết, chạy lại sẽ ra URL **mới** (random).
- **Đây là bank giả** (seed data, tiền ảo, OTP mock `123456`) → public an toàn.
- **Mọi người dùng chung 1 user demo** (`u_an`) — chung số dư/lịch sử. Hợp để
  test tính năng, không phải multi-user thật.
- **Reset dữ liệu sạch:** chạy lại bước 1 (`docker rm -f` + `docker run`) — image
  bootstrap lại `omni.db` từ JSON seed.

## Muốn URL cố định (không đổi, không chết khi tắt máy)?

Cần **named tunnel** + một domain trỏ vào Cloudflare:
```powershell
cloudflared tunnel login
cloudflared tunnel create omni
cloudflared tunnel route dns omni omni.<domain-cua-ban>.com
cloudflared tunnel run --url http://localhost:8000 omni
```
Khi đó link là `https://omni.<domain>.com` cố định. Cần tài khoản Cloudflare +
1 domain (miễn phí nếu dùng domain rẻ trỏ nameserver về Cloudflare).

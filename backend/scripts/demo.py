"""Live demo runner for Omni — walks through all 6 scenarios with a
chat-style transcript that's nice to read in a terminal."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.context.session import session_for  # noqa: E402
from app.services.orchestrator import (  # noqa: E402
    cancel_draft,
    confirm_draft,
    handle_message,
    select_candidate,
)
from app.store import get_store  # noqa: E402


# ANSI colors — fall back to plain if not a TTY.
USE_COLOR = sys.stdout.isatty()


def c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s


def bold(s):
    return c("1", s)


def navy(s):
    return c("38;5;25", s)


def orange(s):
    return c("38;5;208", s)


def grey(s):
    return c("38;5;245", s)


def red(s):
    return c("38;5;160", s)


def green(s):
    return c("38;5;34", s)


def yellow(s):
    return c("38;5;220", s)


def hr():
    print(grey("─" * 74))


def slow(s: str, delay: float = 0.0) -> None:
    for ch in s:
        print(ch, end="", flush=True)
        if delay:
            time.sleep(delay)
    print()


def user_says(text: str) -> None:
    print()
    print(grey("  An  ➤"), bold(text))


def omni_says(resp) -> None:
    print(orange("  🦉  Omni  ◀"), resp.text)
    if resp.draft:
        d = resp.draft
        if d.candidates and d.recipient is None:
            print(grey("       [card] Disambiguation"))
            for cand in d.candidates:
                print(f"         · {cand.display_name} — {cand.bank} {cand.account_masked}")
        elif d.recipient:
            amt = f"{d.amount:,}đ".replace(",", ".") if d.amount else "—"
            print(grey(f"       [card] Số tiền: {bold(amt)}"))
            print(grey(f"              Người nhận: {d.recipient.display_name} ({d.recipient.bank} {d.recipient.account_masked})"))
            if d.description:
                print(grey(f"              Nội dung: {d.description}"))
        for f in d.flags:
            tag = {"info": grey, "warn": yellow, "block": red}[f.severity]
            icon = {"info": "ℹ️", "warn": "⚠️", "block": "⛔"}[f.severity]
            print("       " + tag(f"{icon}  {f.message}"))
        if d.requires_step_up:
            print("       " + yellow("       → Yêu cầu OTP step-up"))
    if resp.history:
        h = resp.history
        print(grey(f"       [card] {h['count']} giao dịch · TB {h['average']:,}đ".replace(",", ".")))
        for item in h["items"][:3]:
            amt = f"{item['amount']:,}đ".replace(",", ".")
            print(grey(f"         · {item['contact']['display_name']} — {amt} — {item['description']}"))
    if resp.balance:
        b = resp.balance
        total = f"{b['total']:,}đ".replace(",", ".")
        print(grey(f"       [card] Tổng số dư: {bold(total)}"))
        for a in b["accounts"]:
            bal = f"{a['balance']:,}đ".replace(",", ".")
            primary = " (chính)" if a["primary"] else ""
            print(grey(f"         · {a['bank']} ••{a['number'][-4:]}{primary} → {bal}"))
    if resp.schedule:
        s = resp.schedule
        amt = f"{s.amount:,}đ".replace(",", ".")
        print(grey(f"       [card] Lịch định kỳ {amt} · next_run = {s.next_run.strftime('%d/%m/%Y %H:%M')}"))
    print()


def system(text: str) -> None:
    print(grey(f"  · {text}"))


def scenario(num: int, title: str, subtitle: str = "") -> None:
    print()
    hr()
    print(bold(navy(f"  Kịch bản {num:02d} — {title}")))
    if subtitle:
        print(grey("  " + subtitle))
    hr()


USER = "u_an"


def reset_session():
    session_for(USER).clear_draft()


def main() -> None:
    print()
    print(bold(orange("  ╔══════════════════════════════════════════════════════════════════════╗")))
    print(bold(orange("  ║                                                                      ║")))
    print(bold(orange("  ║          OMNI — AI Assistant for Banking                             ║")))
    print(bold(orange("  ║          Team: One Last Token                                        ║")))
    print(bold(orange("  ║                                                                      ║")))
    print(bold(orange("  ╚══════════════════════════════════════════════════════════════════════╝")))

    store = get_store()
    me = store.get_user(USER)
    primary = store.primary_account(USER)
    print()
    print(f"  {grey('User:')} {bold(me.display_name)}   "
          f"{grey('SĐT:')} {me.phone}   "
          f"{grey('Số dư:')} {bold(f'{primary.balance:,}đ'.replace(',', '.'))}")
    print(f"  {grey('Danh bạ:')} {', '.join(c.display_name for c in store.contacts_of(USER))}")

    # ----------------------------------------------------------------- KB01
    scenario(1, "Giao dịch thông thường", "Câu lệnh chuyển tiền trực tiếp — gặp trùng tên Minh")
    reset_session()
    user_says("Chuyển cho Minh 2 triệu tiền ăn tháng này")
    omni_says(handle_message(USER, "Chuyển cho Minh 2 triệu tiền ăn tháng này"))
    system("→ Omni nhận diện 2 người tên Minh và yêu cầu chọn rõ.")

    # ----------------------------------------------------------------- KB02
    scenario(2, "Nhớ ngữ cảnh cá nhân", "Alias 'mẹ' + tham chiếu thời gian 'như tháng trước'")
    reset_session()
    user_says("Gửi cho mẹ 5 triệu như tháng trước")
    r1 = handle_message(USER, "Gửi cho mẹ 5 triệu như tháng trước")
    omni_says(r1)
    system("→ 'mẹ' → Nguyễn Thị Lan (VCB); nội dung tự suy ra từ giao dịch trước.")
    user_says("Xác nhận")
    omni_says(confirm_draft(USER, r1.draft.id))
    system("→ Giao dịch hoàn tất, số dư đã trừ.")

    # ----------------------------------------------------------------- KB03
    scenario(3, "Mơ hồ — cần xác minh", "Disambiguation flow: chọn đúng Minh")
    reset_session()
    user_says("Chuyển cho Minh 500k")
    r2 = handle_message(USER, "Chuyển cho Minh 500k")
    omni_says(r2)
    system("→ Người dùng tap chọn Trần Hoàng Minh.")
    pick = next(c for c in r2.draft.candidates if "Hoàng" in c.display_name)
    r3 = select_candidate(USER, r2.draft.id, pick.id)
    user_says(f"(chọn {pick.display_name})")
    omni_says(r3)
    user_says("Huỷ")
    omni_says(cancel_draft(USER, r3.draft.id if r3.draft else r2.draft.id))
    system("→ Người dùng huỷ — không thực hiện chuyển khoản.")

    # ----------------------------------------------------------------- KB04
    scenario(4, "Truy vấn lịch sử", "Tổng hợp giao dịch trong tháng với 'mẹ'")
    reset_session()
    user_says("Tháng này mình gửi mẹ bao nhiêu rồi?")
    omni_says(handle_message(USER, "Tháng này mình gửi mẹ bao nhiêu rồi?"))
    system("→ Filter theo contact + period; trả về tổng và trung bình.")

    # ----------------------------------------------------------------- KB05
    scenario(5, "Cảnh báo bất thường", "Người nhận mới + số tiền cao gấp ~30× trung bình")
    reset_session()
    user_says("Chuyển 50 triệu cho Hùng STK 9990001234")
    omni_says(handle_message(USER, "Chuyển 50 triệu cho Hùng STK 9990001234"))
    system("→ Safety layer chặn: 3 cờ (new_recipient_large_amount, amount_above_average, insufficient_balance).")

    # ----------------------------------------------------------------- KB06
    scenario(6, "Lên lịch định kỳ", "Tạo cron mùng-1-mỗi-tháng cho mẹ")
    reset_session()
    user_says("Đặt lịch chuyển mẹ 2tr vào mùng 1 hàng tháng")
    omni_says(handle_message(USER, "Đặt lịch chuyển mẹ 2tr vào mùng 1 hàng tháng"))
    system("→ Cron '0 9 1 * *', next_run được tính tự động.")

    # ----------------------------------------------------------------- bonus
    scenario(7, "Bonus — Truy vấn số dư", "Câu lệnh ngắn, intent=balance")
    reset_session()
    user_says("Số dư của mình còn bao nhiêu?")
    omni_says(handle_message(USER, "Số dư của mình còn bao nhiêu?"))

    print()
    hr()
    print(bold(green("  ✓ Demo hoàn tất — 6/6 kịch bản chính + 1 bonus đều hoạt động.")))
    hr()
    final = store.primary_account(USER).balance
    print(f"  {grey('Số dư còn lại:')} {bold(f'{final:,}đ'.replace(',', '.'))}  "
          f"{grey('(đã trừ 5tr cho mẹ ở KB02)')}")
    print()
    print(grey("  Mở UI tương tác tại: ") + bold("http://localhost:5173"))
    print(grey("  API docs:           ") + bold("http://localhost:8000/docs"))
    print()


if __name__ == "__main__":
    main()

"""Multi-turn conversation test — proves chat context is wired through.

Calls the orchestrator in-process so we can reset session memory between
flows (otherwise sessions accumulate stale context across runs).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.context.session import session_for  # noqa: E402
from app.services.orchestrator import (  # noqa: E402
    cancel_draft,
    confirm_draft,
    handle_message,
)

USER = "u_an"


def reset() -> None:
    s = session_for(USER)
    s.current_draft = None
    s.current_contact_draft = None
    s.history.clear()


def show(turn: int, user_text: str, resp) -> None:
    print(f"  T{turn}  user:  {user_text}")
    print(f"      omni:  {resp.text}")
    if resp.draft and resp.draft.recipient:
        d = resp.draft
        print(
            f"      [draft] recipient={d.recipient.display_name}"
            f" amount={d.amount} desc='{d.description}'"
            f" flags={[f.code for f in d.flags]}"
        )
    if resp.history:
        h = resp.history
        print(f"      [history] count={h['count']} total={h['total']} period={h.get('period')}")
    if resp.balance:
        b = resp.balance
        print(f"      [balance] total={b['total']} accounts=" + str(
            [f"{a['bank']} {a['balance']}" for a in b['accounts']]
        ))
    print()


def section(title: str) -> None:
    print()
    print("─" * 72)
    print("  " + title)
    print("─" * 72)


def main() -> None:
    section("Flow 1 — Lịch sử có follow-up ('Còn tháng trước?')")
    reset()
    show(1, "Tháng này tôi gửi mẹ bao nhiêu rồi?",
         handle_message(USER, "Tháng này tôi gửi mẹ bao nhiêu rồi?"))
    show(2, "Còn tháng trước?",
         handle_message(USER, "Còn tháng trước?"))

    section("Flow 2 — Modify draft trong cùng hội thoại")
    reset()
    show(1, "Chuyển cho mẹ 5 triệu tiền sinh hoạt",
         handle_message(USER, "Chuyển cho mẹ 5 triệu tiền sinh hoạt"))
    r2 = handle_message(USER, "Đổi sang 2 triệu thôi")
    show(2, "Đổi sang 2 triệu thôi", r2)
    if r2.draft:
        show(3, "Xác nhận", confirm_draft(USER, r2.draft.id))

    section("Flow 3 — Balance follow-up")
    reset()
    show(1, "Số dư bao nhiêu?", handle_message(USER, "Số dư bao nhiêu?"))
    show(2, "Còn tài khoản phụ thì sao?",
         handle_message(USER, "Còn tài khoản phụ thì sao?"))

    section("Flow 4 — Đổi recipient giữa chừng")
    reset()
    r1 = handle_message(USER, "Chuyển cho mẹ 1 triệu")
    show(1, "Chuyển cho mẹ 1 triệu", r1)
    r2 = handle_message(USER, "Mà thôi, đổi sang chị Thảo đi")
    show(2, "Mà thôi, đổi sang chị Thảo đi", r2)
    if r2.draft:
        cancel_draft(USER, r2.draft.id)

    section("Flow 5 — History theo chủ đề + follow-up so sánh")
    reset()
    show(1, "Tháng này mình tiêu vào những chủ đề nào?",
         handle_message(USER, "Tháng này mình tiêu vào những chủ đề nào?"))
    show(2, "Cái nào nhiều nhất?",
         handle_message(USER, "Cái nào nhiều nhất?"))


if __name__ == "__main__":
    main()

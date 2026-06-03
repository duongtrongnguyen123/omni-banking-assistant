"""Smoke test for all 6 demo scenarios from slide 6."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.orchestrator import handle_message, confirm_draft  # noqa: E402
from app.context.session import session_for  # noqa: E402
from app.store import get_store  # noqa: E402

USER = "u_an"


def _reset():
    session_for(USER).clear_draft()


def _line(s: str = "") -> None:
    print(s)


def case(title: str, user_text: str, then=None):
    _reset()
    _line(f"\n=== {title} ===")
    _line(f"> {user_text}")
    resp = handle_message(USER, user_text)
    _line(f"Omni ({resp.intent}): {resp.text}")
    if resp.draft:
        d = resp.draft
        _line(
            f"  draft: id={d.id} recipient="
            f"{d.recipient.display_name if d.recipient else None} "
            f"amount={d.amount} candidates={[c.display_name for c in d.candidates]}"
        )
        for f in d.flags:
            _line(f"  flag[{f.severity}]: {f.code} — {f.message}")
    if resp.history:
        h = resp.history
        _line(f"  history: count={h['count']} total={h['total']}")
    if resp.balance:
        _line(f"  balance: total={resp.balance['total']}")
    if resp.schedule:
        _line(f"  schedule: next_run={resp.schedule.next_run.isoformat()}")
    if then:
        then(resp)
    return resp


def main() -> None:
    # KB01 — Giao dịch thông thường
    def confirm_after(resp):
        if resp.draft and not any(f.severity == "block" for f in resp.draft.flags):
            r2 = confirm_draft(USER, resp.draft.id, otp="123456")
            _line(f"Omni (confirm): {r2.text}")

    case(
        "KB01 — Giao dịch thông thường",
        "Chuyển cho Minh 2 triệu tiền ăn tháng này",
    )

    # KB02 — Hiểu ngữ cảnh cá nhân
    case(
        "KB02 — Nhớ ngữ cảnh cá nhân",
        "Gửi cho mẹ 5 triệu như tháng trước",
        then=confirm_after,
    )

    # KB03 — Mơ hồ, cần xác minh
    def pick_minh_tcb(resp):
        if resp.draft and resp.draft.candidates:
            tcb = next(
                (c for c in resp.draft.candidates if "Techcom" in c.bank), None
            )
            if tcb:
                from app.services.orchestrator import select_candidate
                r2 = select_candidate(USER, resp.draft.id, tcb.id)
                _line(f"Omni (after select): {r2.text}")

    case(
        "KB03 — Mơ hồ / cần xác minh",
        "Chuyển cho Minh 500k",
        then=pick_minh_tcb,
    )

    # KB04 — Truy vấn lịch sử
    case(
        "KB04 — Truy vấn lịch sử",
        "Tháng này mình gửi mẹ bao nhiêu rồi?",
    )

    # KB05 — Cảnh báo bất thường
    case(
        "KB05 — Cảnh báo bất thường",
        "Chuyển 50 triệu cho Hùng STK 9990001234",
    )

    # KB06 — Lên lịch định kỳ
    case(
        "KB06 — Lên lịch định kỳ",
        "Đặt lịch chuyển mẹ 2tr vào mùng 1 hàng tháng",
    )

    _line()
    _line("Final balance:")
    bal = get_store().get_user(USER).accounts[0].balance
    _line(f"  {bal:,}đ")


if __name__ == "__main__":
    main()

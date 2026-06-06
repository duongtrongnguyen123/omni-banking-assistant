"""Ad-hoc multi-turn probe — drives long transfer dialogues through the
real orchestrator (LLM on, fake-redis session backend) and prints the
draft state after every turn. Used to see, with evidence, whether the
assistant keeps context across amount/recipient changes and whether it
ever fabricates a value the user never gave.

Run:  PYTHONIOENCODING=utf-8 python scripts/multiturn_probe.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OMNI_SESSION_BACKEND", "fake-redis")

from app.services.orchestrator import handle_message, session_for  # noqa: E402


def _fmt_draft(d) -> str:
    if d is None:
        return "draft=None"
    rec = None
    if d.recipient is not None:
        rec = d.recipient.display_name
    elif d.candidates:
        rec = "AMB[" + ", ".join(c.display_name for c in d.candidates) + "]"
    flags = [f.code for f in d.flags]
    return (
        f"recipient={rec} amount={d.amount} predicted={d.predicted_amount} "
        f"awaiting_otp={getattr(d, 'awaiting_otp', None)} flags={flags}"
    )


def run(user_id: str, title: str, turns: list[str]) -> None:
    # Clean isolation between scenarios: drop both the draft and the
    # conversation history so one scenario's turns can't leak into the
    # next via the (shared-user) session.
    session_for(user_id).clear_draft()
    session_for(user_id).set_conversation_history([])
    print("\n" + "=" * 78)
    print("SCENARIO:", title, "  [user=%s]" % user_id)
    print("=" * 78)
    for t in turns:
        resp = handle_message(user_id, t)
        print(f"\nUSER : {t}")
        print(f"OMNI : {resp.text[:220]}")
        print("STATE:", _fmt_draft(resp.draft), "| intent=", resp.intent)
    session_for(user_id).clear_draft()


if __name__ == "__main__":
    run(
        "u_an",
        "Change amount 3x, then recipient, all mid-draft",
        [
            "chuyển cho mẹ 2 triệu",
            "à đổi thành 3 triệu",
            "thôi 5 triệu đi",
            "mà thôi chỉ 1 triệu thôi",
            "đổi người nhận sang chị Thảo",
            "xác nhận",
        ],
    )

    run(
        "u_an",
        "No amount given to ambiguous recipient — must ASK, not fabricate",
        [
            "chuyển tiền cho lan",
        ],
    )

    run(
        "u_an",
        "Amount but ambiguous recipient, then disambiguate",
        [
            "chuyển 500k cho lan",
            "người đầu tiên",
            "xác nhận",
        ],
    )

    run(
        "u_an",
        "Recipient first, amount later, then change mind on amount",
        [
            "mình muốn chuyển cho mẹ",
            "2 triệu nhé",
            "à không, 4 triệu",
            "xác nhận",
        ],
    )

    run(
        "u_an",
        "ORIGINAL BUG with LLM: 10tr to Nam, then redirect to ambiguous lan "
        "(must NOT carry 10tr — must ask amount + which Lan)",
        [
            "chuyển cho nam 10 triệu",
            "à chuyển cho lan đi",
        ],
    )

    run(
        "u_an",
        "Abandon a draft then go vague — stale 10tr must NOT resurface",
        [
            "chuyển cho nam 10 triệu",  # builds a 10tr draft (history now holds it)
            "ờ thôi cái kia đi",        # vague, no new transfer cue
            "chắc vậy",                  # still vague
        ],
    )

    run(
        "u_an",
        "Change source account (người gửi) mid-draft",
        [
            "chuyển cho mẹ 2 triệu",
            "dùng tài khoản phụ nhé",
            "xác nhận",
        ],
    )

    sys.exit(0)

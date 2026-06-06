"""Light concurrent smoke: drive N users × M turns through handle_message
in threads, assert no crashes / no shared-state leak.

Not a load test — those need real HTTP + Locust. This catches obvious
deadlocks, session bleed, and the global-state mutations that show up
only under concurrency. Runs in ~10s.

Run:
    .venv/bin/python scripts/smoke_concurrent.py
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")
os.environ["GROQ_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.context.session import session_for  # noqa: E402
from app.services.orchestrator import handle_message  # noqa: E402

USERS = ["u_an"] * 8  # all hit the same demo user — worst case for session locks
TURNS = [
    "Số dư còn bao nhiêu",
    "Gửi mẹ 500k",
    "Tháng này tiêu bao nhiêu",
    "Lưu Lê Mai STK 0123987654 Vietcombank",
    "Chuyển cho Minh 200k",
    "Khoản nào trả đều hàng tháng",
    "ATM Vietcombank gần nhất",
    "/help",
]


def turn(user_id: str, text: str) -> tuple[str, str, bool, str]:
    try:
        session_for(user_id).clear_draft()
        r = handle_message(user_id, text)
        ok = bool(r.intent) and bool(r.text)
        return (text[:30], r.intent, ok, "")
    except Exception as e:  # pragma: no cover — that's the bug we're hunting
        return (text[:30], "?", False, repr(e))


def main() -> int:
    print(f"Concurrent smoke: {len(USERS)} users × {len(TURNS)} turns = "
          f"{len(USERS) * len(TURNS)} dispatches")

    t0 = time.perf_counter()
    failures: list[str] = []
    intent_counts: dict[str, int] = {}

    with ThreadPoolExecutor(max_workers=len(USERS)) as pool:
        futures = [
            pool.submit(turn, u, t)
            for u in USERS
            for t in TURNS
        ]
        for fut in as_completed(futures):
            text, intent, ok, err = fut.result()
            intent_counts[intent] = intent_counts.get(intent, 0) + 1
            if not ok:
                failures.append(f"{text!r:34s} → {err or 'no reply'}")

    elapsed = time.perf_counter() - t0
    print(f"\n  wall = {elapsed:.2f}s  · throughput = "
          f"{len(futures)/elapsed:.1f} req/s")
    print(f"  intents: {intent_counts}")
    if failures:
        print(f"\n  {len(failures)} FAIL(S):")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("  ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

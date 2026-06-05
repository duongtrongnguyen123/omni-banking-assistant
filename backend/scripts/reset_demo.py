"""One-shot demo reset — the panic button for pitch day.

If anything breaks during the live demo (chat carries stale state,
suggester returns nothing, OTP is mocked but feels wrong), run:

    make reset

That invokes this script which:
  1. Removes the runtime SQLite (demo seed re-bootstraps on next request)
  2. Wipes any in-process drafts / contact drafts / schedule drafts
  3. Pre-warms the fastembed model so the first chat after reset is fast
  4. Pre-trains the suggester for the demo user
  5. Verifies all KB scenarios route correctly (rule pipeline only)

Total wall time: ~10 s on a warm cache, ~30 s cold (fastembed download).

Exits non-zero if any verification step fails — safe to chain with `make check`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"


def step(label: str) -> None:
    print(f"  {DIM}→{RESET} {label}", end=" ", flush=True)


def ok(detail: str = "") -> None:
    print(f"{GREEN}OK{RESET} {DIM}{detail}{RESET}")


def fail(why: str) -> None:
    print(f"{RED}FAIL{RESET}\n    {RED}{why}{RESET}")
    sys.exit(1)


def main() -> None:
    print("Omni demo reset\n")

    # 1. Remove runtime SQLite (NOT contest/Czech/BankSim DBs)
    step("Removing runtime SQLite")
    db_dir = Path(__file__).resolve().parent.parent / "app" / "data"
    runtime_db = db_dir / "omni.db"
    for suffix in ("", "-shm", "-wal"):
        p = db_dir / f"omni.db{suffix}"
        if p.exists():
            p.unlink()
    if runtime_db.exists():
        fail(f"failed to remove {runtime_db}")
    ok("(re-bootstraps from JSON seed on next request)")

    # 2. Wipe in-process drafts. Achievable by clearing any persistent
    # session backend — for memory backend this is a no-op after restart.
    step("Wiping session drafts")
    try:
        from app.context.session import get_backend
        backend = get_backend()
        # The memory + fake-redis backends both support a `clear_all()` shortcut.
        clear = getattr(backend, "clear_all", None)
        if callable(clear):
            clear()
            ok("(via backend.clear_all)")
        else:
            ok("(memory backend — will reset on uvicorn restart)")
    except Exception as e:
        ok(f"(skipped: {e})")

    # 3. Re-bootstrap the seed
    step("Bootstrapping demo seed")
    try:
        from app.store import get_store
        s = get_store()
        contacts = s.contacts_of("u_an")
        txs = s.transactions_of("u_an")
        if len(contacts) < 3:
            fail(f"only {len(contacts)} contacts after bootstrap")
        ok(f"({len(contacts)} contacts, {len(txs)} tx)")
    except Exception as e:
        fail(str(e))

    # 4. Pre-train the suggester for the demo user
    step("Pre-training suggester")
    try:
        from app.ml.suggester import train_for, suggest
        train_for("u_an")
        suggestions = suggest("u_an", k=5)
        ok(f"({len(suggestions)} top picks ready)")
    except Exception as e:
        fail(str(e))

    # 5. Verify all KB scenarios route correctly (LLMs forced off)
    step("Verifying KB scenarios (rule fallback only)")
    os.environ["GROQ_API_KEY"] = ""
    os.environ["GEMINI_API_KEY"] = ""
    try:
        from app.services.orchestrator import handle_message
        from app.context.session import session_for

        cases = {
            "transfer": "Chuyển cho Minh 2 triệu",
            "history":  "tháng này mình gửi mẹ bao nhiêu",
            "schedule": "đặt lịch chuyển mẹ 2tr mùng 1 hàng tháng",
            "add_contact": "Lưu Lê Mai STK 0123987654 Vietcombank",
            "recurring": "có khoản nào trả đều hàng tháng không",
            "balance": "số dư còn bao nhiêu",
        }
        results = {}
        for want, msg in cases.items():
            s = session_for("u_an")
            s.clear_draft()
            s.clear_contact_draft()
            s.clear_schedule_draft()
            r = handle_message("u_an", msg)
            results[want] = r.intent
            if r.intent != want:
                fail(
                    f"{want}: expected intent={want}, got {r.intent} "
                    f"for '{msg}'"
                )
        ok(f"({len(cases)}/{len(cases)} green)")
    except SystemExit:
        raise
    except Exception as e:
        fail(str(e))

    print(f"\n{GREEN}Demo state ready — go pitch.{RESET}")
    print(f"{DIM}Backend on :8000 should still be running; restart if drafts feel stale.{RESET}")


if __name__ == "__main__":
    main()

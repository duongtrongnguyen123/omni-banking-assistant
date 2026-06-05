"""Targeted micro-benchmarks for the hot paths inside `handle_message`.

The full HTTP harness in ``bench.py`` is the source of truth for end-to-end
P50/P95/P99 numbers, but at the unoptimised baseline a single chat
roundtrip on contest data takes ~20s, so a 200-iteration sweep is
~1 hour per flow. This script breaks the orchestrator open and times
each layer in isolation so we can iterate on the optimisation pass in
seconds, then re-run ``bench.py`` once at the end for the headline
table.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")
os.environ.setdefault("GROQ_API_KEY", "")
os.environ.setdefault("GEMINI_API_KEY", "")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

USER = "u_an"


def timed(label, fn, iters=10, warmup=2):
    samples = []
    for i in range(iters + warmup):
        t0 = time.perf_counter()
        fn()
        dt = (time.perf_counter() - t0) * 1000
        if i >= warmup:
            samples.append(dt)
    samples.sort()
    median = samples[len(samples) // 2]
    p95 = samples[int(0.95 * (len(samples) - 1))]
    print(f"{label:42}  P50={median:8.1f}ms  P95={p95:8.1f}ms  n={len(samples)}")
    return median


def main():
    from app.store import get_store
    from app.services.orchestrator import handle_message
    from app.context.alias import resolve_recipient
    from app.ml import suggester
    from app.ml.insights import summary as insights_summary
    from app.banking.recurring import detect_recurring
    from app.banking.service import get_history

    store = get_store()

    # ---- Store primitives ------------------------------------------------
    timed("store.contacts_of(u_an)", lambda: store.contacts_of(USER))
    timed("store.transactions_of(u_an)", lambda: store.transactions_of(USER), iters=3)
    timed("store.primary_account(u_an)", lambda: store.primary_account(USER))
    timed("store.get_contact(c_10000567)", lambda: store.get_contact("c_10000567"))

    # ---- Alias resolution ------------------------------------------------
    contacts = store.contacts_of(USER)

    def _alias():
        for q in ["mẹ", "Minh", "Nam", "anh Tuấn"]:
            resolve_recipient(q, contacts)

    timed("alias_resolve (×4 queries)", _alias)

    # ---- get_history -----------------------------------------------------
    timed(
        "get_history(this_month)",
        lambda: get_history(user_id=USER, period="this_month"),
        iters=3,
    )
    timed(
        "get_history(last_month, semantic=ăn uống)",
        lambda: get_history(
            user_id=USER, period="last_month",
            semantic_filter="ăn uống",
        ),
        iters=3,
    )

    # ---- Recurring -------------------------------------------------------
    txs = store.transactions_of(USER)
    timed(
        "detect_recurring(520k tx)",
        lambda: detect_recurring(txs),
        iters=2,
    )

    # ---- Insights --------------------------------------------------------
    timed(
        "insights.summary(u_an)",
        lambda: insights_summary(USER),
        iters=2,
    )

    # ---- Suggester -------------------------------------------------------
    suggester.train_for(USER)
    timed("suggester.suggest(k=5)", lambda: suggester.suggest(USER, k=5))
    timed(
        "suggester.suggest(k=5, include_all)",
        lambda: suggester.suggest(USER, k=5, include_all=True),
    )

    # ---- Full orchestrator handle_message --------------------------------
    timed(
        "handle_message: transfer",
        lambda: handle_message(USER, "Chuyển cho mẹ 2 triệu tiền ăn"),
        iters=3,
    )
    timed(
        "handle_message: balance",
        lambda: handle_message(USER, "Số dư"),
    )
    timed(
        "handle_message: history",
        lambda: handle_message(USER, "Tháng này tiêu bao nhiêu"),
        iters=3,
    )
    timed(
        "handle_message: smalltalk",
        lambda: handle_message(USER, "Chào"),
    )
    timed(
        "handle_message: add_contact",
        lambda: handle_message(USER, "Lưu Nam STK 9990001234 MB Bank"),
    )


if __name__ == "__main__":
    main()

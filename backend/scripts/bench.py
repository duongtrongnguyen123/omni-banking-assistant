"""Performance benchmarks for Omni at contest scale.

Boots a real uvicorn process against the 520k-row contest DB, then hits
each user-facing flow ≥200 times (configurable; heavy flows scale down
automatically) and reports P50/P95/P99 latency.

Usage
-----

    cd backend
    OMNI_DB_PATH=app/data/omni_contest.db .venv/bin/python -m scripts.bench

The script spawns its own uvicorn — no need to start the server manually.
LLM providers are disabled in the spawned process so we measure the local
hot-path (NLU rule extractor, SQL, suggester, embeddings) rather than
external API jitter. Same reasoning for embeddings: contest rows aren't
backfilled, so the vector code paths short-circuit to lexical fallbacks —
which is what the demo would see too.

Output is a fixed-width table; numbers in milliseconds:

    FLOW                         P50    P95    P99    n
    chat (transfer)              45ms   120ms  180ms  200

Drops the first 10 iterations as warmup so JIT / page-cache / Pydantic
class-cache effects don't pollute the steady-state measurement.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

USER = "u_an"
WARMUP = 10
DEFAULT_ITERS = 200

# Per-flow overrides: heavy flows that touch the full 520k tx table cap
# out at a smaller count so the baseline run is bounded. The baseline
# pre-optimisation cost is so high (15-30s per chat-history call against
# unindexed 520k rows + Pydantic round-trip) that 50 iters would push the
# whole bench past an hour. After optimisation the same flows finish in
# tens of milliseconds and the caps no longer bind.
HEAVY_ITERS_FAST = 50
HEAVY_ITERS_VERY_FAST = 20
# Caps used when the script is invoked with --baseline (pre-optimisation):
# bring everything heavy down to ~30 samples so the run completes inside
# the bash budget without sacrificing P50/P95 stability.
BASELINE_HEAVY_FAST = 30
BASELINE_HEAVY_VERY_FAST = 12

# Insights is the most expensive single endpoint at contest scale because
# it scans every completed tx, computes per-contact z-scores, and runs the
# subscription miner. The cap here is generous because each call is
# isolated — no compounding effect across iterations.
INSIGHTS_CAP = 5


# ---------------------------------------------------------------------------
# uvicorn lifecycle
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"uvicorn never became healthy on :{port}")


def _spawn_uvicorn(port: int, db_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["OMNI_DB_PATH"] = str(db_path)
    # Strip API keys so the NLU path falls through to the deterministic
    # rule extractor — otherwise the bench measures Groq/Gemini RTT.
    env["GROQ_API_KEY"] = ""
    env["GEMINI_API_KEY"] = ""
    # Embedding backfill on 520k contest rows would take many minutes
    # and isn't representative of demo cost.
    env["OMNI_SKIP_EMBED_BACKFILL"] = "1"

    venv_python = ROOT / ".venv" / "bin" / "python"
    python_bin = str(venv_python) if venv_python.exists() else sys.executable
    cmd = [
        python_bin, "-m", "uvicorn",
        "app.main:app",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--log-level", "warning",
    ]
    log_path = Path("/tmp/omni_bench_uvicorn.log")
    print(f"[bench] spawning uvicorn on :{port} (OMNI_DB_PATH={db_path}) "
          f"log={log_path}", flush=True)
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        cmd, env=env, cwd=str(ROOT),
        stdout=log_file, stderr=subprocess.STDOUT,
    )
    _wait_for_health(port)
    print("[bench] uvicorn ready", flush=True)
    return proc


# ---------------------------------------------------------------------------
# Timer
# ---------------------------------------------------------------------------


def _measure(label: str, fn: Callable[[], None], iters: int) -> dict:
    samples: list[float] = []
    for i in range(iters + WARMUP):
        t0 = time.perf_counter()
        fn()
        dt = (time.perf_counter() - t0) * 1000.0
        if i >= WARMUP:
            samples.append(dt)
    samples.sort()
    return {
        "label": label,
        "p50": _pct(samples, 50),
        "p95": _pct(samples, 95),
        "p99": _pct(samples, 99),
        "n": len(samples),
    }


def _pct(sorted_samples: list[float], p: int) -> float:
    if not sorted_samples:
        return 0.0
    k = max(0, min(len(sorted_samples) - 1,
                   int(round(p / 100 * (len(sorted_samples) - 1)))))
    return sorted_samples[k]


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


# (label, message, iters_override). Heavy flows (transfer, recurring, history)
# get a smaller iter cap so the baseline run is bounded. After the
# optimisation pass they'll all be well under 100ms and could run at the
# full DEFAULT_ITERS, but keeping the same caps lets us compare
# before / after on the same axis.
CHAT_MESSAGES: list[tuple[str, str, Optional[int]]] = [
    ("chat (transfer)",        "Chuyển cho mẹ 2 triệu tiền ăn",            HEAVY_ITERS_FAST),
    ("chat (transfer_ref)",    "Gửi mẹ 5 triệu như tháng trước",            HEAVY_ITERS_FAST),
    ("chat (balance)",         "Số dư hiện tại bao nhiêu",                 None),
    ("chat (history_simple)",  "Tháng này mình tiêu bao nhiêu",            HEAVY_ITERS_FAST),
    ("chat (history_rag)",     "tháng trước tiêu bao nhiêu cho ăn uống",   HEAVY_ITERS_VERY_FAST),
    ("chat (schedule)",        "Đặt lịch chuyển mẹ 2tr mùng 1 hàng tháng", HEAVY_ITERS_FAST),
    ("chat (recurring)",       "Mình có khoản nào trả định kỳ không",      HEAVY_ITERS_VERY_FAST),
    ("chat (smalltalk)",       "Chào Omni",                                None),
    ("chat (add_contact)",     "Lưu Nam STK 9990001234 MB Bank",           None),
    ("chat (unknown)",         "asdf qwerty",                              None),
]


def _reset_session(client: httpx.Client) -> None:
    try:
        client.post("/api/session/reset", timeout=60.0)
    except Exception:
        pass


def bench_chat(client: httpx.Client, iters: int) -> list[dict]:
    """Each chat message gets its own row. Session is reset between
    iterations so an in-flight draft doesn't change the dispatch path."""
    results = []
    for label, msg, iter_override in CHAT_MESSAGES:
        n = iter_override if iter_override is not None else iters

        def _hit(msg=msg) -> None:
            _reset_session(client)
            r = client.post("/api/chat", json={"message": msg}, timeout=300.0)
            r.raise_for_status()
        print(f"[bench]   {label} ({n} iters) …", flush=True)
        row = _measure(label, _hit, n)
        print(f"[bench]   {label} done: "
              f"P50={row['p50']:.1f}ms P95={row['p95']:.1f}ms", flush=True)
        results.append(row)
    return results


def _report(row: dict) -> dict:
    print(f"[bench]   {row['label']} done: P50={row['p50']:.1f}ms "
          f"P95={row['p95']:.1f}ms", flush=True)
    return row


def bench_suggestions(client: httpx.Client, iters: int) -> dict:
    print(f"[bench]   suggestions/recipients ({iters} iters) …", flush=True)

    def _hit() -> None:
        r = client.get("/api/suggestions/recipients?limit=5", timeout=120.0)
        r.raise_for_status()
    return _report(_measure("suggestions/recipients", _hit, iters))


def bench_insights(client: httpx.Client, iters: int) -> Optional[dict]:
    # Check route exists
    try:
        probe = client.get("/api/insights/summary", timeout=300.0)
        if probe.status_code == 404:
            return None
    except Exception:
        return None

    def _hit() -> None:
        r = client.get("/api/insights/summary", timeout=300.0)
        r.raise_for_status()
    insights_iters = min(iters, INSIGHTS_CAP)
    print(f"[bench]   insights/summary ({insights_iters} iters) …", flush=True)
    return _report(_measure("insights/summary", _hit, insights_iters))


def bench_alias_resolution(iters: int) -> dict:
    """In-process call (no HTTP) — alias resolution is a hot path inside
    the chat handler but the network overhead would dominate at 50µs/call.
    Runs 50 queries per iteration, so dividing by 50 gives per-query cost."""
    os.environ["OMNI_SKIP_EMBED_BACKFILL"] = "1"
    from app.context.alias import resolve_recipient  # noqa: E402
    from app.store import get_store  # noqa: E402

    contacts = get_store().contacts_of(USER)
    queries = [
        "mẹ", "Minh", "Nam", "anh Tuấn", "chị Lan",
        "em Hoa", "bố", "shipper", "Phương", "Thảo",
        "Hoàng", "Đức", "Vinh", "Trang", "Linh",
        "Tâm", "Hà", "Mai", "Quốc", "Bảo",
        "Khánh", "Phúc", "Long", "An", "Vũ",
        "Hùng", "Tú", "Cường", "Sơn", "Hải",
        "Tùng", "Thắng", "Quang", "Trung", "Đạt",
        "Bình", "Khoa", "Huy", "Toàn", "Việt",
        "Dương", "Nhật", "Phong", "Đăng", "Khải",
        "Nguyên", "Anh", "Kiên", "Tiến", "Thiện",
    ]
    assert len(queries) == 50

    def _hit() -> None:
        for q in queries:
            resolve_recipient(q, contacts)

    n = max(iters // 5, 40)
    print(f"[bench]   alias_resolution (×50 queries × {n} iters) …", flush=True)
    return _report(_measure("alias_resolution (×50q)", _hit, n))


def bench_suggester_inference(iters: int) -> dict:
    """Direct call into suggester.suggest() — bypasses HTTP so we see the
    pure model-inference cost (predict_proba + rule mix + reason
    generation)."""
    os.environ["OMNI_SKIP_EMBED_BACKFILL"] = "1"
    from app.ml.suggester import suggest, train_for  # noqa: E402

    train_for(USER)  # warm the cache

    def _hit() -> None:
        suggest(USER, k=5)

    print(f"[bench]   suggester.suggest(k=5) ({iters} iters) …", flush=True)
    return _report(_measure("suggester.suggest(k=5)", _hit, iters))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_table(rows: list[dict], title: str) -> None:
    print()
    print(f"== {title} ==")
    print(f"{'FLOW':32}{'P50':>10}{'P95':>10}{'P99':>10}{'n':>6}")
    print("-" * 68)
    for r in rows:
        print(
            f"{r['label']:32}{r['p50']:>8.1f}ms{r['p95']:>8.1f}ms"
            f"{r['p99']:>8.1f}ms{r['n']:>6}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iters", type=int, default=DEFAULT_ITERS)
    parser.add_argument(
        "--db",
        type=str,
        default=os.environ.get("OMNI_DB_PATH") or "app/data/omni_contest.db",
    )
    parser.add_argument("--skip-http", action="store_true",
                        help="Skip HTTP-backed benches (chat/insights/suggestions)")
    parser.add_argument("--skip-inproc", action="store_true",
                        help="Skip in-process benches (alias/suggester)")
    parser.add_argument("--skip-chat", action="store_true",
                        help="Skip the chat-flow benches but still run "
                             "suggestions + insights (useful when iterating "
                             "on the heavy non-chat endpoints).")
    parser.add_argument(
        "--baseline", action="store_true",
        help=("Lower per-flow caps for the unoptimised baseline so the run "
              "fits in a single bash budget — use this once, then re-run "
              "without the flag for the post-optimisation comparison."),
    )
    args = parser.parse_args()

    # Apply the baseline overrides in-place. Only the heavy chat flows
    # (transfer / history / recurring) are dialled down; light flows
    # (balance, smalltalk, unknown) still run at the full DEFAULT_ITERS.
    if args.baseline:
        global CHAT_MESSAGES
        CHAT_MESSAGES = [
            (label, msg,
             BASELINE_HEAVY_FAST if override == HEAVY_ITERS_FAST
             else BASELINE_HEAVY_VERY_FAST if override == HEAVY_ITERS_VERY_FAST
             else override)
            for label, msg, override in CHAT_MESSAGES
        ]

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")

    # In-process benches must see the same DB.
    os.environ["OMNI_DB_PATH"] = str(db_path)

    all_rows: list[dict] = []
    proc: Optional[subprocess.Popen] = None
    try:
        if not args.skip_http:
            port = _free_port()
            proc = _spawn_uvicorn(port, db_path)
            # Disable HTTP keep-alive so a stale long-idle connection (e.g. after a
            # 25s baseline transfer call) doesn't get reused and surface as
            # "Connection refused" half-open. Each iteration opens a fresh
            # TCP socket — the overhead is negligible vs the server work.
            limits = httpx.Limits(max_keepalive_connections=0, max_connections=10)
            with httpx.Client(base_url=f"http://127.0.0.1:{port}",
                              headers={"x-user-id": USER, "Connection": "close"},
                              limits=limits) as client:
                # One warm hit so per-route imports finish on the server side.
                client.post("/api/session/reset", timeout=60.0)
                _ = client.post("/api/chat", json={"message": "Chào"}, timeout=120.0)

                print(f"\n[bench] {args.iters} iterations per flow (+{WARMUP} warmup)")

                if not args.skip_chat:
                    all_rows.extend(bench_chat(client, args.iters))
                all_rows.append(bench_suggestions(client, args.iters))
                ins = bench_insights(client, args.iters)
                if ins is not None:
                    all_rows.append(ins)

        if not args.skip_inproc:
            all_rows.append(bench_alias_resolution(args.iters))
            all_rows.append(bench_suggester_inference(args.iters))

        _print_table(all_rows, f"Omni perf — n={args.iters}, db={db_path.name}")

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # Also emit a machine-readable summary for downstream comparisons.
    print()
    print("# bench results (markdown)")
    print()
    print("| Flow | P50 | P95 | P99 | n |")
    print("|---|---|---|---|---|")
    for r in all_rows:
        print(
            f"| {r['label']} | {r['p50']:.1f}ms | {r['p95']:.1f}ms | "
            f"{r['p99']:.1f}ms | {r['n']} |"
        )


if __name__ == "__main__":
    main()

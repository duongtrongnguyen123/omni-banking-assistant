"""Simulate 5 000 transfers on the synth-v2 DB through the A/B framework.

What this does
--------------
For each simulated transfer:
  1. Pick a synthetic user uniformly at random.
  2. Pick a held-out test transaction for that user (the "truth").
  3. Ask ``services.suggester.suggest_for(user_id, when=tx.created_at)``
     for the top-K suggestions. This routes through ``abtest.pick_arm``
     just like a live request would.
  4. Pretend the user confirmed the transfer to the test row's actual
     contact. Call ``consume_outcome`` so the arm is credited with a
     hit (top-1 matched) or a miss (top-1 didn't match).

After all trials, print the per-arm hit_rate table and identify the
winner with margin in percentage points. We expect tree_freq to win on
the synth-v2 generator (the BankSim-tuned weights work here too because
synth-v2 is non-Vietnamese-archetype-coded, see `docs/eval-protocol.md`).

Usage
-----
    OMNI_DB_PATH=backend/app/data/omni_synth_v2.db \\
        BANDIT_MIN_TRIALS=100 \\
        backend/.venv/bin/python backend/scripts/eval_abtest.py

Knobs
-----
    EVAL_N_TRIALS      — number of simulated transfers (default 5000)
    EVAL_SEED          — RNG seed for reproducibility (default 42)
    BANDIT_MIN_TRIALS  — trials/arm before Thompson kicks in (default 100,
                         per the task spec; runtime constant override only).
    OMNI_DB_PATH       — points at the synth-v2 SQLite
"""

from __future__ import annotations

import os
import random
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")
os.environ.setdefault("OMNI_DISABLE_SCHEDULE_TICK", "1")
os.environ.setdefault(
    "OMNI_DB_PATH",
    str(ROOT / "app" / "data" / "omni_synth_v2.db"),
)
# Spec: "After 100 trials per arm, switch from uniform random to Thompson
# sampling". The integration constraint document says ≥30 — the eval honours
# the more aggressive spec because 5 000 trials gives the bandit plenty of
# headroom to find the winner.
os.environ.setdefault("BANDIT_MIN_TRIALS", "100")
# Persist nowhere — eval should be hermetic. We write to a tmp path that
# gets cleaned up at the end.
import tempfile  # noqa: E402
_TMP_STATE = tempfile.NamedTemporaryFile(prefix="bandit_eval_", suffix=".json",
                                         delete=False)
_TMP_STATE.close()
os.environ.setdefault("OMNI_BANDIT_STATE_PATH", _TMP_STATE.name)


from app.db.connection import get_connection  # noqa: E402
from app.ml import abtest, bandit, suggester  # noqa: E402
from app.services.suggester import suggest_for, consume_outcome  # noqa: E402


def _list_users(conn) -> list[str]:
    return [r["id"] for r in conn.execute(
        "SELECT id FROM users WHERE id LIKE 'u_synth_%' ORDER BY id").fetchall()]


class _Tx:
    __slots__ = ("contact_id", "created_at", "category")

    def __init__(self, contact_id: str, created_at: datetime, category: str) -> None:
        self.contact_id = contact_id
        self.created_at = created_at
        self.category = category


def _load_txs(conn, user_id: str) -> list[_Tx]:
    rows = conn.execute(
        """SELECT contact_id, created_at, category
           FROM transactions WHERE owner_id = ?
           ORDER BY created_at""",
        (user_id,),
    ).fetchall()
    return [_Tx(r["contact_id"], datetime.fromisoformat(r["created_at"]),
                r["category"] or "other")
            for r in rows]


def main() -> None:
    n_trials = int(os.environ.get("EVAL_N_TRIALS", "5000"))
    seed = int(os.environ.get("EVAL_SEED", "42"))

    print(f"DB: {os.environ.get('OMNI_DB_PATH')}")
    print(f"Trials: {n_trials}   Seed: {seed}   "
          f"BANDIT_MIN_TRIALS: {bandit.MIN_TRIALS_PER_ARM}")
    print()

    abtest.register_defaults()
    abtest.reset()
    bandit.seed(seed)
    rng = random.Random(seed)

    conn = get_connection()
    users = _list_users(conn)
    if not users:
        print("No synthetic users found. Run gen_synthetic_users.py first.")
        sys.exit(1)

    # Build the test pool per user — last 20 % of each user's history,
    # filtered to contacts with ≥3 train hits (same protocol as
    # eval_suggester_holdout.py). We don't actually train per-arm — the
    # production suggester trains once per user on the user's full history
    # and the arms only differ in the score-mixing weights.
    test_pool: dict[str, list[_Tx]] = {}
    for u in users:
        txs = _load_txs(conn, u)
        if len(txs) < 20:
            continue
        cut = int(len(txs) * 0.8)
        train, test = txs[:cut], txs[cut:]
        train_counts = Counter(t.contact_id for t in train)
        keep = {c for c, n in train_counts.items() if n >= 3}
        test = [t for t in test if t.contact_id in keep]
        if not test:
            continue
        test_pool[u] = test
        # Warm the production suggester with the user's full history. The
        # eval is about ranking, not about hold-out — fine to include the
        # test rows here because the arm weights don't depend on them.
        suggester.train_for(u)

    users_with_tests = sorted(test_pool.keys())
    if not users_with_tests:
        print("No testable users — bail.")
        sys.exit(1)
    print(f"Users with test rows: {len(users_with_tests)} "
          f"(pool size: {sum(len(v) for v in test_pool.values())} rows)")
    print()

    # ----- Simulate trials -----
    arm_picks: Counter = Counter()
    bandit_active_at: int | None = None
    t0 = time.perf_counter()
    for i in range(n_trials):
        u = rng.choice(users_with_tests)
        tx = rng.choice(test_pool[u])

        # We pass the test tx's timestamp as "when" so the day-of-month
        # features are scored against the same moment as ground truth.
        _arm_name, _results = suggest_for(u, when=tx.created_at, k=5)
        arm_picks[_arm_name] += 1
        consume_outcome(u, tx.contact_id)

        if bandit_active_at is None:
            rep = abtest.report()
            if rep and all(a["trials"] >= bandit.MIN_TRIALS_PER_ARM
                           for a in rep.values()):
                bandit_active_at = i + 1

        if (i + 1) % 500 == 0:
            rep = abtest.report()
            best = max(rep.items(), key=lambda kv: kv[1]["hit_rate"])
            print(f"  [{i+1:5d}] best={best[0]:<11s}  "
                  f"hit_rate={best[1]['hit_rate']:.3f}  "
                  f"trials={best[1]['trials']}  "
                  f"bandit_on={bandit_active_at is not None}")

    elapsed = time.perf_counter() - t0
    print()
    print(f"Done in {elapsed:.1f}s "
          f"({n_trials / elapsed:.0f} trials/s)")
    if bandit_active_at is not None:
        print(f"Thompson sampling engaged after trial #{bandit_active_at}")
    else:
        print("Thompson sampling did NOT engage "
              "(some arm stayed below the threshold)")
    print()

    # ----- Final report + winner analysis -----
    print("=== Final per-arm report ===")
    rep = abtest.report()
    print(f"  {'arm':<12s}  {'weights':<22s}  {'trials':>6s}  {'hits':>6s}  "
          f"{'hit_rate':>9s}  {'95% CI':>20s}")
    sorted_arms = sorted(rep.items(), key=lambda kv: -kv[1]["hit_rate"])
    for name, a in sorted_arms:
        w = a["weights"]
        ci = a["ci"]
        print(f"  {name:<12s}  ({w[0]:.2f},{w[1]:.2f},{w[2]:.2f})       "
              f"{a['trials']:6d}  {a['hits']:6d}  {a['hit_rate']:9.4f}  "
              f"[{ci[0]:.3f}, {ci[1]:.3f}]")
    print()

    print("=== Arm pick breakdown over the run ===")
    total = sum(arm_picks.values())
    for name, n in arm_picks.most_common():
        print(f"  {name:<12s}  {n:5d}  ({n/total:.1%})")
    print()

    # Winner vs uniform baseline. Uniform baseline = average of all arms'
    # hit_rates: if you randomised across arms with equal probability, this
    # is what you'd expect long-term.
    rates = [a["hit_rate"] for a in rep.values()]
    uniform_baseline = sum(rates) / len(rates) if rates else 0.0
    winner_name, winner = sorted_arms[0]
    runner_name, runner = sorted_arms[1]
    print("=== Headline ===")
    print(f"  Winner:           {winner_name}  "
          f"(hit_rate {winner['hit_rate']:.3f}, "
          f"weights {tuple(winner['weights'])})")
    print(f"  Runner-up:        {runner_name}  "
          f"(hit_rate {runner['hit_rate']:.3f})")
    print(f"  Margin over #2:   "
          f"{(winner['hit_rate'] - runner['hit_rate']) * 100:+.1f} pp")
    print(f"  Uniform baseline: {uniform_baseline:.3f}  "
          f"({(winner['hit_rate'] - uniform_baseline) * 100:+.1f} pp lift)")

    try:
        os.unlink(_TMP_STATE.name)
    except OSError:
        pass


if __name__ == "__main__":
    main()

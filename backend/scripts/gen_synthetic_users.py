"""Parameterised synthetic-user generator for honest cross-validated eval.

The previous synthetic seed (``generate_synthetic_data.py``) gives a SINGLE
demo user (``u_an``) whose transaction patterns we hand-encoded ourselves.
Evaluating the suggester on that user is circular — the model finds
exactly the day-of-month / day-of-week regularities we wrote in.

This script generates MANY distinct users with DIFFERENT pattern strengths
and DIFFERENT contact mixes. With multiple users we can run two kinds of
honest evaluation:

  1. **In-distribution per-user holdout** — train on the first 80 % of a
     user's tx, hit@K on the last 20 %. This is what we report.
  2. **Cross-user holdout** — train on user A, predict for user B. Should
     drop to ~baseline (≈ 1 / |A.contacts ∩ B.contacts|) because the
     model learned A's day-of-month preferences, which don't generalise
     to B. This proves the model captures user-specific behaviour, not
     a single global "most things happen on the 1st of the month" prior.

Reproducibility is the whole point. Every random draw is seeded; the
output DB is independent (``omni_synth_v2.db``) so the existing demo DB
is untouched.

Usage
-----

    .venv/bin/python scripts/gen_synthetic_users.py \\
        --n-users 20 --months 6 --seed 42 --pattern mixed --noise 0.10

CLI flags:
    --n-users N      number of users (default 20)
    --months M       history depth in months (default 6)
    --seed S         random seed for full reproducibility (default 42)
    --noise N        amount-noise std as fraction of typical (default 0.10)
    --pattern P      strength: "tight" / "loose" / "mixed" (default "mixed")
    --db-path PATH   override output DB path
                     (default backend/app/data/omni_synth_v2.db)
"""

from __future__ import annotations

import argparse
import calendar
import os
import random
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Default-disable embedder; we're writing raw transactions only.
os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")


# ---------------------------------------------------------------------------
# Persona archetypes — used to pick which pattern shapes a generated user
# leans on. Each archetype has its own contact-mix and amount-band template.
# ---------------------------------------------------------------------------

@dataclass
class ContactArchetype:
    """One reusable contact template — a fixed identity (mẹ, sếp, shipper-A …)
    with a base pattern. Each generated user instantiates a SUBSET with
    user-specific jitter on day / dow / amount band so two users sharing
    "mẹ" do not have identical mẹ-shaped histories."""

    key: str                 # short label, e.g. "mom"
    display_name: str        # Vietnamese display name template
    bank_choices: tuple[str, ...]
    category: str
    aliases: tuple[str, ...]
    kind: str                # "dom" / "dow" / "dow_set" / "dom_once" / "random"
    base_params: tuple       # interpretation depends on kind
    amount_lo: int
    amount_hi: int
    desc_tpl: str
    monthly_freq: float = 1.0  # how often this archetype fires per month


ARCHETYPES: list[ContactArchetype] = [
    ContactArchetype("mom", "Nguyễn Thị {first}", ("Vietcombank", "BIDV"),
                     "family", ("mẹ", "me", "mom", "má"),
                     "dom", (1, 2),
                     4_500_000, 5_500_000, "Tiền sinh hoạt tháng", 1.0),
    ContactArchetype("dad", "Trần Văn {first}", ("Vietcombank", "Agribank"),
                     "family", ("bố", "ba", "papa"),
                     "dom", (5, 2),
                     1_500_000, 2_500_000, "Tiền hiếu bố tháng", 0.9),
    ContactArchetype("aunt", "Lê Thị {first}", ("ACB", "Sacombank"),
                     "family", ("cô", "co"),
                     "dom", (10, 3),
                     300_000, 800_000, "Gửi cô", 0.7),
    ContactArchetype("sis", "Phạm Thị {first}", ("Techcombank", "MB Bank"),
                     "family", ("chị", "chi"),
                     "dom", (15, 3),
                     300_000, 800_000, "Tiền chị", 0.6),

    ContactArchetype("grocery", "Cửa hàng {first}", ("MB Bank", "Techcombank"),
                     "daily", ("tạp hoá", "tap hoa"),
                     "dow", (6,),
                     200_000, 400_000, "Mua đồ tạp hoá", 1.0),
    ContactArchetype("lunch", "Cơm Văn Phòng {first}", ("MB Bank",),
                     "daily", ("cơm", "com vp"),
                     "dow_set", (0, 2, 4),
                     35_000, 80_000, "Cơm văn phòng", 1.0),
    ContactArchetype("pt", "PT {first}", ("Vietinbank",),
                     "health", ("pt", "huấn luyện viên"),
                     "dow", (0,),
                     1_900_000, 2_100_000, "PT gym tuần", 0.8),
    ContactArchetype("yoga", "Yoga {first}", ("ACB",),
                     "health", ("yoga",),
                     "dow", (5,),
                     800_000, 1_200_000, "Buổi yoga", 0.8),

    ContactArchetype("shipper_a", "Shipper {first}", ("MoMo Wallet", "MB Bank"),
                     "daily", ("shipper", "ship"),
                     "random", (3, 5),
                     30_000, 120_000, "GrabFood", 1.0),
    ContactArchetype("shipper_b", "Shipper {first}", ("MoMo Wallet",),
                     "daily", ("ship",),
                     "random", (2, 4),
                     30_000, 120_000, "Đặt đồ ăn", 0.8),
    ContactArchetype("bestie", "Lê {first}", ("Techcombank",),
                     "friends", ("bestie", "best"),
                     "dow_count", (4, 2),
                     150_000, 350_000, "Cafe với bestie", 0.8),
    ContactArchetype("colleague", "Nguyễn {first}", ("MB Bank", "TPBank"),
                     "friends", ("đồng nghiệp",),
                     "random", (2, 4),
                     150_000, 700_000, "Cafe + ăn trưa", 0.7),
    ContactArchetype("boss", "Đỗ Văn {first}", ("Vietcombank",),
                     "work", ("sếp", "sep"),
                     "dom_once", (28,),
                     400_000, 800_000, "Quà cuối tháng cho sếp", 0.7),
    ContactArchetype("friend1", "Vũ {first}", ("ACB",),
                     "friends", ("bạn",),
                     "random", (1, 3),
                     150_000, 400_000, "Chia tiền nhậu", 0.6),
    ContactArchetype("friend2", "Bùi {first}", ("Sacombank",),
                     "friends", ("bạn",),
                     "random", (1, 2),
                     100_000, 300_000, "Chia tiền", 0.5),
    ContactArchetype("rent", "Chủ nhà {first}", ("BIDV",),
                     "rent", ("chủ nhà",),
                     "dom", (3, 1),
                     5_000_000, 6_500_000, "Tiền nhà tháng", 1.0),
    ContactArchetype("internet", "Internet {first}", ("Vietinbank",),
                     "utility", ("net", "internet"),
                     "dom", (20, 2),
                     200_000, 300_000, "Cước internet", 0.9),
]

VN_FIRST_NAMES = [
    "An", "Bình", "Châu", "Dung", "Đạt", "Giang", "Hà", "Hằng", "Hùng",
    "Khánh", "Lan", "Linh", "Mai", "Nam", "Ngân", "Nga", "Phong", "Phương",
    "Quân", "Quỳnh", "Sơn", "Thảo", "Thanh", "Thuỷ", "Trang", "Tuấn",
    "Uyên", "Vy", "Yến",
]


# ---------------------------------------------------------------------------
# Pattern-strength knobs
# ---------------------------------------------------------------------------

# adherence: probability that a scheduled tx actually fires
# jitter_mult: multiplier applied to day-of-month jitter window
PATTERN_PROFILES = {
    "tight":  {"adherence": 0.95, "jitter_mult": 0.6, "amount_noise_mult": 0.5},
    "loose":  {"adherence": 0.75, "jitter_mult": 1.5, "amount_noise_mult": 1.5},
    "mixed":  {"adherence": 0.85, "jitter_mult": 1.0, "amount_noise_mult": 1.0},
}


# ---------------------------------------------------------------------------
# Persona builder
# ---------------------------------------------------------------------------

@dataclass
class UserPersona:
    user_id: str
    display_name: str
    archetypes: list[tuple[ContactArchetype, dict]]
    pattern: str   # tight / loose / mixed


def _build_persona(rng: random.Random, idx: int, default_pattern: str) -> UserPersona:
    """Pick 5–15 archetypes and instantiate a user with jittered parameters."""
    # 5-15 contacts inclusive
    n_contacts = rng.randint(5, min(15, len(ARCHETYPES)))

    # Always include the "core" three so most users have a mẹ / grocery /
    # lunch shape — that's realistic. Then sample the rest.
    core = ["mom", "grocery", "lunch"]
    core_archs = [a for a in ARCHETYPES if a.key in core]
    others = [a for a in ARCHETYPES if a.key not in core]
    rng.shuffle(others)
    picks = core_archs + others[: max(0, n_contacts - len(core_archs))]
    picks = picks[:n_contacts]

    # Per-user pattern strength; "mixed" rolls per-user.
    if default_pattern == "mixed":
        pattern = rng.choice(["tight", "loose", "mixed"])
    else:
        pattern = default_pattern

    profile = PATTERN_PROFILES[pattern]
    jitter_mult = profile["jitter_mult"]

    archetypes: list[tuple[ContactArchetype, dict]] = []
    for arch in picks:
        # Per-user jitter: shift the day-of-month preference by ±2 and
        # the amount band by ±20 % so two users sharing "mom" don't
        # collapse to identical traces.
        amount_shift = rng.uniform(0.8, 1.2)
        amount_lo = int(arch.amount_lo * amount_shift)
        amount_hi = int(arch.amount_hi * amount_shift)

        params = arch.base_params
        if arch.kind == "dom":
            target_day, jitter = params
            shift = rng.randint(-2, 2)
            new_day = max(1, min(28, target_day + shift))
            new_jitter = max(1, int(round(jitter * jitter_mult)))
            params = (new_day, new_jitter)
        elif arch.kind == "dom_once":
            (target_day,) = params
            shift = rng.randint(-3, 3)
            params = (max(1, min(28, target_day + shift)),)
        # dow / dow_set / dow_count / random — leave as-is; the noise is
        # primarily in amount and adherence.

        user_arch_state = {
            "params": params,
            "amount_lo": amount_lo,
            "amount_hi": amount_hi,
            "monthly_freq": arch.monthly_freq,
        }
        archetypes.append((arch, user_arch_state))

    user_id = f"u_synth_{idx:03d}"
    display_name = f"User {idx:03d} ({pattern})"
    return UserPersona(user_id=user_id, display_name=display_name,
                       archetypes=archetypes, pattern=pattern)


# ---------------------------------------------------------------------------
# Transaction generation
# ---------------------------------------------------------------------------

def _days_in(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _amount(rng: random.Random, lo: int, hi: int, noise_mult: float) -> int:
    """Pick a base value then perturb by Gaussian noise scaled by ``noise_mult``.
    Always round to nearest 1k VND to mimic banking inputs."""
    base = rng.randint(lo, hi)
    sigma = (hi - lo) * 0.15 * noise_mult
    val = max(1_000, int(round(base + rng.gauss(0, sigma))))
    return int(round(val / 1_000) * 1_000)


def _ts(year: int, month: int, day: int, rng: random.Random, hour: int) -> str:
    minute = rng.randint(0, 59)
    return datetime(year, month, day, hour, minute, 0).isoformat() + "+07:00"


def _generate_user_tx(persona: UserPersona, months: int,
                      start_year: int, start_month: int,
                      noise: float, rng: random.Random) -> list[dict]:
    """Emit transactions for one user. Returns the row dicts ready for SQL."""
    profile = PATTERN_PROFILES[persona.pattern]
    adherence = profile["adherence"]
    noise_eff = noise * profile["amount_noise_mult"]

    rows: list[dict] = []

    # Synthesise contact-id namespace local to this user.
    # arch.key is reused across users; we prefix with user-id so two
    # users' "mom" are distinct contact records in the DB.
    for arch, state in persona.archetypes:
        cid = f"{persona.user_id}__{arch.key}"
        amt_lo, amt_hi = state["amount_lo"], state["amount_hi"]
        params = state["params"]

        # Walk the calendar from (start_year, start_month) for `months` steps.
        y, m = start_year, start_month
        for _ in range(months):
            n_days = _days_in(y, m)
            kind = arch.kind

            if kind == "dom":
                target_day, jitter = params
                if rng.random() < adherence:
                    day = max(1, min(n_days, target_day + rng.randint(-jitter, jitter)))
                    rows.append({
                        "id": f"tg_{uuid.uuid4().hex[:10]}",
                        "owner_id": persona.user_id,
                        "contact_id": cid,
                        "amount": _amount(rng, amt_lo, amt_hi, noise_eff),
                        "description": f"{arch.desc_tpl} {m}",
                        "category": arch.category,
                        "status": "completed",
                        "created_at": _ts(y, m, day, rng, hour=rng.randint(8, 21)),
                    })
            elif kind == "dom_once":
                target_day = params[0]
                if rng.random() < adherence * 0.85:  # less reliable archetype
                    day = max(1, min(n_days, target_day + rng.randint(-2, 2)))
                    rows.append({
                        "id": f"tg_{uuid.uuid4().hex[:10]}",
                        "owner_id": persona.user_id,
                        "contact_id": cid,
                        "amount": _amount(rng, amt_lo, amt_hi, noise_eff),
                        "description": arch.desc_tpl,
                        "category": arch.category,
                        "status": "completed",
                        "created_at": _ts(y, m, day, rng, hour=rng.randint(18, 21)),
                    })
            elif kind == "dow":
                target_dow = params[0]
                for day in range(1, n_days + 1):
                    if datetime(y, m, day).weekday() == target_dow:
                        if rng.random() < adherence:
                            rows.append({
                                "id": f"tg_{uuid.uuid4().hex[:10]}",
                                "owner_id": persona.user_id,
                                "contact_id": cid,
                                "amount": _amount(rng, amt_lo, amt_hi, noise_eff),
                                "description": f"{arch.desc_tpl} {m}/{day}",
                                "category": arch.category,
                                "status": "completed",
                                "created_at": _ts(y, m, day, rng, hour=19 if target_dow == 5 else 7),
                            })
            elif kind == "dow_set":
                target_dows = set(params)
                # In "loose" pattern, the set is fired less often
                fire_p = adherence * 0.7
                for day in range(1, n_days + 1):
                    if datetime(y, m, day).weekday() in target_dows:
                        if rng.random() < fire_p:
                            rows.append({
                                "id": f"tg_{uuid.uuid4().hex[:10]}",
                                "owner_id": persona.user_id,
                                "contact_id": cid,
                                "amount": _amount(rng, amt_lo, amt_hi, noise_eff),
                                "description": arch.desc_tpl,
                                "category": arch.category,
                                "status": "completed",
                                "created_at": _ts(y, m, day, rng, hour=11),
                            })
            elif kind == "dow_count":
                target_dow, n = params
                matches = [d for d in range(1, n_days + 1)
                           if datetime(y, m, d).weekday() == target_dow]
                rng.shuffle(matches)
                want = max(0, min(len(matches),
                                  int(round(n * adherence))))
                for day in matches[:want]:
                    rows.append({
                        "id": f"tg_{uuid.uuid4().hex[:10]}",
                        "owner_id": persona.user_id,
                        "contact_id": cid,
                        "amount": _amount(rng, amt_lo, amt_hi, noise_eff),
                        "description": arch.desc_tpl,
                        "category": arch.category,
                        "status": "completed",
                        "created_at": _ts(y, m, day, rng, hour=18),
                    })
            elif kind == "random":
                lo, hi = params
                base_n = rng.randint(lo, hi)
                n = max(0, int(round(base_n * adherence)))
                if n > 0:
                    days = rng.sample(range(1, n_days + 1), min(n, n_days))
                    for day in days:
                        rows.append({
                            "id": f"tg_{uuid.uuid4().hex[:10]}",
                            "owner_id": persona.user_id,
                            "contact_id": cid,
                            "amount": _amount(rng, amt_lo, amt_hi, noise_eff),
                            "description": arch.desc_tpl,
                            "category": arch.category,
                            "status": "completed",
                            "created_at": _ts(y, m, day, rng, hour=rng.randint(8, 21)),
                        })

            # advance month
            m += 1
            if m > 12:
                m = 1
                y += 1

    rows.sort(key=lambda r: r["created_at"])
    return rows


# ---------------------------------------------------------------------------
# Contact records (one per (user, archetype))
# ---------------------------------------------------------------------------

def _contact_rows(personas: list[UserPersona], rng: random.Random) -> list[dict]:
    """Materialise contact records for every (user, archetype) pair."""
    rows: list[dict] = []
    for p in personas:
        for arch, _ in p.archetypes:
            first = rng.choice(VN_FIRST_NAMES)
            bank = rng.choice(arch.bank_choices)
            acct = "".join(str(rng.randint(0, 9)) for _ in range(10))
            rows.append({
                "id": f"{p.user_id}__{arch.key}",
                "owner_id": p.user_id,
                "display_name": arch.display_name.format(first=first),
                "bank": bank,
                "account_number": acct,
                "account_masked": f"*{acct[-3:]}",
                "label": arch.key,
                "verified": 1,
                "frequent": 1,
                "aliases": list(arch.aliases),
            })
    return rows


# ---------------------------------------------------------------------------
# DB writer
# ---------------------------------------------------------------------------

def _write_db(db_path: Path, personas: list[UserPersona],
              contacts: list[dict], txs: list[dict]) -> None:
    """Create / overwrite a fresh SQLite DB and bulk-insert everything."""
    # Force the connection module to point at our chosen file.
    os.environ["OMNI_DB_PATH"] = str(db_path)

    # Ensure a clean slate.
    for suffix in ("", "-shm", "-wal"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Important: reset the cached connection so the new env var takes
    # effect even if a previous import touched the singleton.
    from app.db import connection as conn_module
    conn_module.reset_connection()
    conn = conn_module.get_connection()  # initialises schema from schema.sql

    # Import alias fold helper.
    from app.context.alias import _fold

    conn.execute("BEGIN")
    try:
        # Users + a primary account each
        for p in personas:
            conn.execute(
                "INSERT OR REPLACE INTO users(id, display_name, phone) VALUES(?,?,?)",
                (p.user_id, p.display_name, "0900000000"),
            )
            conn.execute(
                """INSERT OR REPLACE INTO accounts
                   (id, user_id, bank, number, balance, currency, is_primary)
                   VALUES(?,?,?,?,?,?,?)""",
                (f"acc_{p.user_id}", p.user_id, "Omni Bank",
                 "9999000000", 50_000_000, "VND", 1),
            )

        for c in contacts:
            conn.execute(
                """INSERT OR REPLACE INTO contacts
                   (id, owner_id, display_name, bank, account_number,
                    account_masked, label, verified, frequent)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (c["id"], c["owner_id"], c["display_name"], c["bank"],
                 c["account_number"], c["account_masked"], c["label"],
                 c["verified"], c["frequent"]),
            )
            for alias in c.get("aliases", []):
                conn.execute(
                    """INSERT OR IGNORE INTO contact_aliases
                       (contact_id, alias, alias_normalized) VALUES(?,?,?)""",
                    (c["id"], alias, _fold(alias)),
                )

        for t in txs:
            conn.execute(
                """INSERT OR REPLACE INTO transactions
                   (id, owner_id, contact_id, amount, description, category,
                    status, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (t["id"], t["owner_id"], t["contact_id"], t["amount"],
                 t["description"], t["category"], t["status"], t["created_at"]),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n-users", type=int, default=20)
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--noise", type=float, default=0.10,
                    help="amount-noise std as fraction of typical (0.1 = ±10%%)")
    ap.add_argument("--pattern", choices=("tight", "loose", "mixed"),
                    default="mixed")
    ap.add_argument("--db-path", type=str, default=None,
                    help="output DB path (default backend/app/data/omni_synth_v2.db)")
    args = ap.parse_args()

    db_path = Path(args.db_path) if args.db_path else (
        ROOT / "app" / "data" / "omni_synth_v2.db"
    )

    rng = random.Random(args.seed)

    # Pin start month so re-runs are stable even when "today" moves.
    start_year, start_month = 2025, 12
    # Walk forward so the last month sits in or near "now".
    # months=6 → Dec 2025 .. May 2026.

    personas = [
        _build_persona(rng, idx=i, default_pattern=args.pattern)
        for i in range(args.n_users)
    ]

    all_txs: list[dict] = []
    for p in personas:
        # Per-user RNG split for additional isolation: same global seed
        # yields stable per-user sub-seed, but the per-user RNG ensures
        # interleaved calls don't desync.
        user_seed = rng.randint(0, 2**31 - 1)
        user_rng = random.Random(user_seed)
        rows = _generate_user_tx(p, args.months, start_year, start_month,
                                 args.noise, user_rng)
        all_txs.extend(rows)

    contacts = _contact_rows(personas, rng)

    print(f"Generated {len(personas)} users / {len(contacts)} contacts "
          f"/ {len(all_txs):,} transactions")
    print(f"  seed={args.seed} months={args.months} noise={args.noise} "
          f"pattern={args.pattern}")
    print(f"  output: {db_path}")

    # Per-pattern breakdown
    by_pattern: dict[str, int] = {}
    for p in personas:
        by_pattern[p.pattern] = by_pattern.get(p.pattern, 0) + 1
    for k, v in sorted(by_pattern.items()):
        print(f"    pattern={k}: {v} users")

    # Per-user tx counts (first 10)
    user_counts: dict[str, int] = {}
    for t in all_txs:
        user_counts[t["owner_id"]] = user_counts.get(t["owner_id"], 0) + 1
    sample = sorted(user_counts.items())[:10]
    print("  per-user tx counts (sample):")
    for u, n in sample:
        print(f"    {u}: {n} tx")
    if len(user_counts) > 10:
        print(f"    … ({len(user_counts) - 10} more)")

    _write_db(db_path, personas, contacts, all_txs)
    print("Inserted into SQLite. Run eval_suggester_holdout.py next.")


if __name__ == "__main__":
    main()

# Pre-registered evaluation protocol — synthetic v2 cross-user holdout

This document is the **pre-registered** evaluation protocol for the
Omni next-recipient suggester on the parameterised multi-user synthetic
dataset (`omni_synth_v2.db`).

"Pre-registered" means: the random seed, the hyperparameters, the
metric, and the hold-out split were decided **before** any number was
reported. We did not search across seeds to find a flattering result.
The same command line, run by anyone with this repo, produces the same
numbers — that's reproducibility, and reproducibility is the only
defence against p-hacking accusations.

## Sources

- Generator: `backend/scripts/gen_synthetic_users.py`
- Evaluator: `backend/scripts/eval_suggester_holdout.py`
- Output DB: `backend/app/data/omni_synth_v2.db` (gitignored — regenerate locally)
- Output JSON: `docs/eval-results/omni_synth_v2_eval.json` (committed reproducibility artifact)

## Hyperparameters (pinned)

| Knob | Value | Why |
|------|-------|-----|
| `--seed` | `42` | Fixed, never tuned. |
| `--n-users` | `20` | See sample-size analysis below. |
| `--months` | `6` | Matches contest dataset window (Jan-Jun 2026). |
| `--noise` | `0.10` | ±10 % amount jitter; mid-range, not cherry-picked. |
| `--pattern` | `mixed` | Per-user roll of `tight` / `loose` / `mixed`. |
| Train/test split | first 80 % / last 20 % by `created_at` | Standard time-ordered hold-out; no future leakage. |
| Min train hits per contact | `3` | Test rows pointing at contacts seen < 3× in train are dropped — measures generalisation, not cold-start. |
| Model | `RandomForestClassifier(n_estimators=50, max_depth=5, min_samples_leaf=1, class_weight="balanced", random_state=42)` | Same as production suggester defaults for n_tx < 10 000. |
| Headline weights | `(tree=0.60, freq=0.40, rule=0.00)` | Best on BankSim per `docs/eval-real-data.md` §3. The rule scorer is Vietnamese-locale-tuned and gated off here. |

## Metric

**Hit@K** — fraction of test transactions whose true recipient sits in
the top-K of `suggest()` when ranked by `tree_weight * predict_proba +
freq_weight * frequency_prior + rule_weight * rule_score`.

Reported for K ∈ {1, 3, 5}. We micro-average across the union of test
rows (i.e. weight by per-user test size, not by user count) so a
sparsely-active user does not dominate.

## Hold-out strategy

For each generated user (`u_synth_000` … `u_synth_019`):

1. Sort the user's transactions by `created_at`.
2. Cut at index `floor(len * 0.8)`: head → train, tail → test.
3. Filter the test slice to rows whose `contact_id` appears ≥ 3 times in
   the train slice. This rules out cold-start contacts — the suggester
   cannot reasonably rank a contact it has never seen.
4. Train one Random Forest per user on the train slice.
5. Score every test row with the headline weights.

Cross-user has two flavours:

- **Raw (no mapping)** — feed B's tx into A's model directly. Because
  the generator namespaces contact ids with the user id, A's label set
  never contains B's contact ids, so Hit@K must be 0 — this is a
  sanity check on data isolation.
- **Archetype-mapped** — translate B's `..._mom` → A's `..._mom` by the
  shared archetype suffix. Now we measure how well A's day-of-month /
  day-of-week priors transfer to B's behaviour. The gap between
  in-distribution and archetype-mapped is the **user-specific lift**:
  whatever the model captures beyond shared archetype identity.

## Sample-size analysis (rough chi-squared)

Goal: ±2 percentage points (pp) absolute confidence on Hit@K at the
micro-averaged level. For a binomial outcome with `p ≈ 0.5` the 95 %
half-width is `1.96 * sqrt(p (1 - p) / n)`. To get ±0.02:

```
0.02 ≥ 1.96 * sqrt(0.25 / n)
n     ≥ (1.96 / 0.02)^2 * 0.25 ≈ 2 401
```

For `p ≈ 0.8` (closer to our observed Hit@5) the bound relaxes to
`n ≥ 1 537`. With **20 users × ~30 test rows = ~600 test rows** we are
under-sized for ±2 pp on Hit@1; uncertainty is closer to ±4 pp. We
report the numbers honestly and flag the bound rather than pretending
to higher precision.

Cross-user pairs are capped at 30 (deterministic stride sample) so the
eval finishes in under 15 s. That gives ≈ 870 test rows across pairs,
still in the ±3 pp band.

To tighten the bound to ±2 pp without changing the protocol, re-run
with `--n-users 50` (no other knobs change). The reproducible seed
remains `42`.

## Headline reading

If `hit@K(in-dist)` ≫ `hit@K(cross-mapped)`, the model captures
user-specific timing (good — model differentiates Alice's mom-pattern
from Bob's).

If `hit@K(in-dist) ≈ hit@K(cross-mapped)`, the model is mostly relying
on the **shared archetype** identity (the frequency prior + the
generic "mom on day 1" pattern), not on user-specific learning. That
is exactly the "synthetic = circular" caveat we acknowledge, surfaced
quantitatively.

## What this protocol does NOT prove

- It does not say anything about real-world banking data — those
  numbers live in `docs/eval-real-data.md` (Czech PKDD'99 + BankSim).
- It does not validate the rule scorer. The rule scorer is
  locale-gated by `docs/eval-real-data.md` §3 and turned off here.
- It does not validate alias resolution, the NLU, or the safety
  rules — those have separate tests.

## Reproducing

```bash
cd backend
.venv/bin/python scripts/gen_synthetic_users.py \
    --n-users 20 --months 6 --seed 42 --pattern mixed --noise 0.10

OMNI_DB_PATH=app/data/omni_synth_v2.db \
EVAL_WRITE_JSON=../docs/eval-results/omni_synth_v2_eval.json \
    .venv/bin/python scripts/eval_suggester_holdout.py
```

Expected runtime: ~10 s total on a 2025-era MacBook M-series. Result
JSON contains per-user + per-pair breakdowns for downstream analysis.

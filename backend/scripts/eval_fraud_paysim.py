"""Isolation Forest fraud eval on PaySim (2016) — newer + 10x larger
than BankSim. Honest comparison numbers go into docs/eval-real-data.md.

PaySim schema (Kaggle ealaxi/paysim1):
  step, type, amount, nameOrig, oldbalanceOrg, newbalanceOrig,
  nameDest, oldbalanceDest, newbalanceDest, isFraud, isFlaggedFraud

Fraud is concentrated in TRANSFER + CASH_OUT (per paper). We train an
Isolation Forest on the 9 numeric features (amount, balances, step,
one-hot type) and report recall / precision / FP-rate at threshold 0.5
to match the BankSim eval setup.

Run:
    .venv/bin/python scripts/eval_fraud_paysim.py [--sample N]
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

CSV = (
    Path(__file__).resolve().parent.parent.parent
    / "data" / "public" / "paysim" / "PS_20174392719_1491204439457_log.csv"
)

TYPE_CODES = {"CASH_IN": 0, "CASH_OUT": 1, "DEBIT": 2, "PAYMENT": 3, "TRANSFER": 4}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=500_000,
                        help="Random subsample (stratified) for speed. 0 = full.")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--contamination", default="auto")
    args = parser.parse_args()

    print(f"Loading {CSV.name} …")
    t_load = time.perf_counter()
    df = pd.read_csv(CSV)
    print(f"  {len(df):,} rows in {time.perf_counter() - t_load:.1f}s")
    print(f"  fraud rate: {df['isFraud'].mean():.4f} "
          f"({df['isFraud'].sum():,} positives)")

    # PaySim paper notes fraud only in TRANSFER / CASH_OUT — but we leave
    # other types in the train set so the model sees the full distribution
    # like the per-user IF in the live app does. Out-of-distribution rows
    # should score low and not bias precision much.
    if args.sample and args.sample < len(df):
        frauds = df[df["isFraud"] == 1]
        legit_n = args.sample - len(frauds)
        legit = df[df["isFraud"] == 0].sample(
            n=legit_n, random_state=42,
        )
        df = pd.concat([frauds, legit], ignore_index=True)
        df = df.sample(frac=1, random_state=42).reset_index(drop=True)
        print(f"  stratified subsample → {len(df):,} rows "
              f"({df['isFraud'].sum():,} fraud)")

    feats = pd.DataFrame({
        "log_amount": np.log1p(df["amount"]),
        "log_old_orig": np.log1p(df["oldbalanceOrg"]),
        "log_new_orig": np.log1p(df["newbalanceOrig"]),
        "log_old_dest": np.log1p(df["oldbalanceDest"]),
        "log_new_dest": np.log1p(df["newbalanceDest"]),
        "step": df["step"],
        "type": df["type"].map(TYPE_CODES).astype(int),
        "delta_orig": df["oldbalanceOrg"] - df["newbalanceOrig"],
        "delta_dest": df["newbalanceDest"] - df["oldbalanceDest"],
    })
    X = feats.values
    y = df["isFraud"].values

    print(f"\nTraining IsolationForest on {X.shape[0]:,} × {X.shape[1]} features …")
    t_fit = time.perf_counter()
    clf = IsolationForest(
        n_estimators=100,
        contamination=args.contamination,
        max_samples=min(256, X.shape[0]),
        random_state=42,
        n_jobs=-1,
    )
    clf.fit(X)
    print(f"  fit in {time.perf_counter() - t_fit:.1f}s")

    t_score = time.perf_counter()
    raw = -clf.decision_function(X)
    p50 = float(np.quantile(raw, 0.5))
    p95 = float(np.quantile(raw, 0.95))
    score = 1 / (1 + np.exp(-(raw - p50) / max(p95 - p50, 1e-6) * 4))
    print(f"  score in {time.perf_counter() - t_score:.1f}s")

    print(f"\nThreshold {args.threshold}:")
    pred = (score >= args.threshold).astype(int)
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    tn = int(((pred == 0) & (y == 0)).sum())
    n_fraud = int(y.sum())
    n_legit = int(len(y) - n_fraud)
    recall = tp / max(n_fraud, 1)
    precision = tp / max(tp + fp, 1)
    fp_rate = fp / max(n_legit, 1)
    print(f"  recall      = {recall:.3f}  ({tp}/{n_fraud})")
    print(f"  precision   = {precision:.3f}  ({tp}/{tp + fp})")
    print(f"  FP-rate     = {fp_rate:.3f}  ({fp}/{n_legit})")
    print(f"  median fraud score = {float(np.median(score[y == 1])):.3f}")
    print(f"  median legit score = {float(np.median(score[y == 0])):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

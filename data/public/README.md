# Public reference datasets

We intentionally keep these out of git (size + license hygiene) and
re-fetch on demand. Both download scripts no-op when the expected files
already exist.

## Czech PKDD'99 Financial Dataset (real bank, ~1.05M tx, ground-truth recurring)

Used by `backend/scripts/eval_recurring_czech.py` to evaluate the
recurring-payment detector against the bank's own `permanent_orders`
table (`fin_order.tsv`).

```bash
mkdir -p data/public/czech_pkdd99
cd data/public/czech_pkdd99
BASE=https://raw.githubusercontent.com/dnoeth/1999_Czech_financial_dataset_Teradata/master
for f in fin_account.tsv fin_client.tsv fin_disp.tsv fin_district.tsv \
         fin_loan.tsv fin_order.tsv fin_card.tsv fin_trans.tsv; do
    curl -L -sS -o "$f" "$BASE/$f"
done
```

Original: https://sorry.vse.cz/~berka/challenge/pkdd1999/data_berka.zip
(the GitHub mirror above is a Teradata-prep of the same source: dates
shifted +20 years, amounts /10, Czech `k_symbol` re-coded to English
two-letter abbreviations e.g. `HH`=household, `LO`=loan).

## BankSim (synthetic but with labeled fraud, ~594k tx)

Used by `backend/scripts/eval_fraud_banksim.py` to evaluate the
Isolation-Forest fraud model on real fraud labels (not synthetic
injection), and `backend/scripts/eval_suggester_banksim.py` to evaluate
the next-merchant Hit@K honestly (no circular synthetic encoding).

```bash
mkdir -p data/public/banksim
cd data/public/banksim
curl -L -sS -o bs140513_032310.csv \
    https://raw.githubusercontent.com/atavci/fraud-detection-on-banksim-data/master/Data/synthetic-data-from-a-financial-payment-system/bs140513_032310.csv
```

Original: https://www.kaggle.com/datasets/ealaxi/banksim1 (requires
Kaggle login). The atavci mirror above is the same CSV redistributed
under the original CC0 license.

## What gets generated

| Script | Output | Size |
|--------|--------|------|
| `backend/scripts/load_czech.py` | `backend/app/data/omni_czech.db` | ~80 MB |
| `backend/scripts/load_banksim.py` | `backend/app/data/omni_banksim.db` | ~60 MB |

Both DBs are gitignored.

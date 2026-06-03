# Omni banking simulation dataset

This folder adapts the supplied six-month transaction workbook for Omni.

## Data semantics

- The Omni demo account is `u_an` (`Nguyen Hoang An`).
- Each source `CIF_NO` is a counterparty that has transacted with that account.
- Negative source amounts are outgoing transfers: `u_an -> counterparty`.
- Positive source amounts are incoming transfers: `counterparty -> u_an`.
- Zero-value rows remain in the enriched audit dataset but are excluded from
  the backend demo subset.

## Generate

The transformer uses only the Python standard library. It reads the workbook's
CSV-in-one-cell worksheet, creates a stable Vietnamese name for each CIF and
writes:

```text
data/
├── counterparties.csv
├── source_profile.json
├── demo/
│   ├── metadata.json
│   ├── users.json
│   ├── contacts.json
│   ├── transactions.json
│   └── schedules.json
└── generated/
    └── transactions_enriched_6m.csv
```

Run:

```bash
python3 data/transform_banking_dataset.py \
  --input "/path/to/Du lieu gia lap - banking_simulation_6M.csv.xlsx"
```

`data/generated/` is ignored by Git because the enriched CSV is large and can
always be regenerated.

## Run Omni with the generated demo subset

The existing curated seed remains the default. To opt into this dataset:

```bash
cd backend
BANKING_DATA_DIR=../data/demo .venv/bin/python -m uvicorn app.main:app --reload --port 8001
```

The backend transaction model currently represents outgoing transfers only.
For that reason, `data/demo/transactions.json` is a bounded outgoing subset.
The full enriched CSV preserves both incoming and outgoing directions.

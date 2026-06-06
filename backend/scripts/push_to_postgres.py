#!/usr/bin/env python3
import os
import sys
import json
import csv
import argparse
from pathlib import Path
from datetime import datetime
import psycopg2
from psycopg2.extras import execute_values

def parse_args():
    parser = argparse.ArgumentParser(description="Create schema and seed PostgreSQL database.")
    parser.add_argument(
        "--db-url",
        type=str,
        default=os.getenv("DATABASE_URL"),
        help="PostgreSQL connection string (e.g. postgresql://user:pass@host:5432/db)",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default="backend/app/data",
        help="Path to JSON data directory",
    )
    parser.add_argument(
        "--csv-dir",
        type=str,
        default="data",
        help="Path to CSV data directory",
    )
    return parser.parse_args()

def main():
    args = parse_args()
    db_url = args.db_url
    if not db_url:
        print("Error: Database URL must be provided via --db-url or DATABASE_URL env var.", file=sys.stderr)
        sys.exit(1)

    data_dir = Path(args.data_dir)
    csv_dir = Path(args.csv_dir)

    print(f"Connecting to database at: {db_url.split('@')[-1]} ...")
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
        cur = conn.cursor()
    except Exception as e:
        print(f"Error connecting to database: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        # Step 1: Drop existing tables
        print("Dropping existing tables if they exist...")
        drop_queries = [
            "DROP TABLE IF EXISTS napas_accounts CASCADE;",
            "DROP TABLE IF EXISTS schedules CASCADE;",
            "DROP TABLE IF EXISTS transactions CASCADE;",
            "DROP TABLE IF EXISTS contacts CASCADE;",
            "DROP TABLE IF EXISTS accounts CASCADE;",
            "DROP TABLE IF EXISTS users CASCADE;"
        ]
        for query in drop_queries:
            cur.execute(query)
        conn.commit()
        print("Existing tables dropped successfully.")

        # Step 2: Create tables
        print("Creating tables...")
        create_queries = [
            """
            CREATE TABLE users (
                id VARCHAR PRIMARY KEY,
                display_name VARCHAR NOT NULL,
                phone VARCHAR NOT NULL
            );
            """,
            """
            CREATE TABLE accounts (
                id VARCHAR PRIMARY KEY,
                user_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                bank_name VARCHAR NOT NULL,
                account_number VARCHAR NOT NULL,
                balance NUMERIC(15, 2) NOT NULL CHECK (balance >= 0),
                currency VARCHAR(10) DEFAULT 'VND',
                is_primary BOOLEAN DEFAULT FALSE
            );
            """,
            """
            CREATE TABLE contacts (
                id VARCHAR PRIMARY KEY,
                owner_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                display_name VARCHAR NOT NULL,
                bank_name VARCHAR NOT NULL,
                account_number VARCHAR NOT NULL,
                aliases TEXT[] DEFAULT '{}'::TEXT[],
                label VARCHAR,
                is_verified BOOLEAN DEFAULT TRUE,
                is_frequent BOOLEAN DEFAULT FALSE
            );
            """,
            """
            CREATE TABLE transactions (
                id VARCHAR PRIMARY KEY,
                owner_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                contact_id VARCHAR REFERENCES contacts(id) ON DELETE SET NULL,
                amount NUMERIC(15, 2) NOT NULL CHECK (amount > 0),
                description TEXT,
                category VARCHAR DEFAULT 'other',
                status VARCHAR DEFAULT 'completed',
                created_at TIMESTAMP WITH TIME ZONE NOT NULL
            );
            """,
            """
            CREATE TABLE schedules (
                id VARCHAR PRIMARY KEY,
                owner_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                contact_id VARCHAR NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                source_account_id VARCHAR REFERENCES accounts(id) ON DELETE SET NULL,
                amount NUMERIC(15, 2) NOT NULL CHECK (amount > 0),
                description TEXT,
                cron VARCHAR NOT NULL,
                next_run TIMESTAMP WITH TIME ZONE,
                is_active BOOLEAN DEFAULT TRUE
            );
            """,
            """
            CREATE TABLE napas_accounts (
                bank_name VARCHAR NOT NULL,
                account_number VARCHAR NOT NULL,
                display_name VARCHAR NOT NULL,
                PRIMARY KEY (bank_name, account_number)
            );
            """
        ]
        for query in create_queries:
            cur.execute(query)
        conn.commit()
        print("Schema tables created successfully.")

        # Step 3: Seed Users and Accounts
        users_file = data_dir / "users.json"
        print(f"Seeding users and accounts from {users_file}...")
        if users_file.exists():
            with open(users_file, "r", encoding="utf-8") as f:
                users_data = json.load(f)
            
            for user in users_data:
                cur.execute(
                    "INSERT INTO users (id, display_name, phone) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING;",
                    (user["id"], user["display_name"], user["phone"])
                )
                for acc in user.get("accounts", []):
                    cur.execute(
                        """
                        INSERT INTO accounts (id, user_id, bank_name, account_number, balance, currency, is_primary)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING;
                        """,
                        (
                            acc["id"],
                            user["id"],
                            acc["bank"],
                            acc["number"],
                            acc["balance"],
                            acc.get("currency", "VND"),
                            acc.get("primary", False)
                        )
                    )
            conn.commit()
            print("Users and accounts seeded successfully.")
        else:
            print(f"Warning: {users_file} not found. Skipping user seeding.")

        # Step 4: Seed Contacts
        inserted_contact_ids = set()
        
        # 4a. Load contacts from contacts.json
        contacts_json_file = data_dir / "contacts.json"
        print(f"Seeding contacts from {contacts_json_file}...")
        if contacts_json_file.exists():
            with open(contacts_json_file, "r", encoding="utf-8") as f:
                contacts_data = json.load(f)
            
            for c in contacts_data:
                cur.execute(
                    """
                    INSERT INTO contacts (id, owner_id, display_name, bank_name, account_number, aliases, label, is_verified, is_frequent)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING;
                    """,
                    (
                        c["id"],
                        c["owner_id"],
                        c["display_name"],
                        c["bank"],
                        c["account_number"],
                        c.get("aliases", []),
                        c.get("label"),
                        c.get("verified", True),
                        c.get("frequent", False)
                    )
                )
                inserted_contact_ids.add(c["id"])
            conn.commit()
            print(f"Seeded {len(contacts_data)} contacts from contacts.json.")
        else:
            print(f"Warning: {contacts_json_file} not found. Skipping initial contacts seeding.")

        # 4b. Load contacts from counterparties.csv
        counterparties_file = csv_dir / "counterparties.csv"
        print(f"Seeding contacts from {counterparties_file}...")
        if counterparties_file.exists():
            contacts_batch = []
            with open(counterparties_file, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cid = row["contact_id"]
                    if cid in inserted_contact_ids:
                        continue
                    
                    aliases = [a.strip() for a in row["aliases"].split("|") if a.strip()] if row.get("aliases") else []
                    label = row["label"] if row.get("label") else None
                    is_verified = row["verified"].lower() == "true" if row.get("verified") else True
                    is_frequent = row["frequent"].lower() == "true" if row.get("frequent") else False

                    contacts_batch.append((
                        cid,
                        "u_an", # All these counterparties are An's contacts
                        row["counterparty_name"],
                        row["bank"],
                        row["account_number"],
                        aliases,
                        label,
                        is_verified,
                        is_frequent
                    ))
                    inserted_contact_ids.add(cid)
            
            if contacts_batch:
                execute_values(
                    cur,
                    """
                    INSERT INTO contacts (id, owner_id, display_name, bank_name, account_number, aliases, label, is_verified, is_frequent)
                    VALUES %s
                    ON CONFLICT (id) DO NOTHING;
                    """,
                    contacts_batch
                )
            conn.commit()
            print(f"Seeded {len(contacts_batch)} additional contacts from counterparties.csv.")
        else:
            print(f"Warning: {counterparties_file} not found. Skipping CSV contacts seeding.")

        # Step 5: Seed Schedules (from schedules.json if any)
        schedules_file = data_dir / "schedules.json"
        print(f"Seeding schedules from {schedules_file}...")
        if schedules_file.exists():
            with open(schedules_file, "r", encoding="utf-8") as f:
                try:
                    schedules_data = json.load(f)
                except Exception:
                    schedules_data = []
            
            for s in schedules_data:
                # Ensure contact exists in DB before inserting
                if s["contact_id"] not in inserted_contact_ids:
                    print(f"Skipping schedule {s['id']} as contact {s['contact_id']} is missing.")
                    continue
                cur.execute(
                    """
                    INSERT INTO schedules (id, owner_id, contact_id, source_account_id, amount, description, cron, next_run, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING;
                    """,
                    (
                        s["id"],
                        s["owner_id"],
                        s["contact_id"],
                        s.get("source_account_id"),
                        s["amount"],
                        s.get("description", ""),
                        s["cron"],
                        s.get("next_run"),
                        s.get("active", True)
                    )
                )
            conn.commit()
            print("Schedules seeded successfully.")
        else:
            print(f"Warning: {schedules_file} not found. Skipping schedules seeding.")

        # Step 6: Seed Transactions
        # 6a. Seed from transactions.json
        transactions_json_file = data_dir / "transactions.json"
        print(f"Seeding transactions from {transactions_json_file}...")
        count_json_tx = 0
        if transactions_json_file.exists():
            with open(transactions_json_file, "r", encoding="utf-8") as f:
                transactions_data = json.load(f)
            
            for t in transactions_data:
                # Ensure contact exists or use NULL if not present (nullable fallback)
                cid = t["contact_id"] if t["contact_id"] in inserted_contact_ids else None
                cur.execute(
                    """
                    INSERT INTO transactions (id, owner_id, contact_id, amount, description, category, status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING;
                    """,
                    (
                        t["id"],
                        t["owner_id"],
                        cid,
                        t["amount"],
                        t.get("description", ""),
                        t.get("category", "other"),
                        t.get("status", "completed"),
                        t["created_at"]
                    )
                )
                count_json_tx += 1
            conn.commit()
            print(f"Seeded {count_json_tx} transactions from transactions.json.")
        else:
            print(f"Warning: {transactions_json_file} not found. Skipping JSON transactions seeding.")

        # 6b. Seed from transactions_enriched_6m.csv (Using PostgreSQL COPY command - extremely fast)
        transactions_csv_file = csv_dir / "transactions_enriched_6m.csv"
        print(f"Seeding transactions from {transactions_csv_file} (using fast COPY direct upload)...")
        if transactions_csv_file.exists():
            # Create staging table matching the CSV headers exactly
            cur.execute("""
                CREATE TEMP TABLE staging_transactions (
                    transaction_id VARCHAR,
                    source_cif_no VARCHAR,
                    counterparty_name VARCHAR,
                    counterparty_bank VARCHAR,
                    counterparty_account_number VARCHAR,
                    direction VARCHAR,
                    sender_id VARCHAR,
                    sender_name VARCHAR,
                    receiver_id VARCHAR,
                    receiver_name VARCHAR,
                    signed_amount_vnd VARCHAR,
                    amount_vnd VARCHAR,
                    note_raw TEXT,
                    note_normalized TEXT,
                    category VARCHAR,
                    transaction_at VARCHAR,
                    status VARCHAR
                );
            """)
            
            # Use copy_expert to stream load the CSV file
            with open(transactions_csv_file, "r", encoding="utf-8-sig") as f:
                cur.copy_expert("COPY staging_transactions FROM STDIN WITH CSV HEADER", f)
            conn.commit()
            print("  CSV file copied to staging table. Processing database insertion...")

            # Insert and transform staging data to final transactions table
            cur.execute("""
                INSERT INTO transactions (id, owner_id, contact_id, amount, description, category, status, created_at)
                SELECT 
                    transaction_id,
                    'u_an'::VARCHAR,
                    CASE 
                        WHEN source_cif_no IS NOT NULL AND EXISTS (SELECT 1 FROM contacts WHERE id = 'c_' || source_cif_no) 
                        THEN 'c_' || source_cif_no 
                        ELSE NULL 
                    END,
                    COALESCE(NULLIF(ABS(NULLIF(amount_vnd, '')::NUMERIC), 0.0), 1.0),
                    COALESCE(note_normalized, note_raw, 'Giao dịch'),
                    COALESCE(category, 'other'),
                    COALESCE(status, 'completed'),
                    COALESCE(NULLIF(transaction_at, '')::TIMESTAMP WITH TIME ZONE, NOW())
                FROM staging_transactions
                ON CONFLICT (id) DO NOTHING;
            """)
            conn.commit()
            
            # Fetch count
            cur.execute("SELECT COUNT(*) FROM staging_transactions;")
            total_csv_tx = cur.fetchone()[0]
            
            # Drop staging table
            cur.execute("DROP TABLE IF EXISTS staging_transactions;")
            conn.commit()
            
            print(f"Seeded {total_csv_tx} transactions from transactions_enriched_6m.csv.")
        else:
            print(f"Warning: {transactions_csv_file} not found. Skipping CSV transactions seeding.")

        # Step 7: Seed NAPAS accounts (Union of counterparties.csv and napas_accounts.json)
        print("Seeding NAPAS accounts...")
        napas_set = set()
        napas_batch = []
        
        # 7a. Parse napas_accounts.json
        napas_json_file = data_dir / "napas_accounts.json"
        if napas_json_file.exists():
            with open(napas_json_file, "r", encoding="utf-8") as f:
                napas_json_data = json.load(f)
            for item in napas_json_data:
                bank = item["bank"]
                number = item["account_number"]
                name = item["display_name"]
                key = (bank, number)
                if key not in napas_set:
                    napas_set.add(key)
                    napas_batch.append((bank, number, name))
            print(f"  Parsed {len(napas_json_data)} entries from napas_accounts.json.")
        
        # 7b. Parse counterparties.csv for interbank accounts
        if counterparties_file.exists():
            count_csv_napas = 0
            with open(counterparties_file, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    bank = row["bank"]
                    number = row["account_number"]
                    name = row["counterparty_name"]
                    key = (bank, number)
                    if key not in napas_set:
                        napas_set.add(key)
                        napas_batch.append((bank, number, name))
                        count_csv_napas += 1
            print(f"  Parsed {count_csv_napas} additional entries from counterparties.csv.")

        if napas_batch:
            execute_values(
                cur,
                """
                INSERT INTO napas_accounts (bank_name, account_number, display_name)
                VALUES %s
                ON CONFLICT (bank_name, account_number) DO NOTHING;
                """,
                napas_batch
            )
            conn.commit()
            print(f"Seeded a total of {len(napas_set)} NAPAS interbank accounts.")
        else:
            print("No NAPAS accounts to seed.")

        # Step 8: Verification query
        print("\nVerifying database counts:")
        cur.execute("SELECT COUNT(*) FROM users;")
        print(f"  Users: {cur.fetchone()[0]}")
        cur.execute("SELECT COUNT(*) FROM accounts;")
        print(f"  Accounts: {cur.fetchone()[0]}")
        cur.execute("SELECT COUNT(*) FROM contacts;")
        print(f"  Contacts: {cur.fetchone()[0]}")
        cur.execute("SELECT COUNT(*) FROM schedules;")
        print(f"  Schedules: {cur.fetchone()[0]}")
        cur.execute("SELECT COUNT(*) FROM transactions;")
        print(f"  Transactions: {cur.fetchone()[0]}")
        cur.execute("SELECT COUNT(*) FROM napas_accounts;")
        print(f"  NAPAS Accounts: {cur.fetchone()[0]}")

        print("\nAll tasks completed successfully!")

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"Transaction aborted due to error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()

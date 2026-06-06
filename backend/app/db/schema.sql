-- Omni SQLite schema.
--
-- Design notes
--   * one DB per process; user-level RLS is enforced by always filtering on
--     owner_id in app code (no PG-style policies in SQLite).
--   * contact_aliases is normalised to support O(log n) lookups via the
--     `ix_aliases_norm` index on the diacritic-stripped form.
--   * embedding columns store sentence vectors as raw BLOBs (float32, little-
--     endian). NULL = "not embedded yet" → embedder will fill on next pass.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    phone         TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bank       TEXT NOT NULL,
    number     TEXT NOT NULL,
    balance    INTEGER NOT NULL DEFAULT 0,
    currency   TEXT NOT NULL DEFAULT 'VND',
    is_primary INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_accounts_user ON accounts(user_id);

CREATE TABLE IF NOT EXISTS contacts (
    id              TEXT PRIMARY KEY,
    owner_id        TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    bank            TEXT NOT NULL,
    account_number  TEXT NOT NULL,
    account_masked  TEXT NOT NULL,
    label           TEXT,
    verified        INTEGER NOT NULL DEFAULT 0,
    frequent        INTEGER NOT NULL DEFAULT 0,
    embedding       BLOB
);
CREATE INDEX IF NOT EXISTS ix_contacts_owner ON contacts(owner_id);
CREATE INDEX IF NOT EXISTS ix_contacts_owner_acc ON contacts(owner_id, account_number);

CREATE TABLE IF NOT EXISTS contact_aliases (
    contact_id        TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    alias             TEXT NOT NULL,
    alias_normalized  TEXT NOT NULL,  -- folded form, used for lookup
    PRIMARY KEY (contact_id, alias_normalized)
);
CREATE INDEX IF NOT EXISTS ix_aliases_norm ON contact_aliases(alias_normalized);

CREATE TABLE IF NOT EXISTS transactions (
    id           TEXT PRIMARY KEY,
    owner_id     TEXT NOT NULL,
    contact_id   TEXT,
    amount       INTEGER NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT 'other',
    status       TEXT NOT NULL DEFAULT 'completed',
    created_at   TEXT NOT NULL,
    embedding    BLOB
);
CREATE INDEX IF NOT EXISTS ix_tx_owner_created ON transactions(owner_id, created_at DESC);
-- OPT-4 (bench): covers ``transactions_of(user_id, contact_id=…)`` plus the
-- ORDER BY created_at DESC tail so the planner doesn't fall back to a
-- scan of every owner_id row when the per-contact baseline asks for a
-- single recipient's history. Without ``created_at`` in the key, SQLite
-- on a fresh DB (no ANALYZE) picks ``ix_tx_owner_created`` and filters
-- contact_id in memory — 2.3s vs <10ms on contest data.
CREATE INDEX IF NOT EXISTS ix_tx_owner_contact_created
    ON transactions(owner_id, contact_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_tx_owner_contact ON transactions(owner_id, contact_id);
CREATE INDEX IF NOT EXISTS ix_tx_owner_category ON transactions(owner_id, category);
-- OPT-4 (bench): covers the per-status filter used by insights and
-- recurring detection (``status='completed'`` is the only value queried
-- in the hot path, but the partial index is cheap on a status column
-- whose cardinality is ~3).
CREATE INDEX IF NOT EXISTS ix_tx_owner_status_created
    ON transactions(owner_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS schedules (
    id                 TEXT PRIMARY KEY,
    owner_id           TEXT NOT NULL,
    contact_id         TEXT NOT NULL,
    source_account_id  TEXT,
    amount             INTEGER NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    cron               TEXT NOT NULL,
    next_run           TEXT NOT NULL,
    active             INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_sched_owner ON schedules(owner_id);

-- Budget envelopes: monthly cap per spending category. One row per
-- (user_id, category) — the orchestrator enforces uniqueness so the
-- "update existing" path doesn't accidentally insert duplicates.
CREATE TABLE IF NOT EXISTS budgets (
    id                 TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL,
    category           TEXT NOT NULL,
    monthly_limit_vnd  INTEGER NOT NULL,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_budgets_user ON budgets(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_budgets_user_category
    ON budgets(user_id, category);

-- Savings goals: a named pot the user contributes toward. ``current_vnd``
-- tracks accumulated contributions; ``deadline`` is optional (NULL when
-- the user said "mục tiêu mua xe" without a date).
CREATE TABLE IF NOT EXISTS savings_goals (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    target_vnd   INTEGER NOT NULL,
    current_vnd  INTEGER NOT NULL DEFAULT 0,
    deadline     TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_goals_user ON savings_goals(user_id);

-- Durable chat history. Unlike the ephemeral session store (TTL-bounded,
-- capped at ~20 messages, used only for in-flight drafts + short NLU
-- context), this is the permanent archive that powers the left-hand
-- "conversations" sidebar. One row per conversation; one row per turn.
-- There is no sign-in yet, so rows are namespaced only by ``user_id``
-- (the demo user). ON DELETE CASCADE drops a conversation's messages
-- when the conversation row is deleted.
CREATE TABLE IF NOT EXISTS chat_sessions (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_user
    ON chat_sessions(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL,
    role        TEXT NOT NULL,          -- 'user' | 'omni'
    content     TEXT NOT NULL,
    intent      TEXT,                   -- resolved intent for omni turns
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chat_messages_session
    ON chat_messages(session_id, created_at);

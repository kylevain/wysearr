CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    discord_user_id TEXT NOT NULL,
    discord_username TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    raw_request TEXT NOT NULL,
    title TEXT,
    author TEXT,
    target_key TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    service TEXT,
    external_id TEXT,
    external_status TEXT,
    external_title TEXT,
    error TEXT,
    notified_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES requests(id)
);

CREATE TABLE IF NOT EXISTS delivery_aliases (
    message_id TEXT PRIMARY KEY,
    request_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(request_id) REFERENCES requests(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS candidate_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL UNIQUE,
    shelfarr_correlation TEXT NOT NULL UNIQUE
        CHECK (shelfarr_correlation = 'huey:' || request_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'expired', 'failed')),
    prompt_message_id TEXT UNIQUE,
    selected_ordinal INTEGER CHECK (selected_ordinal BETWEEN 1 AND 3),
    dispatch_started_at TEXT,
    failure_message TEXT,
    FOREIGN KEY(request_id) REFERENCES requests(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS candidate_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 3),
    fingerprint TEXT NOT NULL
        CHECK (
            length(fingerprint) = 64
            AND fingerprint NOT GLOB '*[^0-9a-f]*'
        ),
    label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 300),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
    author TEXT CHECK (author IS NULL OR length(author) BETWEEN 1 AND 160),
    year INTEGER CHECK (year IS NULL OR year BETWEEN 0 AND 9999),
    book_type TEXT NOT NULL CHECK (book_type IN ('ebook', 'audiobook')),
    candidate_json TEXT NOT NULL CHECK (length(candidate_json) BETWEEN 2 AND 4096),
    FOREIGN KEY(confirmation_id) REFERENCES candidate_confirmations(id) ON DELETE CASCADE,
    UNIQUE(confirmation_id, ordinal),
    UNIQUE(confirmation_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS candidate_confirmation_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_id INTEGER NOT NULL,
    reply_message_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    discord_user_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('claimed', 'invalid', 'expired', 'duplicate')),
    FOREIGN KEY(confirmation_id) REFERENCES candidate_confirmations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    route TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at TEXT,
    FOREIGN KEY(request_id) REFERENCES requests(id) ON DELETE CASCADE,
    UNIQUE(request_id, event_key, route)
);

CREATE INDEX IF NOT EXISTS notification_deliveries_pending_idx
    ON notification_deliveries(delivered_at, id);

CREATE INDEX IF NOT EXISTS candidate_confirmations_expiry_idx
    ON candidate_confirmations(status, expires_at, id);

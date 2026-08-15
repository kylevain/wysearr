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
    dispatch_started_at TEXT,
    abba_candidate_id TEXT CHECK (
        abba_candidate_id IS NULL OR (
            length(abba_candidate_id) = 69
            AND substr(abba_candidate_id, 1, 5) = 'abba:'
            AND substr(abba_candidate_id, 6) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    lazylibrarian_book_id TEXT CHECK (
        lazylibrarian_book_id IS NULL OR (
            length(lazylibrarian_book_id) BETWEEN 1 AND 255
            AND lazylibrarian_book_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        )
    ),
    canonical_request_id INTEGER REFERENCES requests(id) CHECK (
        canonical_request_id IS NULL OR canonical_request_id > 0
    ),
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

-- One logical ebook request owns one immutable ordered backend policy.  An
-- attempt may advance only before its durable mutation boundary; a crossed
-- boundary is reconciled or quarantined and is never authorized to fall back.
CREATE TABLE IF NOT EXISTS ebook_cascades (
    request_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    policy_json TEXT NOT NULL CHECK (length(policy_json) BETWEEN 3 AND 128),
    current_ordinal INTEGER NOT NULL DEFAULT 0 CHECK (current_ordinal >= 0),
    state TEXT NOT NULL DEFAULT 'searching' CHECK (
        state IN (
            'searching', 'awaiting_selection', 'mutating', 'uncertain',
            'queued', 'completed', 'failed'
        )
    ),
    identity_key TEXT CHECK (
        identity_key IS NULL OR (
            length(identity_key) = 64
            AND identity_key NOT GLOB '*[^0-9a-f]*'
        )
    ),
    identity_fingerprint TEXT CHECK (
        identity_fingerprint IS NULL OR (
            length(identity_fingerprint) = 64
            AND identity_fingerprint NOT GLOB '*[^0-9a-f]*'
        )
    ),
    identity_json TEXT CHECK (
        identity_json IS NULL OR length(identity_json) BETWEEN 2 AND 4096
    ),
    mutation_backend TEXT CHECK (
        mutation_backend IS NULL OR mutation_backend IN ('lazylibrarian', 'shelfarr')
    ),
    mutation_started_at TEXT,
    final_backend TEXT CHECK (
        final_backend IS NULL OR final_backend IN ('lazylibrarian', 'shelfarr')
    ),
    finalizer TEXT CHECK (
        finalizer IS NULL OR finalizer IN ('bookbot', 'shelfarr')
    ),
    FOREIGN KEY(request_id) REFERENCES requests(id) ON DELETE CASCADE,
    CHECK (
        (
            identity_key IS NULL AND identity_fingerprint IS NULL
            AND identity_json IS NULL
        ) OR (
            identity_key IS NOT NULL AND identity_fingerprint IS NOT NULL
            AND identity_json IS NOT NULL
        )
    ),
    CHECK (
        state NOT IN ('queued', 'completed') OR identity_key IS NOT NULL
    ),
    CHECK ((mutation_backend IS NULL) = (mutation_started_at IS NULL)),
    CHECK (
        (final_backend IS NULL AND finalizer IS NULL)
        OR (final_backend = 'lazylibrarian' AND finalizer = 'bookbot')
        OR (final_backend = 'shelfarr' AND finalizer = 'shelfarr')
    ),
    CHECK (
        state NOT IN ('queued', 'completed') OR final_backend IS NOT NULL
    ),
    CHECK (
        state NOT IN (
            'searching', 'awaiting_selection', 'mutating', 'uncertain'
        ) OR final_backend IS NULL
    ),
    CHECK (
        mutation_backend IS NULL OR final_backend IS NULL
        OR mutation_backend = final_backend
    )
);

CREATE TABLE IF NOT EXISTS ebook_backend_attempts (
    request_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    backend TEXT NOT NULL CHECK (backend IN ('lazylibrarian', 'shelfarr')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending', 'searching', 'awaiting_selection', 'mutating',
            'miss', 'unavailable', 'queued', 'completed', 'failed', 'uncertain'
        )
    ),
    started_at TEXT,
    finished_at TEXT,
    mutation_started_at TEXT,
    mutation_resolved_at TEXT,
    backend_identity TEXT CHECK (
        backend_identity IS NULL OR length(backend_identity) BETWEEN 1 AND 255
    ),
    external_id TEXT,
    external_status TEXT,
    outcome_message TEXT CHECK (
        outcome_message IS NULL OR length(outcome_message) <= 1000
    ),
    PRIMARY KEY(request_id, ordinal),
    UNIQUE(request_id, backend),
    FOREIGN KEY(request_id) REFERENCES ebook_cascades(request_id) ON DELETE CASCADE
);

-- Backend-local IDs remain reserved for the lifetime of an active/successful
-- logical request, even after that backend cleanly misses and the service
-- field advances.  An exhausted failed cascade releases these reservations.
CREATE TABLE IF NOT EXISTS ebook_backend_reservations (
    backend TEXT NOT NULL CHECK (backend IN ('lazylibrarian', 'shelfarr')),
    backend_identity TEXT NOT NULL CHECK (length(backend_identity) BETWEEN 1 AND 255),
    request_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(backend, backend_identity),
    FOREIGN KEY(request_id) REFERENCES ebook_cascades(request_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ebook_cascades_active_identity_uq
    ON ebook_cascades(identity_key)
    WHERE identity_key IS NOT NULL
      AND state IN (
          'searching', 'awaiting_selection', 'mutating', 'uncertain',
          'queued', 'completed'
      );

CREATE INDEX IF NOT EXISTS ebook_cascades_resume_idx
    ON ebook_cascades(state, current_ordinal, updated_at, request_id);

CREATE INDEX IF NOT EXISTS ebook_backend_attempts_state_idx
    ON ebook_backend_attempts(status, request_id, ordinal);

-- BookBot writes Huey's requests table directly after its ledger-validated
-- import.  Keep the cascade audit terminal in that same SQLite transaction,
-- regardless of whether Shelfarr or BookBot is the backend-specific finalizer.
DROP TRIGGER IF EXISTS ebook_request_terminal_sync;
CREATE TRIGGER ebook_request_terminal_sync
AFTER UPDATE OF status ON requests
WHEN NEW.media_type = 'ebooks'
 AND NEW.status IN ('complete', 'completed', 'failed')
 AND OLD.status != NEW.status
 AND EXISTS (
     SELECT 1 FROM ebook_cascades
     WHERE request_id = NEW.id
       AND state IN ('mutating', 'uncertain', 'queued')
 )
BEGIN
    UPDATE ebook_backend_attempts
    SET status = CASE
            WHEN NEW.status IN ('complete', 'completed') THEN 'completed'
            ELSE 'failed'
        END,
        finished_at = CURRENT_TIMESTAMP,
        mutation_resolved_at = CASE
            WHEN mutation_started_at IS NOT NULL THEN CURRENT_TIMESTAMP
            ELSE mutation_resolved_at
        END,
        outcome_message = COALESCE(outcome_message, NEW.error)
    WHERE request_id = NEW.id
      AND ordinal = (
          SELECT current_ordinal FROM ebook_cascades WHERE request_id = NEW.id
      )
      AND status IN ('mutating', 'uncertain', 'queued');

    UPDATE ebook_cascades
    SET state = CASE
            WHEN NEW.status IN ('complete', 'completed') THEN 'completed'
            ELSE 'failed'
        END,
        final_backend = CASE
            WHEN NEW.status IN ('complete', 'completed') THEN COALESCE(
                final_backend,
                (
                    SELECT backend FROM ebook_backend_attempts
                    WHERE request_id = NEW.id
                      AND ordinal = ebook_cascades.current_ordinal
                )
            )
            ELSE final_backend
        END,
        finalizer = CASE
            WHEN NEW.status IN ('complete', 'completed') THEN COALESCE(
                finalizer,
                CASE (
                    SELECT backend FROM ebook_backend_attempts
                    WHERE request_id = NEW.id
                      AND ordinal = ebook_cascades.current_ordinal
                )
                    WHEN 'lazylibrarian' THEN 'bookbot'
                    WHEN 'shelfarr' THEN 'shelfarr'
                END
            )
            ELSE finalizer
        END,
        updated_at = CURRENT_TIMESTAMP
    WHERE request_id = NEW.id
      AND state IN ('mutating', 'uncertain', 'queued');

    DELETE FROM ebook_backend_reservations
    WHERE request_id = NEW.id AND NEW.status = 'failed';
END;

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
    request_id INTEGER,
    trusted_event_id INTEGER,
    event_key TEXT NOT NULL,
    route TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at TEXT,
    FOREIGN KEY(request_id) REFERENCES requests(id) ON DELETE CASCADE,
    FOREIGN KEY(trusted_event_id) REFERENCES trusted_library_events(id) ON DELETE CASCADE,
    CHECK ((request_id IS NOT NULL) != (trusted_event_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS trusted_library_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_type TEXT NOT NULL CHECK (source_type = 'physical-disc'),
    source_fingerprint TEXT NOT NULL CHECK (
        length(source_fingerprint) = 64
        AND source_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    source_path TEXT NOT NULL,
    title TEXT,
    year INTEGER CHECK (year IS NULL OR year BETWEEN 1878 AND 2200),
    imdb_id TEXT,
    tmdb_id INTEGER,
    media_type TEXT NOT NULL DEFAULT 'movie' CHECK (
        media_type IN ('movie', 'tv', 'nonstandard', 'ambiguous')
    ),
    group_key TEXT,
    sonarr_series_id INTEGER,
    sonarr_command_id INTEGER,
    metadata_json TEXT CHECK (metadata_json IS NULL OR length(metadata_json) BETWEEN 2 AND 65536),
    state TEXT NOT NULL DEFAULT 'received' CHECK (state IN (
        'received', 'validated', 'identity_resolved', 'import_submitting',
        'importing', 'completed', 'manual_review', 'failed'
    )),
    radarr_movie_id INTEGER,
    radarr_command_id INTEGER,
    final_path TEXT,
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    error TEXT,
    UNIQUE(source_type, source_fingerprint)
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
-- field advances.  Active/fulfilled unavailable retries retain them; only an
-- ordinary terminal failure or an expired retry releases them.
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

-- A cleanly exhausted ebook cascade keeps its canonical work identity here.
-- The row owns every later silent attempt and remains tied to the original
-- Discord/Huey correlation until a final-library import is proved or the
-- bounded retry policy expires.
CREATE TABLE IF NOT EXISTS unavailable_retries (
    request_id INTEGER PRIMARY KEY,
    media_type TEXT NOT NULL CHECK (media_type = 'ebooks'),
    identity_key TEXT NOT NULL CHECK (
        length(identity_key) = 64
        AND identity_key NOT GLOB '*[^0-9a-f]*'
    ),
    metadata_json TEXT NOT NULL CHECK (
        length(metadata_json) BETWEEN 2 AND 4096
    ),
    canonical_title TEXT NOT NULL CHECK (
        length(canonical_title) BETWEEN 1 AND 160
    ),
    canonical_creator TEXT CHECK (
        canonical_creator IS NULL
        OR length(canonical_creator) BETWEEN 1 AND 160
    ),
    canonical_year INTEGER CHECK (
        canonical_year IS NULL OR canonical_year BETWEEN 0 AND 9999
    ),
    discord_user_id TEXT NOT NULL,
    discord_username TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    first_unavailable_at TEXT NOT NULL,
    last_retry_at TEXT,
    -- Durable fairness cursor for read-only checks of blocked Shelfarr jobs.
    -- NULL rows are checked first; each claimed polling batch advances this
    -- value atomically before making any remote request.
    last_proof_check_at TEXT,
    next_retry_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (
        retry_count BETWEEN 0 AND 7
    ),
    state TEXT NOT NULL DEFAULT 'queued' CHECK (
        state IN (
            'queued', 'retrying', 'awaiting_import', 'blocked',
            'fulfilled', 'expired'
        )
    ),
    final_import_state TEXT NOT NULL DEFAULT 'pending' CHECK (
        final_import_state IN ('pending', 'verified')
    ),
    fulfilled_at TEXT,
    expired_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(request_id) REFERENCES requests(id) ON DELETE CASCADE,
    CHECK (
        (state = 'fulfilled' AND final_import_state = 'verified'
            AND fulfilled_at IS NOT NULL)
        OR (state != 'fulfilled' AND final_import_state = 'pending'
            AND fulfilled_at IS NULL)
    ),
    CHECK (
        (state = 'expired' AND expired_at IS NOT NULL)
        OR (state != 'expired' AND expired_at IS NULL)
    ),
    CHECK (
        (state = 'queued' AND next_retry_at IS NOT NULL)
        OR (state != 'queued' AND next_retry_at IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS unavailable_retries_active_identity_uq
    ON unavailable_retries(identity_key)
    WHERE state IN ('queued', 'retrying', 'awaiting_import', 'blocked');

CREATE INDEX IF NOT EXISTS unavailable_retries_due_idx
    ON unavailable_retries(state, next_retry_at, request_id);

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
    WHERE request_id = NEW.id AND NEW.status = 'failed'
      AND NOT EXISTS (
          SELECT 1 FROM unavailable_retries
          WHERE request_id = NEW.id
            AND state IN ('queued', 'retrying', 'awaiting_import', 'blocked',
                          'fulfilled')
      );
END;

-- A definitive failure after a downloader/submission boundary is not safe to
-- reacquire automatically.  Keep ownership and remain silent for operator
-- review; operators must resolve the terminal owner rather than redispatch it.
DROP TRIGGER IF EXISTS unavailable_retry_import_failure_sync;
CREATE TRIGGER unavailable_retry_import_failure_sync
AFTER UPDATE OF status ON requests
WHEN NEW.status = 'failed'
 AND OLD.status != 'failed'
 AND EXISTS (
     SELECT 1 FROM unavailable_retries
     WHERE request_id = NEW.id AND state = 'awaiting_import'
 )
BEGIN
    UPDATE unavailable_retries
    SET state = 'blocked', next_retry_at = NULL,
        updated_at = CURRENT_TIMESTAMP
    WHERE request_id = NEW.id AND state = 'awaiting_import';

    INSERT INTO events (request_id, event_type, message)
    VALUES (
        NEW.id,
        'unavailable_retry_blocked',
        'Unavailable retry retained ownership after a post-mutation failure'
    );
END;

-- Both supported ebook finalizers update requests only after final-library
-- proof: Shelfarr reports its exact request completed, or BookBot commits its
-- ledger-validated copy for the exact Huey tag and download hash.  A blocked
-- owner is never reacquired, but that already-correlated final proof may still
-- repair its failed cascade and fulfil it.  Reopen only the normal terminal
-- notification outbox at that same boundary.
DROP TRIGGER IF EXISTS unavailable_retry_blocked_completion_guard;
CREATE TRIGGER unavailable_retry_blocked_completion_guard
BEFORE UPDATE OF status ON requests
WHEN NEW.status IN ('complete', 'completed')
 AND OLD.status NOT IN ('complete', 'completed')
 AND EXISTS (
     SELECT 1 FROM unavailable_retries
     WHERE request_id = NEW.id AND state = 'blocked'
 )
 AND NOT EXISTS (
     SELECT 1
     FROM ebook_cascades AS cascade
     JOIN ebook_backend_attempts AS attempt
       ON attempt.request_id = cascade.request_id
      AND attempt.ordinal = cascade.current_ordinal
     WHERE cascade.request_id = NEW.id
       AND cascade.state = 'failed'
       AND cascade.identity_key IS NOT NULL
       AND NEW.service IN ('lazylibrarian', 'shelfarr')
       AND (
           cascade.final_backend = NEW.service
           OR (cascade.final_backend IS NULL
               AND cascade.mutation_backend = NEW.service)
       )
       AND attempt.backend = NEW.service
       AND attempt.external_id IS NOT NULL
       AND NEW.external_id IS NOT NULL
       AND lower(attempt.external_id) = lower(NEW.external_id)
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'Blocked unavailable retry lacks exact final-import correlation'
    );
END;

DROP TRIGGER IF EXISTS unavailable_retry_terminal_sync;
CREATE TRIGGER unavailable_retry_terminal_sync
AFTER UPDATE OF status ON requests
WHEN NEW.status IN ('complete', 'completed')
 AND OLD.status NOT IN ('complete', 'completed')
 AND EXISTS (
     SELECT 1 FROM unavailable_retries
     WHERE request_id = NEW.id
       AND state IN ('retrying', 'awaiting_import', 'blocked')
 )
BEGIN
    -- A post-import failure can race the finalizer.  Restore only a blocked
    -- current attempt whose persisted backend and external ID still match the
    -- terminal request exactly; this is final proof, never a new acquisition.
    UPDATE ebook_backend_attempts
    SET status = 'completed', finished_at = CURRENT_TIMESTAMP,
        mutation_resolved_at = CASE
            WHEN mutation_started_at IS NOT NULL THEN CURRENT_TIMESTAMP
            ELSE mutation_resolved_at
        END,
        external_status = 'completed'
    WHERE request_id = NEW.id
      AND ordinal = (
          SELECT current_ordinal FROM ebook_cascades WHERE request_id = NEW.id
      )
      AND backend = NEW.service
      AND external_id IS NOT NULL
      AND NEW.external_id IS NOT NULL
      AND lower(external_id) = lower(NEW.external_id)
      AND EXISTS (
          SELECT 1 FROM unavailable_retries
          WHERE request_id = NEW.id AND state = 'blocked'
      )
      AND EXISTS (
          SELECT 1 FROM ebook_cascades
          WHERE request_id = NEW.id AND state = 'failed'
            AND identity_key IS NOT NULL
            AND (
                final_backend = NEW.service
                OR (final_backend IS NULL AND mutation_backend = NEW.service)
            )
      );

    UPDATE ebook_cascades
    SET state = 'completed',
        final_backend = COALESCE(final_backend, NEW.service),
        finalizer = COALESCE(
            finalizer,
            CASE NEW.service
                WHEN 'lazylibrarian' THEN 'bookbot'
                WHEN 'shelfarr' THEN 'shelfarr'
            END
        ),
        updated_at = CURRENT_TIMESTAMP
    WHERE request_id = NEW.id AND state = 'failed'
      AND EXISTS (
          SELECT 1 FROM unavailable_retries
          WHERE request_id = NEW.id AND state = 'blocked'
      )
      AND EXISTS (
          SELECT 1 FROM ebook_backend_attempts
          WHERE request_id = NEW.id
            AND ordinal = ebook_cascades.current_ordinal
            AND status = 'completed'
            AND backend = NEW.service
            AND external_id IS NOT NULL
            AND NEW.external_id IS NOT NULL
            AND lower(external_id) = lower(NEW.external_id)
      );

    UPDATE unavailable_retries
    SET state = 'fulfilled', final_import_state = 'verified',
        fulfilled_at = CURRENT_TIMESTAMP, next_retry_at = NULL,
        expired_at = NULL, updated_at = CURRENT_TIMESTAMP
    WHERE request_id = NEW.id
      AND state IN ('retrying', 'awaiting_import', 'blocked')
      AND (
          state != 'blocked'
          OR EXISTS (
              SELECT 1 FROM ebook_cascades
              WHERE request_id = NEW.id AND state = 'completed'
                AND (
                    (final_backend = 'lazylibrarian' AND finalizer = 'bookbot')
                    OR (final_backend = 'shelfarr' AND finalizer = 'shelfarr')
                )
          )
      );

    UPDATE requests
    SET notified_at = NULL
    WHERE id = NEW.id
      AND EXISTS (
          SELECT 1 FROM unavailable_retries
          WHERE request_id = NEW.id AND state = 'fulfilled'
      );

    INSERT INTO events (request_id, event_type, message)
    SELECT NEW.id,
           'unavailable_retry_fulfilled',
           'Unavailable retry resolved by verified final-library import'
    WHERE EXISTS (
        SELECT 1 FROM unavailable_retries
        WHERE request_id = NEW.id AND state = 'fulfilled'
    );
END;

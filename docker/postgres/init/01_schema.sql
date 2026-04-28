-- Final schema for MatrixMatch on PostgreSQL.
-- Runs once, on a fresh Docker volume (see docker-entrypoint-initdb.d).
-- Renames: MySQL `user` → `users` (user is reserved in Postgres).
-- Types:   AUTO_INCREMENT INT → SERIAL
--          TINYINT(1)         → SMALLINT (Python code reads/writes 0/1 ints)
--          DECIMAL(5,2)       → NUMERIC(5,2)

CREATE TABLE users (
    researcher_id         SERIAL PRIMARY KEY,
    first_name            VARCHAR(100) NOT NULL,
    last_name             VARCHAR(100) NOT NULL,
    email                 VARCHAR(150) NOT NULL UNIQUE,
    password              VARCHAR(100) NOT NULL,
    role                  VARCHAR(20)  NOT NULL DEFAULT 'Researcher'
                          CHECK (role IN ('Admin', 'Researcher')),
    registered_date       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    must_change_password  SMALLINT     NOT NULL DEFAULT 0,
    is_active             SMALLINT     NOT NULL DEFAULT 1
);

CREATE TABLE documents (
    document_id        SERIAL PRIMARY KEY,
    title              VARCHAR(500) NOT NULL,
    abstract           TEXT         NOT NULL,
    academic_program   VARCHAR(255),
    authors            TEXT,
    key_features       TEXT,
    research_field     TEXT,
    research_field_other TEXT,
    source_file_path   TEXT
);

-- Normalized feature storage (label + description linked to documents).
-- Kept alongside legacy documents.key_features for backward compatibility.
CREATE TABLE document_key_features (
    feature_id    SERIAL PRIMARY KEY,
    document_id   INT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    sort_order    INT NOT NULL DEFAULT 0,
    label         VARCHAR(200) NOT NULL,
    description   TEXT NOT NULL
);
CREATE INDEX ix_document_key_features_document_id ON document_key_features(document_id);

CREATE TABLE comparison_history (
    history_id                SERIAL PRIMARY KEY,
    researcher_id             INT NOT NULL REFERENCES users(researcher_id),
    keywords                  TEXT NOT NULL,
    user_abstract             TEXT NOT NULL,
    academic_program_filter   VARCHAR(255) NOT NULL,
    similarity_threshold      NUMERIC(5,2) NOT NULL,
    top_matches               TEXT NOT NULL,
    gap_analysis              TEXT,
    feature_matrix            TEXT,
    created_at                TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE app_settings (
    setting_key    VARCHAR(100) PRIMARY KEY,
    setting_value  VARCHAR(500) NOT NULL
);

-- One-time self-service password reset tokens. The plaintext token is only
-- ever emailed; we store sha256(token) so a leaked DB doesn't leak valid
-- reset links. A row is "live" iff expires_at > now() AND used_at IS NULL.
CREATE TABLE password_reset_tokens (
    token_hash     CHAR(64) PRIMARY KEY,
    researcher_id  INT NOT NULL REFERENCES users(researcher_id) ON DELETE CASCADE,
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at     TIMESTAMP NOT NULL,
    used_at        TIMESTAMP
);
CREATE INDEX ix_password_reset_tokens_researcher ON password_reset_tokens(researcher_id);

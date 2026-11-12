-- 0001_initial: the eleven tables from docs/PLAN.md "Data contracts".
-- Timestamps are ISO 8601 UTC text. JSON columns are checked with json_valid.

CREATE TABLE signals (
    signal_id        TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    board_slug       TEXT,
    company_name_raw TEXT NOT NULL,
    title            TEXT NOT NULL,
    role_family      TEXT,
    offer_type       TEXT,
    location         TEXT,
    posted_at        TEXT,
    url              TEXT,
    first_seen       TEXT NOT NULL,
    company_id       TEXT REFERENCES companies(company_id)
);
CREATE INDEX signals_company ON signals(company_id);

CREATE TABLE companies (
    company_id     TEXT PRIMARY KEY,
    domain         TEXT UNIQUE,
    name           TEXT NOT NULL,
    segment        TEXT,
    status         TEXT NOT NULL DEFAULT 'sourced' CHECK (status IN (
                       'sourced', 'resolved', 'qualified', 'captured', 'extracted',
                       'diagnosed', 'built', 'approved', 'sent', 'replied',
                       'meeting', 'closed', 'dropped')),
    drop_reason    TEXT,
    drop_stage     TEXT,
    best_signal_id TEXT REFERENCES signals(signal_id),
    pilot          INTEGER NOT NULL DEFAULT 0 CHECK (pilot IN (0, 1)),
    hand           INTEGER NOT NULL DEFAULT 0 CHECK (hand IN (0, 1)),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK ((status = 'dropped') = (drop_reason IS NOT NULL AND drop_stage IS NOT NULL))
);
CREATE INDEX companies_status ON companies(status);

-- Status only moves forward, or to dropped; dropped is terminal.
CREATE TRIGGER companies_status_forward
BEFORE UPDATE OF status ON companies
WHEN NEW.status <> OLD.status AND (
    OLD.status = 'dropped'
    OR (NEW.status <> 'dropped' AND
        (CASE NEW.status
            WHEN 'sourced' THEN 1 WHEN 'resolved' THEN 2 WHEN 'qualified' THEN 3
            WHEN 'captured' THEN 4 WHEN 'extracted' THEN 5 WHEN 'diagnosed' THEN 6
            WHEN 'built' THEN 7 WHEN 'approved' THEN 8 WHEN 'sent' THEN 9
            WHEN 'replied' THEN 10 WHEN 'meeting' THEN 11 WHEN 'closed' THEN 12 END)
        <
        (CASE OLD.status
            WHEN 'sourced' THEN 1 WHEN 'resolved' THEN 2 WHEN 'qualified' THEN 3
            WHEN 'captured' THEN 4 WHEN 'extracted' THEN 5 WHEN 'diagnosed' THEN 6
            WHEN 'built' THEN 7 WHEN 'approved' THEN 8 WHEN 'sent' THEN 9
            WHEN 'replied' THEN 10 WHEN 'meeting' THEN 11 WHEN 'closed' THEN 12 END))
)
BEGIN
    SELECT RAISE(ABORT, 'company status may only move forward or to dropped');
END;

CREATE TABLE artifacts (
    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES companies(company_id),
    kind        TEXT NOT NULL,
    url         TEXT NOT NULL,
    outcome     TEXT NOT NULL CHECK (outcome IN (
                    'ok', 'not_found', 'blocked', 'dead', 'robots_denied')),
    fetched_at  TEXT NOT NULL,
    text_sha256 TEXT,
    cache_path  TEXT
);
CREATE INDEX artifacts_company ON artifacts(company_id, kind);

CREATE TABLE facts (
    fact_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id        TEXT NOT NULL REFERENCES companies(company_id),
    fact              TEXT NOT NULL,
    value             TEXT NOT NULL,
    artifact_kind     TEXT NOT NULL,
    quote             TEXT NOT NULL CHECK (length(trim(quote)) > 0),
    char_start        INTEGER NOT NULL CHECK (char_start >= 0),
    extractor_version TEXT NOT NULL,
    UNIQUE (company_id, fact, value, artifact_kind, char_start, extractor_version)
);

-- The only table holding personal data. Never placed in a prompt, log or test.
CREATE TABLE contacts (
    contact_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id       TEXT NOT NULL REFERENCES companies(company_id),
    name             TEXT,
    title            TEXT,
    source_url       TEXT,
    email            TEXT,
    email_confidence REAL CHECK (email_confidence BETWEEN 0 AND 1),
    channel          TEXT
);
CREATE INDEX contacts_company ON contacts(company_id);

CREATE TABLE gold (
    gold_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   TEXT NOT NULL REFERENCES companies(company_id),
    gap_type     TEXT CHECK (gap_type IN ('G1','G2','G3','G4','G5')),
    abstain      INTEGER NOT NULL CHECK (abstain IN (0, 1)),
    evidence     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence)),
    split        TEXT NOT NULL CHECK (split IN ('teach', 'held_out')),
    gaps_version TEXT NOT NULL,
    UNIQUE (company_id, gaps_version),
    CHECK ((abstain = 1) = (gap_type IS NULL))
);

CREATE TABLE diagnoses (
    diagnosis_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id       TEXT NOT NULL REFERENCES companies(company_id),
    prompt_version   TEXT NOT NULL,
    attempted        INTEGER NOT NULL CHECK (attempted IN (0, 1)),
    gap_type         TEXT CHECK (gap_type IN ('G1','G2','G3','G4','G5')),
    confidence       REAL CHECK (confidence BETWEEN 0 AND 1),
    claims           TEXT NOT NULL DEFAULT '[]'
                         CHECK (json_valid(claims) AND json_type(claims) = 'array'),
    abstain_reason   TEXT,
    need             TEXT,
    secondary        TEXT NOT NULL DEFAULT '[]'
                         CHECK (json_valid(secondary) AND json_type(secondary) = 'array'),
    cannot_see       TEXT,
    faithfulness     TEXT CHECK (faithfulness IS NULL OR json_valid(faithfulness)),
    verifier_verdict TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (company_id, prompt_version),
    CHECK ((attempted = 1 AND gap_type IS NOT NULL)
        OR (attempted = 0 AND gap_type IS NULL AND abstain_reason IS NOT NULL)),
    CHECK (attempted = 0 OR (length(trim(coalesce(need, ''))) > 0
                             AND length(trim(coalesce(cannot_see, ''))) > 0))
);

-- Claims (protocol/GAPS.md v1.1): each is an object with a boolean found and a
-- non-empty url. found = true needs a non-empty text quote; found = false (an
-- absence) must have a null or missing quote.
CREATE TRIGGER diagnoses_claim_quote_insert
BEFORE INSERT ON diagnoses
WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.claims)
    WHERE CASE
        WHEN type IS NOT 'object' THEN 1
        WHEN json_type(value, '$.url') IS NOT 'text' THEN 1
        WHEN length(trim(json_extract(value, '$.url'))) = 0 THEN 1
        WHEN json_type(value, '$.found') = 'true' THEN
            CASE
                WHEN json_type(value, '$.quote') IS NOT 'text' THEN 1
                WHEN length(trim(json_extract(value, '$.quote'))) = 0 THEN 1
                ELSE 0 END
        WHEN json_type(value, '$.found') = 'false' THEN
            CASE WHEN coalesce(json_type(value, '$.quote'), 'null') = 'null' THEN 0
                 ELSE 1 END
        ELSE 1 END)
BEGIN
    SELECT RAISE(ABORT, 'diagnoses claim: found needs quote and url; not found needs url and no quote');
END;

CREATE TRIGGER diagnoses_claim_quote_update
BEFORE UPDATE OF claims ON diagnoses
WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.claims)
    WHERE CASE
        WHEN type IS NOT 'object' THEN 1
        WHEN json_type(value, '$.url') IS NOT 'text' THEN 1
        WHEN length(trim(json_extract(value, '$.url'))) = 0 THEN 1
        WHEN json_type(value, '$.found') = 'true' THEN
            CASE
                WHEN json_type(value, '$.quote') IS NOT 'text' THEN 1
                WHEN length(trim(json_extract(value, '$.quote'))) = 0 THEN 1
                ELSE 0 END
        WHEN json_type(value, '$.found') = 'false' THEN
            CASE WHEN coalesce(json_type(value, '$.quote'), 'null') = 'null' THEN 0
                 ELSE 1 END
        ELSE 1 END)
BEGIN
    SELECT RAISE(ABORT, 'diagnoses claim: found needs quote and url; not found needs url and no quote');
END;

CREATE TABLE teardowns (
    teardown_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id     TEXT NOT NULL REFERENCES companies(company_id),
    slice_kind     TEXT,
    page_slug      TEXT UNIQUE,
    email_subject  TEXT,
    email_body     TEXT,
    review_state   TEXT NOT NULL DEFAULT 'pending',
    review_minutes REAL CHECK (review_minutes IS NULL OR review_minutes >= 0)
);
CREATE INDEX teardowns_company ON teardowns(company_id);

CREATE TABLE touches (
    touch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      TEXT NOT NULL REFERENCES companies(company_id),
    touch_no        INTEGER NOT NULL CHECK (touch_no >= 1),
    channel         TEXT NOT NULL,
    sent_at         TEXT,
    outcome         TEXT,
    reply_at        TEXT,
    applied         INTEGER CHECK (applied IS NULL OR applied IN (0, 1)),
    hubspot_deal_id TEXT,
    UNIQUE (company_id, touch_no)
);

CREATE TABLE llm_calls (
    call_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    stage          TEXT NOT NULL,
    company_id     TEXT REFERENCES companies(company_id),
    prompt_version TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    input_tokens   INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens  INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    cache_hit      INTEGER NOT NULL DEFAULT 0 CHECK (cache_hit IN (0, 1)),
    output         TEXT CHECK (output IS NULL OR json_valid(output)),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX llm_calls_key ON llm_calls(stage, company_id, prompt_version);

CREATE TABLE suppressions (
    email_or_domain TEXT PRIMARY KEY,
    reason          TEXT NOT NULL,
    added_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

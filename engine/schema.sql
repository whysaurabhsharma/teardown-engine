-- engine/schema.sql: the current full schema (version 19), for reading.
-- The database is built only from migrations/; tests/test_db.py checks the two
-- match. To change the schema, add a numbered migration, then update this file.


-- description_text was added by 0003_signal_description, fresh and india_located
-- by 0004_signal_filter, companies.review_reason by 0005_company_review and
-- companies.hq, hq_basis and hq_evidence by 0007_company_hq and
-- companies.headcount, reviewed_at and review_note by 0008_company_review_fields; they
-- are laid out the way SQLite stores an ALTER TABLE ADD COLUMN so this file
-- matches the migrated database.
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
, description_text TEXT, fresh INTEGER CHECK (fresh IS NULL OR fresh IN (0, 1)), india_located INTEGER
    CHECK (india_located IS NULL OR india_located IN (0, 1)));
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
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')), review_reason TEXT, hq TEXT CHECK (hq IS NULL OR hq IN ('india', 'abroad')), hq_basis TEXT, hq_evidence TEXT NOT NULL DEFAULT '[]'
    CHECK (json_valid(hq_evidence) AND json_type(hq_evidence) = 'array'), headcount INTEGER CHECK (headcount IS NULL OR headcount > 0), reviewed_at TEXT, review_note TEXT,
    CHECK ((status = 'dropped') = (drop_reason IS NOT NULL AND drop_stage IS NOT NULL))
);
CREATE INDEX companies_status ON companies(status);

-- 0008_company_review_fields: what the human review (Step 30) records.
--   headcount    approximate number of people, typed in `te resolve review`;
--                ICP.md's too_large (~1,000) is a human drop, never automatic.
--   reviewed_at  when a person accepted (at qualified) or dropped the company.
--   review_note  free text typed with the decision. Never a person's name.

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

-- 0007_company_hq: where a company's head office is, and the quotes that say so.
--   Written by `te resolve segment` (engine/resolve/segment.py, Step 28), which
--   sets companies.segment from it (protocol/ICP.md, "Segment and location rule").
--   hq           'india' or 'abroad'; NULL when not decided (no evidence, or
--                conflicting evidence: review_reason then reads
--                "segment_unclear: ...").
--   hq_basis     how hq was decided, e.g. 'entity', 'address',
--                'abroad_parent_india_subsidiary', 'seed_default'; NULL with hq.
--   hq_evidence  JSON array of the markers found, each an object with a
--                non-empty verbatim "quote" and the "url" of the page it was
--                read from (plus marker, side, kind, char_start). No quote, no
--                marker: the triggers below reject one without. Kept when hq
--                is NULL so the review sees what was found.
--   Why not facts: facts rows are E4 output tied to captured artifacts
--   (artifact_kind, extractor_version); E2 runs before capture and has no
--   artifacts to point at.
CREATE TRIGGER companies_hq_evidence_insert
BEFORE INSERT ON companies
WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.hq_evidence)
    WHERE type IS NOT 'object'
       OR json_type(value, '$.quote') IS NOT 'text'
       OR length(trim(json_extract(value, '$.quote'))) = 0
       OR json_type(value, '$.url') IS NOT 'text'
       OR length(trim(json_extract(value, '$.url'))) = 0)
BEGIN
    SELECT RAISE(ABORT, 'hq_evidence: every marker needs a quote and a url');
END;

CREATE TRIGGER companies_hq_evidence_update
BEFORE UPDATE OF hq_evidence ON companies
WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.hq_evidence)
    WHERE type IS NOT 'object'
       OR json_type(value, '$.quote') IS NOT 'text'
       OR length(trim(json_extract(value, '$.quote'))) = 0
       OR json_type(value, '$.url') IS NOT 'text'
       OR length(trim(json_extract(value, '$.url'))) = 0)
BEGIN
    SELECT RAISE(ABORT, 'hq_evidence: every marker needs a quote and a url');
END;

-- 0006_company_flags: pairs of companies a person should look at together.
--   Written by `te resolve dedupe` (engine/resolve/dedupe.py, Step 27) and read
--   by the review (Step 30). A flag never merges anything; it only says "these
--   two rows may be one company".
--   company_id < other_company_id: one row per unordered pair and kind.
--   kind   same_stem         same registrable label on different suffixes
--                            (acme.com / acme.in)
--          similar_name      normalised names score >= 90 (token_set_ratio)
--          placeholder_name  as similar_name, where one side is an
--                            unresolved placeholder (domain NULL)
--   score  the normalised-name token_set_ratio, 0..100.
--   detail a human-readable reason.
--   Deleting either company (Step 26 removes emptied placeholders) deletes
--   its flags.
CREATE TABLE company_flags (
    flag_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id       TEXT NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    other_company_id TEXT NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    kind             TEXT NOT NULL CHECK (kind IN ('same_stem', 'similar_name', 'placeholder_name')),
    score            REAL NOT NULL CHECK (score >= 0 AND score <= 100),
    detail           TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (company_id < other_company_id),
    UNIQUE (company_id, other_company_id, kind)
);
CREATE INDEX company_flags_other ON company_flags(other_company_id);

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
, status INTEGER, body_sha256 TEXT, fetcher TEXT
    CHECK (fetcher IS NULL OR fetcher IN ('http', 'playwright', 'dns', 'none')), detail TEXT, bundle_version TEXT, structure_sha256 TEXT, extractor_version TEXT);
CREATE INDEX artifacts_company ON artifacts(company_id, kind);

-- 0009_artifact_capture: status, body_sha256, fetcher, detail and bundle_version
-- are written by E3 capture (engine/capture/); one row per (company_id, kind).
CREATE UNIQUE INDEX artifacts_company_kind ON artifacts(company_id, kind);
-- 0010_artifact_text: text_sha256, structure_sha256 and extractor_version are
-- written by Step 35 (engine/capture/extract_text.py); text and structure live
-- in the cache under text/<sha256> and structure/<sha256>.

CREATE TABLE facts (
    fact_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id        TEXT NOT NULL REFERENCES companies(company_id),
    fact              TEXT NOT NULL,
    value             TEXT NOT NULL,
    artifact_kind     TEXT NOT NULL,
    quote             TEXT NOT NULL CHECK (length(trim(quote)) > 0),
    char_start        INTEGER NOT NULL CHECK (char_start >= 0),
    extractor_version TEXT NOT NULL, found INTEGER NOT NULL DEFAULT 1 CHECK (found IN (0, 1)), quote_source TEXT
    CHECK (quote_source IS NULL OR quote_source IN (
        'text', 'removed_text', 'structure', 'not_found', 'absent')),
    UNIQUE (company_id, fact, value, artifact_kind, char_start, extractor_version)
);
-- facts.found and facts.quote_source were added by 0011_fact_found; SQLite
-- places columns added by ALTER TABLE before the table constraints.
CREATE INDEX facts_company ON facts(company_id, fact);

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
, confidence TEXT
    CHECK (confidence IS NULL OR confidence IN ('high', 'medium', 'low')), email_source TEXT
    CHECK (email_source IS NULL OR email_source IN ('hunter', 'apollo', 'pattern', 'public', 'manual')), linkedin_url TEXT);
CREATE INDEX contacts_company ON contacts(company_id);
-- contacts.confidence, email_source and linkedin_url were added by
-- 0012_contact_people (the People step, engine/people/).

-- gold and diagnoses were rebuilt by 0013_gold_g6: G6 allowed in gap_type,
-- gold.abstain_reason (one of the three GAPS.md codes) and gold.notes added,
-- gold evidence checked by triggers.
CREATE TABLE gold (
    gold_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id     TEXT NOT NULL REFERENCES companies(company_id),
    gap_type       TEXT CHECK (gap_type IN ('G1','G2','G3','G4','G5','G6')),
    abstain        INTEGER NOT NULL CHECK (abstain IN (0, 1)),
    abstain_reason TEXT,
    evidence       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence)),
    split          TEXT NOT NULL CHECK (split IN ('teach', 'held_out')),
    gaps_version   TEXT NOT NULL,
    notes          TEXT,
    UNIQUE (company_id, gaps_version),
    CHECK ((abstain = 1) = (gap_type IS NULL)),
    CHECK ((abstain = 0 AND abstain_reason IS NULL)
        OR (abstain = 1 AND abstain_reason IS NOT NULL
            AND (substr(abstain_reason, 1, 12) = 'no_evidence:'
                 OR substr(abstain_reason, 1, 13) = 'contradicted:'
                 OR substr(abstain_reason, 1, 5) = 'thin:')
            AND length(trim(substr(abstain_reason, instr(abstain_reason, ':') + 1))) > 0))
);

CREATE TRIGGER gold_evidence_insert
BEFORE INSERT ON gold
WHEN json_type(NEW.evidence) IS NOT 'array' OR EXISTS (
    SELECT 1 FROM json_each(NEW.evidence)
    WHERE CASE
        WHEN type IS NOT 'object' THEN 1
        WHEN json_type(value, '$.kind') IS NOT 'text' THEN 1
        WHEN length(trim(json_extract(value, '$.kind'))) = 0 THEN 1
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
    SELECT RAISE(ABORT, 'gold evidence: an array of {kind, found, url, quote}; found needs a quote, not found has none');
END;

CREATE TRIGGER gold_evidence_update
BEFORE UPDATE OF evidence ON gold
WHEN json_type(NEW.evidence) IS NOT 'array' OR EXISTS (
    SELECT 1 FROM json_each(NEW.evidence)
    WHERE CASE
        WHEN type IS NOT 'object' THEN 1
        WHEN json_type(value, '$.kind') IS NOT 'text' THEN 1
        WHEN length(trim(json_extract(value, '$.kind'))) = 0 THEN 1
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
    SELECT RAISE(ABORT, 'gold evidence: an array of {kind, found, url, quote}; found needs a quote, not found has none');
END;

CREATE TABLE diagnoses (
    diagnosis_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id       TEXT NOT NULL REFERENCES companies(company_id),
    prompt_version   TEXT NOT NULL,
    attempted        INTEGER NOT NULL CHECK (attempted IN (0, 1)),
    gap_type         TEXT CHECK (gap_type IN ('G1','G2','G3','G4','G5','G6')),
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
-- absence) must have a null or missing quote. Unchanged from 0001.
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
    hubspot_deal_id TEXT, draft_id TEXT, drafted_at TEXT, status TEXT
    CHECK (status IS NULL OR status IN ('drafted', 'sent')), applied_on TEXT, applied_note TEXT, bounced_on TEXT,
    UNIQUE (company_id, touch_no)
);
-- touches.draft_id, drafted_at and status were added by 0015_touch_drafts
-- (`te queue draft` / `te queue sent`, engine/outreach/draft.py): the Gmail
-- draft id (NULL for a LinkedIn touch sent by hand), when the engine drafted
-- it, and drafted | sent. The engine never sends; a person confirms a send.
-- touches.applied_on, applied_note and bounced_on were added by
-- 0018_review_followups: the application date (`te queue applied`, Step 75)
-- and the date a message bounced (`te queue bounce`, Step 74).

-- cost_usd was added by 0002_llm_cost; it is laid out the way SQLite stores
-- an ALTER TABLE ADD COLUMN so this file matches the migrated database.
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
, cost_usd REAL CHECK (cost_usd IS NULL OR cost_usd >= 0));
CREATE INDEX llm_calls_key ON llm_calls(stage, company_id, prompt_version);

CREATE TABLE suppressions (
    email_or_domain TEXT PRIMARY KEY,
    reason          TEXT NOT NULL,
    added_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- judge_scores was added by 0014_judge_scores: one row per `te judge score`
-- result (Step 50); each prompt_version scored is one round, at most three.
CREATE TABLE judge_scores (
    score_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_version        TEXT NOT NULL,
    held_out              INTEGER NOT NULL CHECK (held_out >= 0),
    same_call             INTEGER NOT NULL CHECK (same_call >= 0),
    false_gap             INTEGER NOT NULL CHECK (false_gap >= 0),
    faithfulness_failures INTEGER NOT NULL CHECK (faithfulness_failures >= 0),
    not_judged            INTEGER NOT NULL CHECK (not_judged >= 0),
    passed                INTEGER NOT NULL CHECK (passed IN (0, 1)),
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX judge_scores_version ON judge_scores(prompt_version);

-- slices, slice_competitors and slice_artifacts were added by 0016_slices: the
-- proof slices (Steps 60-63), the competitors a person confirmed, and the
-- pages fetched for a slice, kept apart from the company's own bundle.
CREATE TABLE slices (
    slice_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      TEXT NOT NULL REFERENCES companies(company_id),
    kind            TEXT NOT NULL CHECK (kind IN (
                        'icp_accounts', 'funnel_compare', 'role_funnel', 'dns_inventory')),
    builder_version TEXT NOT NULL,
    outcome         TEXT NOT NULL CHECK (outcome IN ('ok', 'failed')),
    reason          TEXT,
    title           TEXT NOT NULL,
    columns         TEXT NOT NULL DEFAULT '[]'
                        CHECK (json_valid(columns) AND json_type(columns) = 'array'),
    rows            TEXT NOT NULL DEFAULT '[]'
                        CHECK (json_valid(rows) AND json_type(rows) = 'array'),
    dropped         TEXT NOT NULL DEFAULT '[]'
                        CHECK (json_valid(dropped) AND json_type(dropped) = 'array'),
    meta            TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta)),
    inputs_key      TEXT,
    selected        INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
    fetches         INTEGER NOT NULL DEFAULT 0 CHECK (fetches >= 0),
    searches        INTEGER NOT NULL DEFAULT 0 CHECK (searches >= 0),
    built_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (company_id, kind),
    CHECK ((outcome = 'ok') = (reason IS NULL)),
    CHECK (selected = 0 OR outcome = 'ok')
);
CREATE UNIQUE INDEX slices_selected ON slices(company_id) WHERE selected = 1;

CREATE TRIGGER slices_row_source_insert
BEFORE INSERT ON slices
WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.rows)
    WHERE type IS NOT 'object'
       OR json_type(value, '$.url') IS NOT 'text'
       OR length(trim(json_extract(value, '$.url'))) = 0
       OR json_type(value, '$.quote') IS NOT 'text'
       OR length(trim(json_extract(value, '$.quote'))) = 0)
BEGIN
    SELECT RAISE(ABORT, 'slices row: every row needs a url and a quote');
END;

CREATE TRIGGER slices_row_source_update
BEFORE UPDATE OF rows ON slices
WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.rows)
    WHERE type IS NOT 'object'
       OR json_type(value, '$.url') IS NOT 'text'
       OR length(trim(json_extract(value, '$.url'))) = 0
       OR json_type(value, '$.quote') IS NOT 'text'
       OR length(trim(json_extract(value, '$.quote'))) = 0)
BEGIN
    SELECT RAISE(ABORT, 'slices row: every row needs a url and a quote');
END;

CREATE TABLE slice_competitors (
    company_id     TEXT NOT NULL REFERENCES companies(company_id),
    name           TEXT NOT NULL CHECK (length(trim(name)) > 0),
    domain         TEXT,
    source         TEXT NOT NULL CHECK (source IN (
                       'named_competitors', 'alternatives_search', 'manual')),
    evidence_url   TEXT,
    evidence_quote TEXT,
    rank           INTEGER NOT NULL CHECK (rank >= 1),
    confirmed      INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0, 1)),
    confirmed_at   TEXT,
    UNIQUE (company_id, name),
    CHECK (confirmed = 0 OR (domain IS NOT NULL AND confirmed_at IS NOT NULL))
);

CREATE TABLE slice_artifacts (
    slice_artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id        TEXT NOT NULL REFERENCES companies(company_id),
    slice_kind        TEXT NOT NULL,
    subject_domain    TEXT NOT NULL,
    kind              TEXT NOT NULL,
    url               TEXT NOT NULL,
    outcome           TEXT NOT NULL CHECK (outcome IN (
                          'ok', 'not_found', 'blocked', 'dead', 'robots_denied')),
    fetched_at        TEXT NOT NULL,
    status            INTEGER,
    body_sha256       TEXT,
    fetcher           TEXT CHECK (fetcher IS NULL OR fetcher IN ('http', 'none')),
    detail            TEXT,
    text_sha256       TEXT,
    structure_sha256  TEXT,
    extractor_version TEXT,
    UNIQUE (company_id, slice_kind, subject_domain, kind)
);

-- quality_checks was added by 0017_quality: the latest automated quality-gate
-- result per company (Step 65, `te quality`), with the reasons a teardown was
-- failed before human review, for the review queue (Step 66).
CREATE TABLE quality_checks (
    company_id  TEXT PRIMARY KEY REFERENCES companies(company_id),
    version     TEXT NOT NULL,
    passed      INTEGER NOT NULL CHECK (passed IN (0, 1)),
    reasons     TEXT NOT NULL DEFAULT '[]'
                    CHECK (json_valid(reasons) AND json_type(reasons) = 'array'),
    file_sha256 TEXT,
    checked_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK ((passed = 1) = (json_array_length(reasons) = 0))
);

-- reviews and send_stops were added by 0018_review_followups (Steps 66, 74).
-- reviews: one row per decision a person takes in the review queue (`te queue
-- open`, Step 66).
--   decision   approve | kill | edited (edited: the person changed the file and
--              said so; approve and kill also snapshot the edits)
--   reason     the kill reason code (engine/outreach/review.py KILL_REASONS);
--              set exactly when decision = kill
--   note       free text typed with the decision. Never a person's name or contact.
--   edited     JSON object: drafted section -> fraction of its text changed
--              against the drafter's output (0..1), or null when the drafted
--              text is not available to compare; a section absent was not edited
--   minutes    review minutes: typed, or the time from opening the page
--   file_sha256  the teardown file the decision was taken on
--   version    the queue's version string (review.REVIEW_VERSION)
CREATE TABLE reviews (
    review_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES companies(company_id),
    decision    TEXT NOT NULL CHECK (decision IN ('approve', 'kill', 'edited')),
    reason      TEXT,
    note        TEXT,
    edited      TEXT NOT NULL DEFAULT '{}'
                    CHECK (json_valid(edited) AND json_type(edited) = 'object'),
    minutes     REAL CHECK (minutes IS NULL OR minutes > 0),
    file_sha256 TEXT,
    version     TEXT NOT NULL,
    decided_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK ((decision = 'kill') = (reason IS NOT NULL AND length(trim(reason)) > 0))
);
CREATE INDEX reviews_company ON reviews(company_id);

-- send_stops: the global stop on drafting (CONDUCT Part 3, "Bounces"; Step 74).
-- Two bounces on one UTC day write a row; `te queue draft` refuses while a row
-- has resumed_at NULL; `te queue resume --reason ...` sets resumed_at and
-- resume_reason after the remaining addresses are re-verified.
CREATE TABLE send_stops (
    stop_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    reason        TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    stopped_on    TEXT NOT NULL,
    stopped_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    resumed_at    TEXT,
    resume_reason TEXT,
    CHECK ((resumed_at IS NULL) = (resume_reason IS NULL))
);

-- replies, meetings, closes and hubspot_sync were added by 0019_replies_meetings_sync
-- (Steps 67, 76, 77, 82): replies logged by hand (`te reply`), calls held
-- (`te meeting --held`), the conversation outcome (`te close`) and the HubSpot
-- ids `te sync` stores. See the migration for each column.
CREATE TABLE replies (
    reply_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES companies(company_id),
    touch_no    INTEGER NOT NULL CHECK (touch_no >= 1),
    type        TEXT NOT NULL CHECK (type IN (
                    'positive', 'question', 'referral', 'not_now', 'negative', 'optout')),
    replied_on  TEXT NOT NULL,
    optout      INTEGER NOT NULL DEFAULT 0 CHECK (optout IN (0, 1)),
    note        TEXT,
    logged_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (company_id, touch_no, type, replied_on),
    CHECK (type <> 'optout' OR optout = 1),
    CHECK (optout = 0 OR type IN ('optout', 'negative'))
);
CREATE INDEX replies_company ON replies(company_id);

CREATE TABLE meetings (
    meeting_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES companies(company_id),
    held_on     TEXT NOT NULL,
    minutes     REAL CHECK (minutes IS NULL OR minutes > 0),
    note        TEXT,
    logged_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (company_id, held_on)
);

CREATE TABLE closes (
    company_id  TEXT PRIMARY KEY REFERENCES companies(company_id),
    outcome     TEXT NOT NULL CHECK (outcome IN (
                    'hiring_process', 'paid_project', 'not_now', 'no_fit', 'other')),
    next_step   TEXT NOT NULL CHECK (length(trim(next_step)) > 0),
    closed_on   TEXT NOT NULL,
    logged_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE hubspot_sync (
    object       TEXT NOT NULL CHECK (object IN ('company', 'contact', 'deal', 'assoc')),
    te_key       TEXT NOT NULL CHECK (length(trim(te_key)) > 0),
    hubspot_id   TEXT NOT NULL CHECK (length(trim(hubspot_id)) > 0),
    props_sha256 TEXT,
    synced_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (object, te_key)
);

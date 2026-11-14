-- 0016_slices: the proof slices (docs/PLAN.md Stage 5.2, Steps 60-63;
-- engine/build/slices/, `te slice`).
--   slices             one row per (company, slice kind) built: icp_accounts,
--                      funnel_compare, role_funnel or the provisional G6
--                      dns_inventory. outcome 'ok' or 'failed' (reason says
--                      why, e.g. "search not configured" or "thin: 2 rows,
--                      needs 3"). rows is a JSON array; every row carries a
--                      non-empty url and quote (checked by the triggers below;
--                      the builders drop a row without both before storage).
--                      selected = 1 marks the slice the teardown uses: the
--                      gap's primary slice when it is ok, else role_funnel;
--                      at most one per company. builder_version is the
--                      builder's version string; inputs_key fingerprints what
--                      a network-backed builder was given, so a re-run with
--                      the same inputs does not fetch again.
--   slice_competitors  the two competitors funnel_compare compares against
--                      (Step 61): from the company's own named_competitors
--                      facts first, else one "alternatives" search, or typed
--                      in with `te slice competitors --set`. confirmed = 1 only
--                      after a person said yes; nothing about a competitor is
--                      captured before that, and `te teardown check` refuses
--                      a competitor named on the page that is not confirmed here.
--   slice_artifacts    pages fetched for a slice (a competitor's homepage,
--                      pricing and demo form; a candidate account's homepage,
--                      careers page and job board). Kept apart from the
--                      artifacts table so a company's own bundle (one row per
--                      kind, MIN_USABLE, section 2 of the page) never mixes
--                      with other companies' pages. Same outcome enum and
--                      cache columns as artifacts.
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

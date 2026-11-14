-- 0017_quality: the automated quality gates (docs/PLAN.md Step 65;
-- engine/build/quality.py, `te quality`). One row per company: the latest
-- gate result for its teardown file, read by the review queue (Step 66) to
-- show why a teardown was failed before a person saw it.
--   version      the gate set's version string (quality.QUALITY_VERSION)
--   passed       1 when no gate failed
--   reasons      JSON array of {gate, message}; empty exactly when passed
--   file_sha256  the teardown file the gates read, so a stale result shows
--   checked_at   when the result last changed
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

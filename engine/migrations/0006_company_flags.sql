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

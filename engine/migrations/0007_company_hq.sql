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
ALTER TABLE companies ADD COLUMN hq TEXT CHECK (hq IS NULL OR hq IN ('india', 'abroad'));
ALTER TABLE companies ADD COLUMN hq_basis TEXT;
ALTER TABLE companies ADD COLUMN hq_evidence TEXT NOT NULL DEFAULT '[]'
    CHECK (json_valid(hq_evidence) AND json_type(hq_evidence) = 'array');

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

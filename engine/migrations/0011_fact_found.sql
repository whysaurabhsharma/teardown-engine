-- 0011_fact_found: what E4 extract (docs/PLAN.md Step 37, engine/extract/)
-- records for each fact, beyond the 0001 columns (protocol/GAPS.md v1.2,
-- "Evidence rules": found vs not found).
--   found         1 when the fact says something is there (a price, a tool, an
--                 SPF record); 0 when it records an absence. An absence rests
--                 either on an artifact whose capture outcome is not_found, or
--                 on an artifact that was seen (ok) and holds no match. Facts
--                 are never written from blocked, dead or robots_denied
--                 artifacts: unseen is not absent.
--   quote_source  where the quote can be checked:
--                   text          a verbatim substring of the artifact's
--                                 extracted text (cache/text/)
--                   removed_text  of its nav or footer text (structure
--                                 removed_text)
--                   structure     a string stored in its structure JSON (a
--                                 link's text or href, a field name, a script
--                                 or iframe src)
--                   not_found     the artifact's outcome is not_found; the
--                                 quote is the fixed "not_found: <kind>"
--                   absent        the artifact was seen and nothing matched;
--                                 the quote is the fixed "absent: <what> in <kind>"
--                 NULL on rows written before 0011.
ALTER TABLE facts ADD COLUMN found INTEGER NOT NULL DEFAULT 1 CHECK (found IN (0, 1));
ALTER TABLE facts ADD COLUMN quote_source TEXT
    CHECK (quote_source IS NULL OR quote_source IN (
        'text', 'removed_text', 'structure', 'not_found', 'absent'));
CREATE INDEX facts_company ON facts(company_id, fact);

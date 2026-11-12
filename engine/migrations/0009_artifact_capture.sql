-- 0009_artifact_capture: what E3 capture (docs/PLAN.md Steps 32-34,
-- engine/capture/) records for each artifact, beyond the 0001 columns.
--   status          final HTTP status of the fetch; NULL when nothing was
--                   fetched (not_found by discovery, policy refusals) or for dns.
--   body_sha256     SHA-256 of the raw body stored under data/cache/<sha256>.
--                   Not text_sha256: that is the hash of the extracted text,
--                   written by Step 35.
--   fetcher         'http' (guard.http), 'playwright' (guard.browser fallback),
--                   'dns' (guard.dns) or 'none' (no request: the kind had no
--                   matching link, or policy forbids the fetch).
--   detail          why, in words: "no matching link in nav, footer or sitemap",
--                   "HTTP 403", "playwright retry: 120 chars of text".
--   bundle_version  engine/capture/bundle.py BUNDLE_VERSION the row was captured
--                   under; a bumped version re-captures.
-- One row per (company_id, kind): a re-run or --recheck updates it in place.
ALTER TABLE artifacts ADD COLUMN status INTEGER;
ALTER TABLE artifacts ADD COLUMN body_sha256 TEXT;
ALTER TABLE artifacts ADD COLUMN fetcher TEXT
    CHECK (fetcher IS NULL OR fetcher IN ('http', 'playwright', 'dns', 'none'));
ALTER TABLE artifacts ADD COLUMN detail TEXT;
ALTER TABLE artifacts ADD COLUMN bundle_version TEXT;
CREATE UNIQUE INDEX artifacts_company_kind ON artifacts(company_id, kind);

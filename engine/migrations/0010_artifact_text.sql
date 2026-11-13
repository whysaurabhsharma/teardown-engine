-- 0010_artifact_text: what Step 35 (engine/capture/extract_text.py) records
-- for each ok artifact, beyond text_sha256 (0001).
--   text_sha256        SHA-256 of the extracted text's UTF-8 bytes; the text is
--                      stored under data/cache/text/<text_sha256>.
--   structure_sha256   SHA-256 of the structure JSON (links, buttons, forms,
--                      iframes, jobs, DNS summary), stored under
--                      data/cache/structure/<structure_sha256>.
--   extractor_version  extract_text.EXTRACTOR_VERSION the text was derived
--                      under ("ex-1.0"). NULL means not extracted: a row
--                      whose text_sha256 is set but extractor_version is NULL
--                      predates extraction and its text_sha256 names a raw body.
-- All three are derived from body_sha256 and cleared together by capture when
-- the body changes; a bumped extractor version re-derives them from the cache
-- with no request.
ALTER TABLE artifacts ADD COLUMN structure_sha256 TEXT;
ALTER TABLE artifacts ADD COLUMN extractor_version TEXT;

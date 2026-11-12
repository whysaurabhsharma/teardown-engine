-- 0003_signal_description: the job post's description as plain text, read from
-- the board's JSON by the ATS collectors (docs/PLAN.md Step 21). NULL when the
-- board gives none. The raw JSON stays in the cache; this is derived from it.
ALTER TABLE signals ADD COLUMN description_text TEXT;

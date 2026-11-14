-- 0015_touch_drafts: what `te queue draft` and `te queue sent` (docs/PLAN.md
-- Steps 54, 55, 70, 72; engine/outreach/draft.py) record on a touch, beyond
-- the 0001 columns. The engine never sends: a touch is drafted by the engine
-- and sent by a person, who then confirms it with `te queue sent`.
--   draft_id    the Gmail draft id returned by users.drafts.create; NULL for a
--               touch with no Gmail draft (a LinkedIn message sent by hand).
--   drafted_at  when the engine made the draft (or printed the LinkedIn text),
--               after the send-day recheck passed.
--   status      'drafted' (made, not yet sent) or 'sent' (a person sent it and
--               ran `te queue sent`; sent_at is set with it). NULL for a row
--               written before this migration.
-- Review minutes (Step 55, `te queue done`) go in teardowns.review_minutes,
-- which 0001 already has.
ALTER TABLE touches ADD COLUMN draft_id TEXT;
ALTER TABLE touches ADD COLUMN drafted_at TEXT;
ALTER TABLE touches ADD COLUMN status TEXT
    CHECK (status IS NULL OR status IN ('drafted', 'sent'));

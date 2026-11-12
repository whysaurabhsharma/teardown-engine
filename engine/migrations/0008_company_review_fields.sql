-- 0008_company_review_fields: what the human review (Step 30) records.
--   Written by `te resolve review` (engine/resolve/review.py).
--   headcount    approximate number of people, typed by a person; NULL when
--                not typed. protocol/ICP.md drops a company above roughly
--                1,000 as too_large, but only a person decides that.
--   reviewed_at  UTC time a person accepted the company at qualified, or
--                dropped it, in the review. NULL: not yet reviewed.
--   review_note  free text a person typed with the decision (why dropped,
--                which hold was overridden). Never a person's name or contact.
ALTER TABLE companies ADD COLUMN headcount INTEGER CHECK (headcount IS NULL OR headcount > 0);
ALTER TABLE companies ADD COLUMN reviewed_at TEXT;
ALTER TABLE companies ADD COLUMN review_note TEXT;

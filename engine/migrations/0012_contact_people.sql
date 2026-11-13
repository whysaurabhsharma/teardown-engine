-- 0012_contact_people: what the People step (docs/PLAN.md Steps 40-43,
-- engine/people/, `te people add`) records about a recipient, beyond the 0001
-- columns. contacts stays the only table holding personal data.
--   confidence    the address confidence the plan names (Step 42): high,
--                 medium or low. NULL when no address is stored. This is the
--                 authoritative value; the 0001 email_confidence column is
--                 kept and filled from it by a fixed map (engine/people/
--                 contacts.py CONFIDENCE_VALUE) so the frozen contract still
--                 reads.
--   email_source  where the address came from: hunter, apollo, pattern
--                 (inferred from a public address at the same domain), public
--                 (published on a page), manual. NULL when no address.
--   linkedin_url  the recipient's LinkedIn profile URL, typed by a person
--                 (never fetched, never scraped). Personal data: the model
--                 gateway's contacts guard and every redaction cover it.
ALTER TABLE contacts ADD COLUMN confidence TEXT
    CHECK (confidence IS NULL OR confidence IN ('high', 'medium', 'low'));
ALTER TABLE contacts ADD COLUMN email_source TEXT
    CHECK (email_source IS NULL OR email_source IN ('hunter', 'apollo', 'pattern', 'public', 'manual'));
ALTER TABLE contacts ADD COLUMN linkedin_url TEXT;

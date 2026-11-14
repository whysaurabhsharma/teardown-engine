-- 0018_review_followups: the review queue, follow-ups, bounces and the
-- application flag (docs/PLAN.md Steps 66, 71, 74, 75; engine/outreach/).
--
-- touches, beyond 0001 and 0015:
--   applied_on    the UTC date a person submitted the application through the
--                 company's own form (`te queue applied`, Step 75); set with
--                 applied = 1 on touch 1, which may then hold no draft yet.
--   applied_note  free text typed with it. Never a person's name or contact.
--   bounced_on    the UTC date the message on this touch bounced (`te queue
--                 bounce`, Step 74); outcome is 'bounced' with it.
-- Touches 2 and 3 (`te queue followup-sent`, Step 71) are rows with channel
-- 'email-reply' or 'linkedin', status 'sent' and sent_at; no draft.
ALTER TABLE touches ADD COLUMN applied_on TEXT;
ALTER TABLE touches ADD COLUMN applied_note TEXT;
ALTER TABLE touches ADD COLUMN bounced_on TEXT;

-- reviews: one row per decision a person takes in the review queue (`te queue
-- open`, Step 66).
--   decision   approve | kill | edited (edited: the person changed the file and
--              said so; approve and kill also snapshot the edits)
--   reason     the kill reason code (engine/outreach/review.py KILL_REASONS);
--              set exactly when decision = kill
--   note       free text typed with the decision. Never a person's name or contact.
--   edited     JSON object: drafted section -> fraction of its text changed
--              against the drafter's output (0..1), or null when the drafted
--              text is not available to compare; a section absent was not edited
--   minutes    review minutes: typed, or the time from opening the page
--   file_sha256  the teardown file the decision was taken on
--   version    the queue's version string (review.REVIEW_VERSION)
CREATE TABLE reviews (
    review_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES companies(company_id),
    decision    TEXT NOT NULL CHECK (decision IN ('approve', 'kill', 'edited')),
    reason      TEXT,
    note        TEXT,
    edited      TEXT NOT NULL DEFAULT '{}'
                    CHECK (json_valid(edited) AND json_type(edited) = 'object'),
    minutes     REAL CHECK (minutes IS NULL OR minutes > 0),
    file_sha256 TEXT,
    version     TEXT NOT NULL,
    decided_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK ((decision = 'kill') = (reason IS NOT NULL AND length(trim(reason)) > 0))
);
CREATE INDEX reviews_company ON reviews(company_id);

-- send_stops: the global stop on drafting (CONDUCT Part 3, "Bounces"; Step 74).
-- Two bounces on one UTC day write a row; `te queue draft` refuses while a row
-- has resumed_at NULL; `te queue resume --reason ...` sets resumed_at and
-- resume_reason after the remaining addresses are re-verified.
CREATE TABLE send_stops (
    stop_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    reason        TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    stopped_on    TEXT NOT NULL,
    stopped_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    resumed_at    TEXT,
    resume_reason TEXT,
    CHECK ((resumed_at IS NULL) = (resume_reason IS NULL))
);

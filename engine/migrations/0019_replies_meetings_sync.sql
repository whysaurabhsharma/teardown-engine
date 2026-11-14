-- 0019_replies_meetings_sync: replies, meetings and conversation closes logged
-- by hand, and the HubSpot ids `te sync` stores (docs/PLAN.md Steps 67, 76, 77,
-- 82; engine/outreach/replies.py, engine/outreach/hubspot_sync.py).
--
-- replies: one row per reply a person logged with `te reply` (Step 77). The
-- latest sent touch also carries it: touches.reply_at (the first reply) and
-- touches.outcome 'replied_<type>' (the latest).
--   touch_no    the latest sent touch when the reply was logged
--   type        positive | question | referral | not_now | negative | optout
--   replied_on  the UTC date the reply arrived
--   optout      1 when the reply asked to stop (type optout, or an annoyed
--               negative logged with --optout): the address and the domain went
--               into suppressions the same day (CONDUCT Part 3, Step 72)
--   note        free text typed with it. Never a person's name or contact.
CREATE TABLE replies (
    reply_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES companies(company_id),
    touch_no    INTEGER NOT NULL CHECK (touch_no >= 1),
    type        TEXT NOT NULL CHECK (type IN (
                    'positive', 'question', 'referral', 'not_now', 'negative', 'optout')),
    replied_on  TEXT NOT NULL,
    optout      INTEGER NOT NULL DEFAULT 0 CHECK (optout IN (0, 1)),
    note        TEXT,
    logged_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (company_id, touch_no, type, replied_on),
    CHECK (type <> 'optout' OR optout = 1),
    CHECK (optout = 0 OR type IN ('optout', 'negative'))
);
CREATE INDEX replies_company ON replies(company_id);

-- meetings: one row per call actually held (`te meeting --held`, Step 77).
--   held_on  the UTC date of the call
--   minutes  how long the call took, when typed; not part of hours per meeting
--   note     free text. Never a person's name or contact.
CREATE TABLE meetings (
    meeting_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES companies(company_id),
    held_on     TEXT NOT NULL,
    minutes     REAL CHECK (minutes IS NULL OR minutes > 0),
    note        TEXT,
    logged_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (company_id, held_on)
);

-- closes: the outcome of a conversation and the next step (`te close`, Step 82);
-- one row per company, the company moves to closed with it.
--   outcome    hiring_process | paid_project | not_now | no_fit | other
--   next_step  what happens next, typed. Never a person's name or contact.
--   closed_on  the UTC date it was logged for
CREATE TABLE closes (
    company_id  TEXT PRIMARY KEY REFERENCES companies(company_id),
    outcome     TEXT NOT NULL CHECK (outcome IN (
                    'hiring_process', 'paid_project', 'not_now', 'no_fit', 'other')),
    next_step   TEXT NOT NULL CHECK (length(trim(next_step)) > 0),
    closed_on   TEXT NOT NULL,
    logged_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- hubspot_sync: the HubSpot record each local record was written to (`te sync`,
-- Steps 67, 76). SQLite stays the system of record; HubSpot is the view.
--   object       company | contact | deal | assoc (an association written once)
--   te_key       company_id for company, deal and assoc rows (assoc: company_id
--                plus the pair, e.g. "acme-com:deal-contact"); contact_id for a
--                contact
--   hubspot_id   the HubSpot record id (assoc: the two ids joined)
--   props_sha256 the properties last written, hashed; a re-run whose properties
--                hash the same sends no request
--   synced_at    when the row last changed
-- No contact value is stored here, only ids and a hash.
CREATE TABLE hubspot_sync (
    object       TEXT NOT NULL CHECK (object IN ('company', 'contact', 'deal', 'assoc')),
    te_key       TEXT NOT NULL CHECK (length(trim(te_key)) > 0),
    hubspot_id   TEXT NOT NULL CHECK (length(trim(hubspot_id)) > 0),
    props_sha256 TEXT,
    synced_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (object, te_key)
);

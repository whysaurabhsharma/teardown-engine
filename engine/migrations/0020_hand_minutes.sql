-- 0020_hand_minutes: what a Phase 0 teardown cost by hand (docs/PLAN.md Step 30,
-- `te company add --hand --minutes-research N --minutes-writing N
-- --minutes-slice N`; read by engine/outreach/metrics.py for minutes per
-- teardown, hand-made, and for hours per meeting).
--   research  minutes spent researching the company by hand
--   writing   minutes spent writing the teardown and the email
--   slice     minutes spent building the proof slice
-- Each is NULL until typed. A hand company's minutes are the sum of the
-- recorded parts; when none is recorded, metrics falls back to
-- teardowns.review_minutes.
CREATE TABLE hand_minutes (
    company_id  TEXT PRIMARY KEY REFERENCES companies(company_id),
    research    REAL CHECK (research IS NULL OR research >= 0),
    writing     REAL CHECK (writing IS NULL OR writing >= 0),
    slice       REAL CHECK (slice IS NULL OR slice >= 0),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

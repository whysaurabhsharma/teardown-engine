-- 0013_gold_g6: what the gold set (docs/PLAN.md Steps 44-45, engine/judge/gold.py,
-- `te gold`) needs, from protocol/GAPS.md v1.2.
--   gold.gap_type and diagnoses.gap_type  also accept G6, the provisional gap
--                 (GAPS.md v1.2). SQLite cannot alter a CHECK, so both tables
--                 are rebuilt: renamed aside, created again, every row copied
--                 with its id, the old table dropped. Neither table has an
--                 index beyond its UNIQUE constraint or is referenced by a
--                 foreign key; the diagnoses triggers are dropped with the old
--                 table and created again unchanged; the AUTOINCREMENT
--                 high-water marks are carried over.
--   gold.abstain_reason  required when abstain = 1, NULL otherwise. Starts with
--                 one of the three GAPS.md codes, "no_evidence:",
--                 "contradicted:" or "thin:", followed by text. A row that
--                 abstained before this migration had nowhere to record why;
--                 it is copied with the reason
--                 "no_evidence: not recorded (row predates migration 0013)".
--   gold.notes    free text a person typed with the diagnosis (a ruling, a
--                 doubt). Never a person's name or contact.
--   gold evidence  a JSON array of {kind, found, url, quote}: found = true
--                 needs a non-empty quote; found = false (an absence) a null
--                 quote. Checked by triggers on insert and update, not by a
--                 CHECK, so rows written before 0013 copy over unchanged.

ALTER TABLE gold RENAME TO gold_0012;

CREATE TABLE gold (
    gold_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id     TEXT NOT NULL REFERENCES companies(company_id),
    gap_type       TEXT CHECK (gap_type IN ('G1','G2','G3','G4','G5','G6')),
    abstain        INTEGER NOT NULL CHECK (abstain IN (0, 1)),
    abstain_reason TEXT,
    evidence       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence)),
    split          TEXT NOT NULL CHECK (split IN ('teach', 'held_out')),
    gaps_version   TEXT NOT NULL,
    notes          TEXT,
    UNIQUE (company_id, gaps_version),
    CHECK ((abstain = 1) = (gap_type IS NULL)),
    CHECK ((abstain = 0 AND abstain_reason IS NULL)
        OR (abstain = 1 AND abstain_reason IS NOT NULL
            AND (substr(abstain_reason, 1, 12) = 'no_evidence:'
                 OR substr(abstain_reason, 1, 13) = 'contradicted:'
                 OR substr(abstain_reason, 1, 5) = 'thin:')
            AND length(trim(substr(abstain_reason, instr(abstain_reason, ':') + 1))) > 0))
);

INSERT INTO gold (gold_id, company_id, gap_type, abstain, abstain_reason, evidence, split,
                  gaps_version, notes)
SELECT gold_id, company_id, gap_type, abstain,
       CASE WHEN abstain = 1 THEN 'no_evidence: not recorded (row predates migration 0013)' END,
       evidence, split, gaps_version, NULL
FROM gold_0012;

DELETE FROM sqlite_sequence WHERE name = 'gold';
INSERT INTO sqlite_sequence (name, seq)
SELECT 'gold', seq FROM sqlite_sequence WHERE name = 'gold_0012';

DROP TABLE gold_0012;

CREATE TRIGGER gold_evidence_insert
BEFORE INSERT ON gold
WHEN json_type(NEW.evidence) IS NOT 'array' OR EXISTS (
    SELECT 1 FROM json_each(NEW.evidence)
    WHERE CASE
        WHEN type IS NOT 'object' THEN 1
        WHEN json_type(value, '$.kind') IS NOT 'text' THEN 1
        WHEN length(trim(json_extract(value, '$.kind'))) = 0 THEN 1
        WHEN json_type(value, '$.url') IS NOT 'text' THEN 1
        WHEN length(trim(json_extract(value, '$.url'))) = 0 THEN 1
        WHEN json_type(value, '$.found') = 'true' THEN
            CASE
                WHEN json_type(value, '$.quote') IS NOT 'text' THEN 1
                WHEN length(trim(json_extract(value, '$.quote'))) = 0 THEN 1
                ELSE 0 END
        WHEN json_type(value, '$.found') = 'false' THEN
            CASE WHEN coalesce(json_type(value, '$.quote'), 'null') = 'null' THEN 0
                 ELSE 1 END
        ELSE 1 END)
BEGIN
    SELECT RAISE(ABORT, 'gold evidence: an array of {kind, found, url, quote}; found needs a quote, not found has none');
END;

CREATE TRIGGER gold_evidence_update
BEFORE UPDATE OF evidence ON gold
WHEN json_type(NEW.evidence) IS NOT 'array' OR EXISTS (
    SELECT 1 FROM json_each(NEW.evidence)
    WHERE CASE
        WHEN type IS NOT 'object' THEN 1
        WHEN json_type(value, '$.kind') IS NOT 'text' THEN 1
        WHEN length(trim(json_extract(value, '$.kind'))) = 0 THEN 1
        WHEN json_type(value, '$.url') IS NOT 'text' THEN 1
        WHEN length(trim(json_extract(value, '$.url'))) = 0 THEN 1
        WHEN json_type(value, '$.found') = 'true' THEN
            CASE
                WHEN json_type(value, '$.quote') IS NOT 'text' THEN 1
                WHEN length(trim(json_extract(value, '$.quote'))) = 0 THEN 1
                ELSE 0 END
        WHEN json_type(value, '$.found') = 'false' THEN
            CASE WHEN coalesce(json_type(value, '$.quote'), 'null') = 'null' THEN 0
                 ELSE 1 END
        ELSE 1 END)
BEGIN
    SELECT RAISE(ABORT, 'gold evidence: an array of {kind, found, url, quote}; found needs a quote, not found has none');
END;

ALTER TABLE diagnoses RENAME TO diagnoses_0012;

CREATE TABLE diagnoses (
    diagnosis_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id       TEXT NOT NULL REFERENCES companies(company_id),
    prompt_version   TEXT NOT NULL,
    attempted        INTEGER NOT NULL CHECK (attempted IN (0, 1)),
    gap_type         TEXT CHECK (gap_type IN ('G1','G2','G3','G4','G5','G6')),
    confidence       REAL CHECK (confidence BETWEEN 0 AND 1),
    claims           TEXT NOT NULL DEFAULT '[]'
                         CHECK (json_valid(claims) AND json_type(claims) = 'array'),
    abstain_reason   TEXT,
    need             TEXT,
    secondary        TEXT NOT NULL DEFAULT '[]'
                         CHECK (json_valid(secondary) AND json_type(secondary) = 'array'),
    cannot_see       TEXT,
    faithfulness     TEXT CHECK (faithfulness IS NULL OR json_valid(faithfulness)),
    verifier_verdict TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (company_id, prompt_version),
    CHECK ((attempted = 1 AND gap_type IS NOT NULL)
        OR (attempted = 0 AND gap_type IS NULL AND abstain_reason IS NOT NULL)),
    CHECK (attempted = 0 OR (length(trim(coalesce(need, ''))) > 0
                             AND length(trim(coalesce(cannot_see, ''))) > 0))
);

INSERT INTO diagnoses (diagnosis_id, company_id, prompt_version, attempted, gap_type, confidence,
                       claims, abstain_reason, need, secondary, cannot_see, faithfulness,
                       verifier_verdict, created_at)
SELECT diagnosis_id, company_id, prompt_version, attempted, gap_type, confidence,
       claims, abstain_reason, need, secondary, cannot_see, faithfulness,
       verifier_verdict, created_at
FROM diagnoses_0012;

DELETE FROM sqlite_sequence WHERE name = 'diagnoses';
INSERT INTO sqlite_sequence (name, seq)
SELECT 'diagnoses', seq FROM sqlite_sequence WHERE name = 'diagnoses_0012';

DROP TABLE diagnoses_0012;

-- Claims (protocol/GAPS.md v1.1): each is an object with a boolean found and a
-- non-empty url. found = true needs a non-empty text quote; found = false (an
-- absence) must have a null or missing quote. Unchanged from 0001.
CREATE TRIGGER diagnoses_claim_quote_insert
BEFORE INSERT ON diagnoses
WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.claims)
    WHERE CASE
        WHEN type IS NOT 'object' THEN 1
        WHEN json_type(value, '$.url') IS NOT 'text' THEN 1
        WHEN length(trim(json_extract(value, '$.url'))) = 0 THEN 1
        WHEN json_type(value, '$.found') = 'true' THEN
            CASE
                WHEN json_type(value, '$.quote') IS NOT 'text' THEN 1
                WHEN length(trim(json_extract(value, '$.quote'))) = 0 THEN 1
                ELSE 0 END
        WHEN json_type(value, '$.found') = 'false' THEN
            CASE WHEN coalesce(json_type(value, '$.quote'), 'null') = 'null' THEN 0
                 ELSE 1 END
        ELSE 1 END)
BEGIN
    SELECT RAISE(ABORT, 'diagnoses claim: found needs quote and url; not found needs url and no quote');
END;

CREATE TRIGGER diagnoses_claim_quote_update
BEFORE UPDATE OF claims ON diagnoses
WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.claims)
    WHERE CASE
        WHEN type IS NOT 'object' THEN 1
        WHEN json_type(value, '$.url') IS NOT 'text' THEN 1
        WHEN length(trim(json_extract(value, '$.url'))) = 0 THEN 1
        WHEN json_type(value, '$.found') = 'true' THEN
            CASE
                WHEN json_type(value, '$.quote') IS NOT 'text' THEN 1
                WHEN length(trim(json_extract(value, '$.quote'))) = 0 THEN 1
                ELSE 0 END
        WHEN json_type(value, '$.found') = 'false' THEN
            CASE WHEN coalesce(json_type(value, '$.quote'), 'null') = 'null' THEN 0
                 ELSE 1 END
        ELSE 1 END)
BEGIN
    SELECT RAISE(ABORT, 'diagnoses claim: found needs quote and url; not found needs url and no quote');
END;

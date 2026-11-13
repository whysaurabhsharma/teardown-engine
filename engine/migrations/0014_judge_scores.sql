-- 0014_judge_scores: one row per `te judge score` result (docs/PLAN.md Step 50,
-- Gate 4.2; protocol/GAPS.md "Gold set"). Each prompt_version scored against the
-- held-out rows is one round; GAPS.md allows at most three. A re-run whose
-- counts equal the last row for its prompt_version writes nothing.
--   held_out               held-out gold companies scored (the target assumes 13)
--   same_call              the judge's call equals the human's: same gap, or both abstain
--   false_gap              the judge names a gap where the human abstained
--   faithfulness_failures  claims failing the faithfulness check on a named gap
--                          (a claim that would reach a draft)
--   not_judged             held-out companies with no diagnosis (scoring_failed,
--                          refused, error, or not yet extracted)
--   passed                 1 when every target holds
-- No quote, no gap and no company id is stored here: only counts.
CREATE TABLE judge_scores (
    score_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_version        TEXT NOT NULL,
    held_out              INTEGER NOT NULL CHECK (held_out >= 0),
    same_call             INTEGER NOT NULL CHECK (same_call >= 0),
    false_gap             INTEGER NOT NULL CHECK (false_gap >= 0),
    faithfulness_failures INTEGER NOT NULL CHECK (faithfulness_failures >= 0),
    not_judged            INTEGER NOT NULL CHECK (not_judged >= 0),
    passed                INTEGER NOT NULL CHECK (passed IN (0, 1)),
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX judge_scores_version ON judge_scores(prompt_version);

-- 0004_signal_filter: the Step 25 filter's verdicts, written by `te signal filter`
-- (engine/signal/filter.py). Rows are never deleted; later stages skip fresh = 0.
--   fresh          1 when posted_at, or first_seen if posted_at is NULL, is within
--                  the freshness window (60 days, protocol/ICP.md) of the run date;
--                  0 when older; NULL until the filter has run on the row.
--   india_located  1 when the location names an Indian place from the ICP list;
--                  0 otherwise (including no location); NULL until the filter runs.
--                  Not a drop: Step 28 applies the location rule once the head
--                  office is known.
ALTER TABLE signals ADD COLUMN fresh INTEGER CHECK (fresh IS NULL OR fresh IN (0, 1));
ALTER TABLE signals ADD COLUMN india_located INTEGER
    CHECK (india_located IS NULL OR india_located IN (0, 1));

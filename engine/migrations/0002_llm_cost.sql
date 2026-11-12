-- 0002_llm_cost: the estimated cost of each model call, as the provider reports
-- it (claude -p returns total_cost_usd). NULL when the provider reports none.
ALTER TABLE llm_calls ADD COLUMN cost_usd REAL CHECK (cost_usd IS NULL OR cost_usd >= 0);

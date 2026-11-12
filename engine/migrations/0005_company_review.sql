-- 0005_company_review: why a company needs a person to look at it.
--   review_reason  NULL when nothing needs review. Otherwise "<code>: <detail>",
--                  e.g. "name_mismatch: homepage title 'Foo' scored 62 < 85".
--                  Written by `te resolve` (engine/resolve/domain.py, Step 26)
--                  when the domain is not verified both ways; later E2 steps
--                  (segment, Step 28) may use it the same way. A company with a
--                  review_reason stays at status 'sourced' until it is cleared.
ALTER TABLE companies ADD COLUMN review_reason TEXT;

# Teardown Engine

## What this is
An outbound engine that works out, from public evidence, what a company's new
GTM hire will need, and drafts a one-page teardown for a human to review and
send. The product is judgment shown on a page. Reject any change that turns
this into a volume tool.

## Non-negotiables
- Only engine/guard/ touches the network. Every other module calls guard.
- signal, resolve, capture and extract make zero model calls, except the cached
  title classifier. If you add one elsewhere, you have broken it.
- Every fact and every claim carries a verbatim quote from a captured artifact.
  No quote, no row.
- `attempted` is an explicit boolean set by the judge. Never infer it.
- The verifier is never the model family that wrote the claim.
- Nothing from the contacts table ever enters a prompt, a log or a test.
  Drafts carry placeholders; the renderer fills them locally.
- gold rows with split = held_out never appear in a prompt, a skill or a
  fixture. They exist only to score the judge.
- No code path sends email. The engine creates Gmail drafts only.
- Never read, print, log or commit .env, data/ or site/. To inspect a record,
  ask me to run `te debug export <company_id>` and read debug/.
- Never commit real company names or contact data, including in tests.
- Company status only moves forward, or to dropped.
- No scraping of LinkedIn or Naukri. Respect robots.txt. 1 request per second
  per host. A blocked site is an outcome, never something to work around.

## Conventions
- Python 3.12, uv, click CLI `te`, pydantic models, sqlite3, async httpx.
- Windows: run things as `uv run te ...` or via make. Call external programs
  through subprocess with an argument list, never a shell string.
- Every engine command accepts --company <id>, --limit <n>, --pilot, --dry-run.
- Idempotent always: a re-run changes nothing unless its inputs changed.
- Model calls go through llm.gateway.call, keyed on
  (stage, company_id, prompt_version).
- Schema changes are numbered migrations; migrate takes a backup first.
- Changing a prompt, an extractor or a protocol file bumps its version string.
- Every new module ships with a test in the same commit. Fixtures use invented
  companies only.
- One step from docs/PLAN.md per session. Do not start the next step unprompted.

## Commands
make smoke | signal | resolve | capture | extract | people | judge | build |
queue | sync | metrics | backup | demo | test | verify

## Skills
.claude/skills/gap-codebook      the five gaps, abstain rules, teach examples
.claude/skills/capture-conduct   how Guard and capture behave toward sites
.claude/skills/teardown-voice    page spec, email variants, banned phrases

## Where things are
docs/PLAN.md    the build plan (reference, not instructions)
protocol/       OFFER, ICP, GAPS, CONDUCT, TEARDOWN_SPEC, HAND_NOTES, BUDGET
engine/         code, one folder per engine
debug/          redacted exports you may read; gitignored
data/, site/    gitignored; never touched from git, never read by you

## Current step
Phases 1-4 code done; Phase 5 Steps 60-63 done (slices: role_funnel, dns_inventory, funnel_compare, icp_accounts; migration 0016). Live gates open. Next: Steps 64-65 (drafter, quality gates).

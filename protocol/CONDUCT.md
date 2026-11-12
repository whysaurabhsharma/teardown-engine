# CONDUCT.md

Version 1 · 2026-11-12
Not yet corrected from HAND_NOTES.md.

How the engine and its operator behave toward other people's websites, other
people's data and other people's inboxes. `engine/guard/`, `engine/capture/`,
every collector and `engine/outreach/` follow this file. Source: docs/PLAN.md,
"Outcome contract" non-goals, "Design spine" items 7 and 8, "Sourcing",
"People and email", Steps 18, 22, 34, 42, 51, 57 to 59, 69 to 75, 84 and the
CLAUDE.md non-negotiables.

---

## The rule that matters most

A blocked site is an outcome to record, never a problem to defeat. When a site
refuses, record the outcome and move on.

## Non-goals

No Clay clone. No UI beyond the local review page. No automated sending. No
volume above 80. No contact database. No scraping of LinkedIn or Naukri. Each
one sounds like progress and turns a demonstration of judgment into a commodity
tool.

## Part 1. Websites

### Network boundary

- Only `engine/guard/` touches the network. Every other module calls Guard.
- LinkedIn and Naukri are never read by code. Their terms forbid automated
  collection. A person may look at them by hand and type what they find into
  `te signal add`.
- Search (Exa, Tavily) only finds a board or a site. The board's own JSON is
  the source of truth for what is open.

### Identity

Every request carries one honest User-Agent:

```
teardown-engine/0.1 (+<repo url>; research; contact <SENDER_EMAIL>)
```

### robots.txt

- Check robots.txt with `urllib.robotparser` before any page fetch.
- A disallowed URL returns outcome `robots_denied` without fetching.

### Pacing and limits

- At most 1 request per second per host; at most 4 in flight overall.
- 20 second timeout, 2 MB cap per response, follow up to 5 redirects.
- On 429 or 503, honour `Retry-After`; retry at most once.
- Provider limits live in `protocol/BUDGET.md`; `ratelimit.py` loads them.
- `budget.py` sets a per-run ceiling on fetches and model calls. Run
  `--dry-run` before every batch; it prints the projected count and makes no
  request.
- At scale, spread capture over two evenings so no host sees a burst.

### Outcome enum

```
ok | not_found | blocked | dead | robots_denied
```

- 403, 429 and challenge pages are `blocked`, not errors.
- A page kind with no matching link is `not_found`, which is itself a fact.
- Every artifact row gets an outcome, including `not_found`, `blocked` and
  `robots_denied`.

### Cache

- Every response body is stored raw under `data/cache/<sha256>`. Never store
  parsed output as the source.
- A repeat fetch of the same URL on the same day reads the cache and makes no
  request.
- `fresh=True` bypasses the cache only for send-day rechecks, and only for the
  pages a diagnosis cites.
- Changing an extractor re-derives from cache, with no refetch.

### Discovery

Discover pages from the homepage navigation, the footer and `sitemap.xml`. Do
not guess paths.

### Playwright

- Fallback only: when an `ok` page yields under 500 characters of text, retry it
  once with headless chromium.
- At most 2 playwright pages per company.
- Playwright obeys the same robots and pacing rules as `guard.http`.

## Part 2. People's data

- A recipient's name, title and address never enter a prompt. Only public
  company pages are ever sent to a model. Free model tiers may use prompts to
  improve their products, which is one more reason.
- `contacts` is the only table holding personal data. No engine other than
  People and Outreach reads it.
- Nothing from `contacts` ever enters a prompt, a log or a test. The model
  gateway refuses any prompt containing a value from `contacts`.
- Drafts carry placeholders; the renderer fills them locally.
- `te debug export` removes every `contacts` field.
- Never commit real company names or contact data, including in tests.
  Fixtures use invented companies only. `data/`, `site/` and `debug/` never
  enter git.
- Finding an address: Hunter's finder first, Apollo second, then a pattern
  inferred from a public address at the same domain and checked with Hunter's
  verifier. Do not probe mail servers directly.
- Person lookups on LinkedIn are done by hand, human-paced: about fifteen
  recipients a day.
- In anything published, no company names, no people and no quotes from
  replies, unless a company gives written permission.

## Part 3. Inboxes

### A human gate at send

- No code path sends email. The engine calls only Gmail `users.drafts.create`,
  with the `gmail.compose` scope. No send call exists anywhere in the code.
- A person reads, edits and sends each draft. Nothing leaves without approval.
- Never send a draft that has not been read that day.
- For a `candidate` signal, apply through the company's own form first. The
  candidate email variant is refused until `te queue applied` has set the flag.

### Pace and timing

- Send inside the segment's window (`ICP.md`): 9 to 11 am IST Tuesday to
  Thursday for `in_saas`; 9 to 11 am in the recipient's own time zone for
  `us_india_team`. Tuesday to Thursday carry most sends.
- Five to eight reviewed sends a day at full pace (two a day in Phase 0; the
  pilot ten go out five on one morning and five on the next). Do not raise
  volume to make up for a low reply rate.

### What a message may carry

- One link, to the page. No attachment, no tracking pixel, no shortener.
- The sender's postal address and a one-sentence opt-out in every email and on
  every page.

### Touches

- Three touches at most, on day 0, day 4 and day 9, then the company moves to
  `closed`. No fourth message, on any channel.
- Follow-ups are printed by `te queue followups`; the person replies in the
  thread by hand. The engine has no mail-reading permission. A LinkedIn
  connection request with no pitch may stand in for touch 2.
- LinkedIn is the fallback channel for companies with no usable address: a
  connection note or message sent by hand, one sentence and the link. The same
  three-touch limit applies. Log it as a touch with channel `linkedin`.

### Opt-outs

- A "no", a "stop" or an annoyed reply goes into `suppressions` the same day,
  by address and by domain.
- The queue refuses to draft anything for a suppressed domain.
- Delete that company's teardown page the same day.

### Bounces

Two bounces in one day stops sending. Re-verify the remaining addresses before
resuming, and tighten the confidence rule that let them through.

### Hosting

- Teardown pages live at unguessable URLs, `/{company-slug}-{8 random
  characters}/`, served with `X-Robots-Tag: noindex, nofollow`.
- A page is unlisted and is deleted on request.

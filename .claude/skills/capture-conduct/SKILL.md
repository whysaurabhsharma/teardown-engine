---
name: capture-conduct
description: How the engine behaves toward other people's websites - robots.txt, pacing, User-Agent, the fetch outcome enum, caching and the playwright limit. Use when writing or changing engine/guard/, engine/capture/, any collector, or when a fetch returns 403, 429, a challenge page or an empty body.
---

# Capture conduct

Source of truth: `protocol/CONDUCT.md` (written in Step 16). If they disagree,
CONDUCT.md wins.

## The rule that matters most
A blocked site is an outcome to record, never a problem to defeat. Do not add
proxies, rotating or browser-mimicking User-Agents, header spoofing, stealth
plugins, CAPTCHA solving, cookie replay, or retries designed to get past a
block. If a "fix" for a 403 makes the request look like something it is not,
it is wrong. Record `blocked` and move on.

## Network boundary
- Only `engine/guard/` touches the network. Every other module calls
  `guard.http.fetch` (or the guarded playwright helper).
- No scraping of LinkedIn or Naukri, by any route.

## Identity
- User-Agent: `teardown-engine/0.1 (+<repo url>; research; contact <SENDER_EMAIL>)`.
  One honest UA, always the same.

## robots.txt
- Check with `urllib.robotparser` before every page fetch.
- A disallowed URL returns outcome `robots_denied` without fetching.
- Playwright obeys the same robots check.

## Pacing and limits
- At most 1 request per second per host; at most 4 in flight overall.
- 20 second timeout, 2 MB cap per response, up to 5 redirects.
- On 429 or 503, honour `Retry-After`; retry at most once.
- Provider limits live in `protocol/BUDGET.md`; `ratelimit.py` loads them.

## Outcome enum
`ok | not_found | blocked | dead | robots_denied`
- 403, 429 after its one retry, and challenge pages are `blocked`, not errors.
- A missing page kind is `not_found`, which is itself a fact.
- Every artifact row gets an outcome, including failures.

## Cache
- Every response body is stored raw under `data/cache/<sha256>`. Never store
  parsed output as the source.
- A repeat fetch of the same URL on the same day is a cache hit, no request.
- `fresh=True` bypasses the cache only for send-day rechecks of cited pages.
- Changing an extractor re-derives from cache; it never refetches.

## DNS
- Lookups go through `engine/guard/`. Fixed list only: SPF/DMARC TXT, MX,
  fixed DKIM selectors, fixed sending subdomains (at least em., mail., sg.).
- Never enumerate: no zone transfers, no wordlists, no certificate-log sweeps.
- A name that does not resolve is `not_found`, a fact.

## Playwright
- Fallback only: when an `ok` page yields under 500 characters of text.
- Headless chromium, at most 2 playwright pages per company.
- Same robots, pacing and User-Agent rules as `guard.http`.

## Discovery
Discover pages from the homepage navigation, footer and `sitemap.xml`. Do not
guess paths.

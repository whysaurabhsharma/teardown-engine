# Teardown engine

An outbound engine that turns public GTM job posts into researched,
evidence-backed one-page teardowns for a human to review and send.

A company posting an SDR, AE, growth, RevOps or GTM engineer role has said
three things: there is a pipeline problem, budget is approved, and a named
person's quarter depends on it. The engine finds those posts, captures the
company's public pages and DNS records, works out what the new hire will need
on day one, and drafts a short page that says so, with every claim backed by a
verbatim quote and its source. A person reads every page and sends every email.

The product is judgment shown on a page, not volume.

## Try it

```
uv sync
uv run te demo        # or: make demo
```

The demo runs the whole pipeline offline on three invented companies served
from local fixtures, with canned model answers, and prints the path of a
rendered teardown. It needs no keys and opens no network connection.

## How it works

Eight engines, each a folder under `engine/` and a `te` command. Seven run in
sequence; Guard wraps them all.

| Engine | Command | Does |
| --- | --- | --- |
| E1 Signal | `te signal` | Collects GTM job posts from public ATS boards, careers pages and manual entry |
| E2 Resolve | `te resolve`, `te qualify` | One row per real company, verified domain, segment, qualification |
| E3 Capture | `te capture` | Freezes a fixed bundle of public pages and DNS records per company |
| E4 Extract | `te extract` | Derives facts by rule, each with a verbatim quote; no model calls |
| E5 Judge | `te judge` | Names what the hire will need, with quoted evidence, or abstains; a second model family verifies each claim |
| E6 Build | `te slice`, `te draft`, `te quality`, `te teardown` | Proof slice, page and email drafts, automated quality gates |
| E7 Outreach | `te queue`, `te reply`, `te metrics`, `te sync` | Local review queue, Gmail drafts, follow-up text, reply log, metrics |
| E8 Guard | (all) | Rate limits, robots.txt, cache, budgets, backups, integrity checks |

The full plan is in [docs/PLAN.md](docs/PLAN.md). The rules each engine follows
are in [protocol/](protocol/).

## Non-goals

- No Clay clone.
- No UI beyond the local review page.
- No automated sending. The engine creates Gmail drafts only; a person sends.
- No volume above 80 teardowns.
- No contact database.
- No scraping of LinkedIn or Naukri.

Each of these sounds like progress and would turn a demonstration of judgment
into a commodity tool.

## Conduct

The full rules are in [protocol/CONDUCT.md](protocol/CONDUCT.md). In short:

- Every request goes through Guard, with one honest User-Agent that carries a
  contact address.
- robots.txt is checked before every fetch, including each redirect.
- At most one request per second per host, and at most four in flight overall.
- A blocked site is recorded as an outcome. It is never worked around: no
  proxies, rotating User-Agents, header spoofing, stealth browsers or CAPTCHA
  solving.
- DNS lookups use a fixed, short list of names. No enumeration.
- Only public company pages reach a model. A recipient's name, title and
  address never enter a prompt; the renderer fills placeholders locally.
- Every claim on a page carries a verbatim quote and its source, or says
  plainly that something was not found where it was looked for.
- Pages are unlisted, marked noindex, and deleted on request.
- Three touches at most. An opt-out is honoured the same day, by address and
  by domain.

## Data

Campaign data lives in a local SQLite file under `data/`, which is never
committed, along with the page cache, rendered pages (`site/`) and debug
exports (`debug/`). `uv run te verify --public` checks that none of them, and
no company or contact value from the database, appears in the repository or
its history.

## Licence

MIT. See [LICENSE](LICENSE).

# Teardown Engine — Build Plan

2026-11-11 · Saurabh Sharma

Build an outbound engine that finds Indian SaaS companies and US startups with India teams that are hiring for GTM roles, works out from public evidence what that hire will need on day one, and produces a one-page teardown you send to the person who owns the problem. Six teardowns go out by hand in the first three days, before any code. About 21 part-time days of build follow, with no paid data tools and Claude Code as the build surface.

## What changed in this version

Version 2 fixes seventeen weaknesses found by reading version 1 as a sceptic would. Three mattered most: the plan built for twelve days before testing whether anyone replies; the judge was examined on the same examples it was taught with; and the email never said what you want.
| # | Weakness in version 1 | Why it mattered | Fix in this version | Where |
| --- | --- | --- | --- | --- |
| 1 | No test of the premise before building | Twelve days of code before the first reply could arrive | Six teardowns researched, written and sent by hand in days 1 to 3; three a week by hand during the build | Phase 0 |
| 2 | The judge was tested on its own examples | The five gold teardowns were both the worked examples and the exam, so agreement proved nothing | Eighteen gold diagnoses: five teach, thirteen are held out | Stages 4.1, 4.2 |
| 3 | The email hid the ask | A founder could not tell whether you wanted a job, a contract or a chat | The email says it: looking for a GTM engineer role, open to a project. Two offer types by role | The teardown; Step 1 |
| 4 | Indian SaaS had no reliable source | Greenhouse, Lever and Ashby skew to US-funded startups | Seed list first, a careers-page collector, manual signal entry; Indian-HQ companies count wherever the role sits | Stage 2.1 |
| 5 | The schedule was optimistic | Foundation in two days and a pilot in three were not achievable part-time | Foundation four days, pilot five; about 145 hours over ten weeks | Roadmap; Budget |
| 6 | Several gaps were opinions | "Pricing silence" and "undefined role" second-guess deliberate choices and the hiring manager's own post | Five gaps, each framed as what the new hire needs on day one | The teardown |
| 7 | The ICP list could embarrass you | Twenty-five lookalikes from homepage text; a founder knows their ICP better | Ten accounts, each needing fit evidence and a dated reason to act; you confirm every row | Step 60 |
| 8 | Four slice builders, two overlapping | Build time | Three builders; the pricing and form comparisons are one | Stage 5.2 |
| 9 | Competitors were guessed by search | A wrong competitor is a visible error | Prefer competitors the company names itself; you confirm both | Step 61 |
| 10 | People's data could reach a model | Names and addresses in prompts | Recipient fields never enter a prompt; placeholders are filled at render | Design spine; CLAUDE.md |
| 11 | Evidence could go stale before sending | Sites change between capture and send | Cited pages are re-captured and re-checked on send day | Step 70 |
| 12 | Follow-ups needed mail-read permission | Extra scope and fragile threading | Follow-up text is printed; you reply in the thread yourself | Step 71 |
| 13 | No backup and no migrations | One SQLite file holds the whole campaign | Daily backup and a schema version | Step 17 |
| 14 | Claude Code was locked out of what it must debug | The deny rule blocked all of data/ | te debug export writes a redacted record it can read | Step 20 |
| 15 | Missing dependencies and Windows details | Gmail client, YAML and Playwright were absent from the scaffold | Added, with Windows notes | Step 12; Tech stack |
| 16 | "Zero spend" overstated the case | Your Claude subscription is a cost | Reworded: no paid data or enrichment tools | Outcome contract |
| 17 | No stop rule | Sunk-cost risk | Continue, adjust or stop checks after Phase 0 and after the pilot | Step 9; Step 56 |

## Outcome contract

The deliverable is held conversations with founders and GTM leaders about a GTM engineer role or a paid project, produced by a system you built and can demonstrate. Judge every decision in this plan against meetings held.

What ships:
- Six hand-made teardowns sent in week one, which test the idea before the engine exists.
- A working pipeline that turns public GTM job posts into researched, qualified company records, with no paid data or enrichment tool.
- 70 to 80 teardowns in total. Each names what the new hire will need, backs it with quoted evidence, and attaches a working proof slice built for that company.
- A measured campaign in HubSpot: reply rate, meeting rate and hours per meeting, split by segment.
- A public write-up and a public repo. Code and aggregate numbers only. No company names, no contact data.
| Level | Observable test | When you know |
| --- | --- | --- |
| Stop and rethink | No reply to any of the first 15 sends after 7 working days each | Week 4 |
| Baseline | 8 or more replies, 4 or more meetings held, write-up published | Week 10 |
| Good | 2 or more hiring processes, or 1 paid project | Week 10 |
| Excellent | An offer, or inbound from people who read the write-up | Month 3 to 4 |

Non-goals. Put these in the README. No Clay clone. No UI beyond the local review page. No automated sending. No volume above 80. No contact database. No scraping of LinkedIn or Naukri. Each one sounds like progress and turns a demonstration of judgment into a commodity tool.

What it costs. No paid data tools. Your existing Claude subscription, about 145 hours of your time, and incidental spend under ₹2,000. Say exactly that in the write-up; "free" would be untrue.

The constraint that shapes everything: your time per teardown. Phase 0 measures what a teardown costs by hand. The engine exists to bring that down to 20 to 30 minutes of review. If the engine's teardowns take longer than that, fix the engine before sending more.

## Design spine

Nine decisions carry the project. Everything else is implementation. If a shortcut threatens one of these, keep the decision and cut the feature.

1. By hand first. Before any code, research, write and send six teardowns yourself. If the format earns no interest when a careful human makes it, an engine will not rescue it. The six also become the judge's first gold examples and your baseline for hours saved.

2. The hiring post is the trigger. A company posting an SDR, AE, growth, RevOps or GTM engineer role has told you three things: there is a pipeline problem, budget is approved, and a named person's quarter depends on it. One signal gives you the account, the timing and the recipient.

3. Say what you want. Every email states that you are looking for a GTM engineer role and are open to a project. Where the open role is one you would take, you are a candidate and you also apply. Where it is an SDR or AE role, you are offering the system that makes that hire productive.

4. Evidence before judgment. Every claim on a teardown carries a source URL and a verbatim quote from a page you captured. A deterministic check confirms each quote is a substring of the captured text. A claim with no matching quote is deleted before you see the draft.

5. Abstain by default. The judge is allowed, and expected, to return `no_gap`. A company with no specific, evidenced observation is dropped with a logged reason. Plan for 40 to 60 percent of qualified companies to be dropped. A high drop rate is the system working.

6. Help, framed as what the hire needs. A teardown never grades the company. It says what the person being hired will need on day one, shows which part the public pages suggest is missing, and hands over a first piece of it already built.

7. A human gate at send. The engine writes Gmail drafts. You read, edit and send each one. Nothing leaves without your approval, and the review queue records what you changed so the prompts improve.

8. People's data stays out of models. A recipient's name, title and address never enter a prompt. Drafts carry placeholders that the renderer fills locally. Only public company pages are ever sent to a model.

9. One database, one status per company. A single SQLite file is the system of record, backed up daily. Each company sits in exactly one state. Every engine is idempotent and resumable, so a crash, a rate limit or a closed laptop loses nothing.

## Architecture

Eight engines, each a folder, a CLI command and a contract. Seven run in sequence; the eighth, Guard, wraps all of them. Build them in order: each is useless until the one before it works, which stops you building two unproven layers at once.

Read it left to right, then down. The first row is deterministic and free; a model appears only at Judge and Build. You sit between Build and Outreach, and nothing passes without your approval. A company can leave at Resolve, at Judge or at your review, each time with a logged reason.
| Engine | Job | Reads → writes | Main failure mode | Done when |
| --- | --- | --- | --- | --- |
| E1 Signal | Collect GTM job posts three ways: public ATS endpoints, careers pages of seed companies, and manual entry | web → signals | Collecting posts nobody can act on; a thin Indian SaaS list | Every signal has a company, a role family, an offer type, a location and a post date |
| E2 Resolve | Turn posts into unique companies; tag segment; qualify or drop | signals → companies | Duplicates, wrong domain, wrong company with a similar name | One row per real company, each with a verified domain and a segment |
| E3 Capture | Fetch and freeze the evidence bundle for each company | companies → artifacts + cache files | Blocks recorded as errors; partial bundles | Every artifact has an outcome and a text hash |
| E4 Extract | Derive facts by rule from the frozen bundle | artifacts → facts | A model call sneaking into a deterministic path | All checkable facts computed with zero model calls |
| E5 Judge | Name what the hire will need, with quoted evidence, or abstain | facts + artifacts → diagnoses | Generic or invented claims | Every claim's quote passes the substring check; held-out agreement is measured |
| E6 Build | Produce the proof slice, the teardown page and the email draft | diagnoses → teardowns | Templates that read as mail merge | You would send it with under 30 minutes of edits |
| E7 Outreach | Review queue, Gmail drafts, follow-up text, HubSpot sync, reply log, metrics | teardowns → touches | Sending to the wrong person | Every send is approved by you and logged with its outcome |
| E8 Guard | Rate limits, raw cache, call budgets, conduct rules, backups, integrity checks | wraps all | Silent re-fetching, silent overspend, lost data | make verify passes from a clean clone |

### The company state machine

Every company holds exactly one status. An engine picks up companies in the status it consumes and moves them to the next.
```

```

Three rules make this safe. A status only moves forward, or to `dropped`. Every engine command accepts `--company <id>` and `--limit <n>`, so you can re-run one company or a small batch without touching the rest. And the six companies you write to by hand in Phase 0 are entered later with `hand = 1` at status `sent`, so their results sit in the same tables as everything else.

### Where the model is allowed
| Step | Model used? | Why |
| --- | --- | --- |
| Collecting, deduping, fetching, hashing | No | Deterministic work stays deterministic and free |
| Extracting facts (role counts, pricing type, email provider, stack) | No | Rules are auditable; a model here hides errors |
| Classifying a job title into a GTM role family | Small model, cached | Titles are too varied for regex alone |
| Diagnosing what the hire needs | Strongest model | This is the judgment the project exists to show |
| Verifying the diagnosis | A different model family | A second reader that did not write the claim |
| Writing the teardown and the email | Strongest model, then you | Drafts only; you own the final words |
| Anything containing a person's name, title or address | Never | Recipient fields are filled by the renderer on your machine |

## Tech stack and tools

Nothing here costs money at this scale beyond the Claude subscription you already pay for, and most of it you already have. Read every limit on your own dashboard in Phase 1 and write it into `protocol/BUDGET.md`; the figures below are what was published in November 2026.

### Language and core libraries
| Tool | Job in this build |
| --- | --- |
| Python 3.12 + uv | Runtime and dependency management |
| typer | One CLI, te, with a subcommand per engine |
| pydantic v2 | Every record that crosses an engine boundary is a validated model |
| sqlite3 (standard library) | The system of record, one file: data/engine.db |
| httpx (async) | All HTTP, through the Guard wrapper only |
| selectolax | HTML parsing and text extraction |
| tldextract | Registrable domain from any URL, correct on .co.in and .co.uk |
| dnspython | MX and TXT records for email provider and stack signals |
| tenacity | Retries with backoff, honouring Retry-After |
| rapidfuzz | Fuzzy company-name matching for dedupe and domain checks |
| jinja2 | Teardown page and email templates |
| pyyaml | Reads patterns.yaml and the title rules |
| python-dotenv | Loads .env |
| google-api-python-client, google-auth-oauthlib | Gmail drafts, added in Phase 4 |
| playwright | Added in Phase 3 as a fallback for pages that render nothing without JavaScript |
| pytest, ruff | Tests and lint, run by make test |

### Sourcing
| Source | How you read it | Best for |
| --- | --- | --- |
| Greenhouse boards | GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true | US startups with India teams |
| Lever boards | GET https://api.lever.co/v0/postings/{company}?mode=json | US startups with India teams |
| Ashby boards | GET https://api.ashbyhq.com/posting-api/job-board/{name} | US startups with India teams |
| Workable boards | GET https://apply.workable.com/api/v1/widget/accounts/{subdomain} | Both |
| Careers pages of seed companies | Ordinary HTML through Guard; titles read from the page or its embedded JSON | Indian SaaS on Keka, Darwinbox, Zoho Recruit or a custom page |
| Manual entry | te signal add, when you see a role yourself | Indian SaaS; anything the collectors miss |
| Exa and Tavily search | Their APIs, with your existing keys | Discovering boards; finding official sites |
| Hand-kept seed list | data/seeds.csv | The backbone of the Indian SaaS segment |

LinkedIn and Naukri are never read by code. Their terms forbid automated collection. You may look at them yourself and type what you find into `te signal add`.

### Models
| Role | First route | Fallback | Why |
| --- | --- | --- | --- |
| Job-title classification | Groq, small fast model | Cloudflare Workers AI | High volume, trivial task, cached by title |
| Diagnosis (the judge) | Claude Code headless: claude -p --output-format json --json-schema | Gemini | The strongest model you have; the result lands in structured_output |
| Verification | Gemini | Groq, when Gemini is judging | Always a different family from the judge |
| Teardown and email drafts | Claude Code headless | Gemini | Same rules and skills as your interactive sessions |

Run `claude -p` without `--bare`. Bare mode skips your subscription login and needs an API key; the normal mode uses your plan and loads `CLAUDE.md` and your skills. Free model tiers may use prompts to improve their products, which is one more reason only public company pages ever go into a prompt.

### People and email
| Tool | Job | Free allowance as published |
| --- | --- | --- |
| Hunter | Email finder and verifier | 50 credits a month |
| Apollo | Second source for a contact's address | About 900 credits a year on the free plan, per Apollo's own page; third-party figures vary |
| Your own LinkedIn account, by hand | Confirm who holds the role | No automation |
| Gmail API | Creates first-touch drafts in your mailbox; you press send | Free with your Google account |

### Outreach, tracking and hosting
| Tool | Job | Notes |
| --- | --- | --- |
| A shared document | Hosts the six hand-made teardowns in Phase 0 | A Google Doc or Notion page set to "anyone with the link can view" |
| Cloudflare Pages | Hosts engine-made teardowns at unguessable, unindexed URLs | Deployed from a local folder with wrangler; page views show in the project's analytics if you enable them |
| HubSpot free CRM | Companies, contacts, one deal per company, pipeline stages | API is included on the free plan |
| GitHub | Public code repo, CI for make verify | No company or contact data ever committed |
| Claude Code | Build surface for all eight engines, plus the judge at run time | Your existing plan |

### Windows notes

You are building on Windows with PowerShell. Four things to hold to:
- Run everything as `uv run te ...` or through `make`, so the right environment is always used.
- The pipeline calls `claude` through Python's `subprocess` with an argument list and the prompt on standard input. Never build a shell string; quoting a JSON schema in PowerShell breaks in ways that waste an evening.
- `wrangler` needs Node, which you already have from installing Claude Code.
- After adding `playwright`, run `uv run playwright install chromium` once.

### Accounts

You already hold Groq, Google AI Studio, Cloudflare, Tavily, Exa, GitHub and HubSpot. New: Hunter (free), Apollo (free, if you have no seat), and in Phase 4 a Google Cloud project to enable the Gmail API. Serper is not used; your dashboard showed one credit.

## Data contracts and repo layout

Eleven tables in one SQLite file. Freeze these shapes in Phase 1; every engine reads and writes only these, which is what lets you rebuild one engine without touching the others. The schema carries a version number in `PRAGMA user_version`, and every change to it is a numbered migration.
| Table | One row per | Written by | Key columns |
| --- | --- | --- | --- |
| signals | Job post | E1 | signal_id, source, board_slug, title, role_family, offer_type, location, posted_at, url, company_name_raw |
| companies | Real company | E2 | company_id, domain, name, segment, status, drop_reason, drop_stage, best_signal_id, pilot, hand |
| artifacts | Fetched page or record | E3 | company_id, kind, url, outcome, fetched_at, text_sha256, cache_path |
| facts | Extracted fact | E4 | company_id, fact, value, artifact_kind, quote, char_start, extractor_version |
| contacts | Recipient | People step | company_id, name, title, source_url, email, email_confidence, channel |
| gold | Hand-written diagnosis | You | company_id, gap_type or abstain, evidence (JSON), split (teach or held_out), gaps_version |
| diagnoses | Judge result | E5 | company_id, gap_type, attempted, claims (JSON), verifier_verdict, prompt_version |
| teardowns | Rendered teardown | E6 | company_id, slice_kind, page_slug, email_subject, email_body, review_state, review_minutes |
| touches | Message sent or reply logged | E7 | company_id, touch_no, channel, sent_at, outcome, applied, hubspot_deal_id |
| llm_calls | Model call | E8 | stage, provider, model, input_tokens, output_tokens, cache_hit, company_id |
| suppressions | Opt-out | E7 | email_or_domain, reason, added_at |

`contacts` is the only table holding personal data. No engine other than People and Outreach reads it, and nothing in it is ever placed in a prompt.

### The four records that matter most
```

```

```

```

```

```

```

```

Five rules that save the project:
- `attempted` is an explicit boolean the judge must set. Never infer abstention from a null gap.
- A fact or a claim without a `quote` cannot be written. The database rejects it.
- Model calls are keyed on `(stage, company_id, prompt_version)`. A re-run with the same key reads the stored result and makes no call.
- `gold` rows marked `held_out` are never shown to the judge, in a prompt or a skill. They exist only to score it.
- `te backup` copies `engine.db` to `data/backups/` with a date stamp. It runs automatically before every migration and at the end of every `make` target that writes.

### Repo layout
```

```

`data/` and `site/` never enter git. The public repo holds code, protocol documents and synthetic fixtures, so you can open it to anyone without exposing a company or a person.

## The two segments and the two offers

Same engine, two buyers, two arguments. Aim for at least 25 sends in each segment and let the rest follow whichever answers. The segment tag set in E2 chooses the framing and the send time; the offer type set in E1 chooses what you ask for.
|  | Indian SaaS | US startups with India teams |
| --- | --- | --- |
| Who | HQ in India, B2B software, roughly 10 to 300 people, selling to Indian SMBs or to the US from India | HQ outside India, seed to Series B, with a sales or SDR pod in Bengaluru, Hyderabad, Pune, Gurugram, Mumbai or Chennai |
| How you find them | A seed list you keep by hand, read by the careers-page collector, plus roles you enter yourself | Greenhouse, Lever and Ashby posts whose location is an Indian city |
| Segment rule in E2 | Head office in India. The role may sit anywhere: an Indian company hiring a US account executive is a strong signal | Head office outside India and the role is located in India |
| What they usually lack | Tooling budget and targeting. One founder and one or two reps working a spreadsheet | Someone in the India time zone who thinks in systems. The tools are already paid for |
| What lands | You built the enrichment layer they cannot justify buying, with no paid data tool | Research quality and measurement discipline, and proof that you ship |
| Best proof slice | Ten accounts in their ICP, each with a reason to act now | Their funnel beside two competitors, or their open roles against what each will need |
| Recipient | Founder or CEO under 50 people; head of sales or growth above that | The hiring manager named in the post, or the VP Sales, Head of Growth or RevOps lead |
| Send window | 9 to 11 am IST, Tuesday to Thursday | 9 to 11 am in the recipient's own time zone. Many India-pod managers sit in the US |
| The cost line | Lead with it | Mention once. They care more that it works |

### What you are offering, `protocol/OFFER.md`

The open role decides your position. Write this file in Step 1 and never blur the two.
| Offer type | Role families | Who you are to them | What you do |
| --- | --- | --- | --- |
| candidate | GTM engineer, RevOps, growth, a founding or first GTM hire | An applicant who arrives with proof | Apply through their ATS, then send the teardown to the hiring manager. The email says you have applied |
| adjacent | SDR, BDR, account executive, sales lead, demand generation | Someone offering the system that makes that hire productive | The email says you are looking for a GTM engineer role and are open to a project |

`candidate` signals go to the front of every queue. They are the most direct route to the outcome, and a teardown attached to a real application is far harder to ignore than either alone.

### Qualification rules, `protocol/ICP.md`

A company qualifies when all of these hold:
- It sells software or a software-enabled service to other businesses.
- It has an open GTM role posted or updated in the last 60 days.
- It has a working website with at least a homepage and one of: pricing, product, customers.
- You can name a recipient and reach them by email or LinkedIn.

A company is dropped, with the reason logged, when any of these hold:
| Drop reason | Rule |
| --- | --- |
| staffing_or_agency | A recruiter, staffing firm or outsourcing shop posting on a client's behalf |
| too_large | More than roughly 1,000 people; the recipient will never see it |
| consumer | Sells to consumers only |
| no_site_evidence | Fewer than three usable artifacts after capture |
| no_recipient | No named owner of the problem, or no way to reach them |
| conflict | A company you have worked with, a direct competitor of one, or anywhere your current company's interests make contact awkward |
| no_gap | The judge abstained |
| stale_signal | The role closed before you sent |

### One company, one teardown

A company with five open GTM roles is still one record and one send. Keep a `candidate` role as the signal if there is one; otherwise the most senior GTM role. The count of roles becomes a fact the judge can use.

## The teardown

The teardown is the product. Everything upstream exists to make this one page specific, true and useful to its reader. It is written as help for the person being hired, never as a grade for the company.

### The five gaps, `protocol/GAPS.md`

Each gap starts from what the new hire will need on day one. The judge must pick one or abstain. Each needs evidence from at least two different artifacts and maps to one proof slice. Rewrite this file from what Phase 0 teaches you before any code reads it.
| ID | What the hire will need | What the pages suggest is missing | Evidence required | Abstain when | Proof slice |
| --- | --- | --- | --- | --- | --- |
| G1 | A defined target. Who to call first | No visible definition of the ideal customer | The post asks the hire to build lists or define the ICP; the site has no segment, industry or use-case pages; the customers page is mixed or absent | The site names clear segments, or the post names target accounts | icp_accounts |
| G2 | One path from interest to a conversation | Pricing and the main call to action point in different directions on the same site | Pricing shows self-serve prices while the only CTA is a demo form, or pricing says contact sales while the CTA is a free signup | Pricing and CTA agree | funnel_compare |
| G3 | Tooling and process around them | A first GTM hire asked to be rep, operations and tooling at once | The post says founding or first hire and lists list-building, sequencing and CRM setup; no operations role is open; no sales tooling is detected | An operations role is open, or tooling is clearly in place | icp_accounts, shown with the recipe that produced it |
| G4 | Proof that speaks to their market | Hiring to sell into a region the site shows no customers from | The post names a target region; every case study and logo is from somewhere else | Case studies from the target region exist | icp_accounts, limited to that region |
| G5 | Inbound that reaches them | A demo path that asks a lot before anyone talks | The demo form has eight or more fields, or no booking step, or a general contact form is the only route; AE or SDR roles are open | The form is short and has a booking step | funnel_compare |

Two gaps from version 1 are gone as primary findings. Hidden pricing and a broad job description are usually deliberate, and telling a hiring manager their own post is unclear is a poor opening. Either may appear once, as a question, in a follow-up.

A company can show several gaps. The judge returns the one with the strongest evidence and lists the others as `secondary`.

### The page, `protocol/TEARDOWN_SPEC.md`

One page, under 450 words excluding the proof slice. Eight sections, always in this order.
| # | Section | Content | Limit |
| --- | --- | --- | --- |
| 1 | Header | "{Company}: notes for your {role} hire", the date, your name | One line |
| 2 | What I looked at | The pages and records you read, each linked, with the capture date | A short list |
| 3 | What your {role} will need | The need, and what you could not find on the public pages, with two or three quoted lines and their links | 80 words |
| 4 | Why now | The tie to the open role | 40 words |
| 5 | What I would build | Three concrete items, each a system component | 3 bullets |
| 6 | Already built for you | The proof slice. Up to ten rows on the page | One table |
| 7 | What I might have wrong | This is an outside view from public pages; name the specific thing you cannot see | 40 words |
| 8 | About me, and what I am looking for | Three lines on you, two links, and the offer stated plainly | 60 words |

The footer carries your postal address, one sentence on how to opt out, and a line saying the page is unlisted and will be deleted on request.

### Rules the renderer enforces
- Every sentence in section 3 traces to a claim with a passing quote. No quote, no sentence.
- The words describe what the pages show. They never assert what happens inside the company. "Your pricing page routes to a demo form" is fine. "Your reps are wasting time" is a guess.
- Quotes are short, under 25 words each. No logos and no screenshots of their site appear on the page.
- Competitors are named only after you have confirmed them in review.
- No superlatives about yourself. No individual is named except the recipient, and the recipient's name is a placeholder until the renderer fills it.
- A banned-phrase list in the spec fails the build: "I hope this finds you well", "quick question", "game-changing", "synergy", "leverage" as a verb, "circle back", "pick your brain".
- The company name appears in the header and at least twice in the body, so a template cannot slip through unnoticed.

### The email, two variants

Under 120 words. One observation, one link, the offer stated, one small ask. The link goes to the page, with no attachment, no tracking pixel and no shortener.

For a `candidate` signal:
```

```

For an `adjacent` signal:
```

```

### Follow-ups

Three touches at most, then stop for good.
| Touch | When | Content | How |
| --- | --- | --- | --- |
| 1 | Day 0 | The email above | Gmail draft made by the engine; you send |
| 2 | Day 4 | One new observation from a secondary gap, two sentences | te queue followups prints the text; you press Reply in the thread and paste. A LinkedIn connection request with no pitch may stand in for this touch |
| 3 | Day 9 | One line offering the full proof slice as a file, and a clear close | Same as touch 2 |

Anyone who replies "no", or asks you to stop, goes into `suppressions` the same day.

## Roadmap at a glance

Eight phases, 24 stages, 91 steps: three days by hand, about 21 part-time days of build, four weeks of rolling sends, and three days to publish. Six hand-made emails leave by day 3; the first ten engine-made ones leave by day 19.

The two highlighted phases are the tests of the idea: by hand before any code, and on twelve companies before any scale.

Each stage ends at a gate: a check you pass or fail, with no partial credit. A failed gate means you fix that stage before starting the next. Defects in the engine cost an afternoon; defects that reach a founder's inbox cost you the account.

Two ordering rules. Through Phase 4, every engine runs on the pilot set of twelve companies only; width comes in Phase 5. And the campaign does not wait for the build: from week 2 until the engine takes over, send about three hand-made teardowns a week, using whatever parts of the engine already exist to speed up the research.

## Phase 0 · Prove it by hand (days 1 to 3)

Phase 0 sends six teardowns made entirely by you, with a browser and a document, before any code exists. It answers whether the format earns interest, measures what a teardown costs by hand, and produces the first gold examples for the judge.

### Stage 0.1 · Position and mailbox

Step 1. Write `OFFER.md`. One page, kept on your desktop until the repo exists. Your one-line identity. The two offer types from the segments section, in your own words. What you would say yes to: role, location, working pattern. And a list of companies you will not contact, with the reason.

Step 2. Choose the mailbox. Decide once; changing it mid-campaign splits your threads.
| Option | Use it when | Watch for |
| --- | --- | --- |
| A personal Gmail address | The default. Google handles mail authentication, and five to eight one-to-one emails a day is ordinary use | It reads as personal, which suits a job search |
| A mailbox on a domain you own, with normal mail history | You want a branded address | SPF, DKIM and DMARC must all pass |
| Your current company's domain | Only if everyone there is comfortable with a job search running from it | It ties the search to the company |
| A domain registered this week | Never for this project | It has no reputation and will land in spam |

For a domain you own, check the three records, then send one message to another mailbox, open "Show original", and confirm SPF, DKIM and DMARC all read PASS:
```

```

Step 3. Write your sender block. Your name, the one-line identity, a postal address where you can receive mail, and the opt-out sentence. It goes at the foot of every email and every page.

Gate 0.1: `OFFER.md` exists, the mailbox is chosen and tested, and the sender block is written.

### Stage 0.2 · Six teardowns by hand

Step 4. Pick six companies. Three per segment, found by browsing job boards and company sites yourself. At least two should carry a `candidate` role, one you would take. For each, write down the company, the role and its URL, and the person you will write to.

Step 5. Research and write each one with a timer running. Open the homepage, pricing, customers, careers, the page behind the main button, and the job post. Copy every line you rely on, with its URL. Choose one of the five gaps, or decide there is none and swap the company, noting that as an abstention. Write the page to the eight-section spec in a document. Build the proof slice yourself, ten rows. Expect two to three hours each. Record minutes separately for research, writing and the slice.

Step 6. Publish and send. Share each document as view-only by link. For a `candidate` role, apply first. Then send the matching email variant from your mailbox, two a day, inside the send window for the segment.

Step 7. Keep a log. A plain spreadsheet: company, segment, offer type, gap, recipient role, minutes by activity, date sent, reply, notes. In Phase 2 these six rows are entered into the database with `hand = 1`.

Gate 0.2: six teardowns sent, each one timed.

### Stage 0.3 · Read the result

Step 8. Write `HAND_NOTES.md`. Four short lists. Which pages and facts you actually used, which tells you the extractors worth building. Where the minutes went, which tells you what the engine must automate first. Every judgment call you made, which becomes rules in `GAPS.md`. What you could not learn from outside, which feeds section 7 and the abstain conditions. Then revise the five gaps and the page spec from what you learned.

Step 9. Decide, on day 10. Replies take days, so the build continues meanwhile. Phases 1 to 3 are useful under every outcome below.
| What you see | Decision |
| --- | --- |
| At least one reply | Continue as planned |
| No reply, and a fresh reader finds the six specific and fair | Change the recipient rule or the ask before building Phase 4 |
| No real observation at four of the six companies | The five gaps do not fit this market. Rewrite them from HAND_NOTES.md before Phase 4 |
| Each took over four hours and you dread the next | Cut the target to 40 sends now, using the scope ladder |

Gate 0.3: `HAND_NOTES.md` written, the gaps and the spec revised, and the decision recorded with its date.

From here until the engine takes over in Phase 5, keep sending about three hand-made teardowns a week.

## Phase 1 · Foundation (days 4 to 7)

Phase 1 produces an empty engine that can talk to every provider and store state safely. No company is touched by code yet. Sending setup is deliberately absent; it arrives in Phase 4, when there is something to send.

### Stage 1.1 · Environment

Step 10. Create the repo. In PowerShell:
```

```

Step 11. Bring in what you already wrote. Export this document as Markdown and save it as `docs\PLAN.md`. Move `OFFER.md` and `HAND_NOTES.md` from Phase 0 into `protocol\`. Claude Code reads the plan for context; it is reference, never a to-do list.

Step 12. Scaffold with Claude Code. Start `claude` in the VS Code terminal, switch off auto mode with Shift+Tab, and paste:
```

```

Review the status, then ask it to commit as `skeleton: layout, stubs, licence`.

Step 13. Create `.env` yourself. Never through Claude Code, so no key passes through a model:
```

```

Run `git status --short` and confirm `.env` does not appear. `HUBSPOT_TOKEN` and `TEARDOWN_BASE_URL` join it in Phases 4 and 5.

Step 14. Build the smoke test. Prompt:
```

```

Copy the observed limits into `protocol/BUDGET.md` by hand.

Gate 1.1: `make smoke` exits zero and prints a line for all seven providers.

### Stage 1.2 · Rules

Step 15. Write `CLAUDE.md`, the settings file and the three skills. Use the drafts in the Claude Code operating manual section below, verbatim. This is the highest-value hour of the build: it is what stops a helpful session from adding a model call to a deterministic path in week three.

Step 16. Write the protocol documents. `ICP.md`, `GAPS.md`, `CONDUCT.md` and `TEARDOWN_SPEC.md`, taken from the matching sections of this plan and corrected by `HAND_NOTES.md`. Give each a version line at the top. Commit and tag `protocol-v1`.

Gate 1.2: a fresh Claude Code session, asked what it must never do in this repo, lists the non-negotiables without being shown them.

### Stage 1.3 · Core

Step 17. Build the schema, the state machine, migrations and backup. Prompt:
```

```

Step 18. Build Guard before any engine. Prompt:
```

```

Step 19. Build the model gateway. `engine/llm/gateway.py` exposes one function, `call(stage, company_id, prompt_version, route, prompt, schema)`. It checks `llm_calls` for the key first, validates output against the schema, retries once with the validation error appended, falls back to the route's second provider on a quota error, and records tokens. `claude_headless.py` runs `claude` through `subprocess` with an argument list, sends the prompt on standard input, and reads `structured_output`. The gateway refuses any prompt containing a value from the `contacts` table.

Step 20. Add the debug export and define `make verify`. `te debug export <company_id>` writes that company's facts, diagnosis and artifact texts to `debug/`, with every `contacts` field removed. That folder is what you point Claude Code at when a record misbehaves. `make verify` runs the tests, checks the schema version matches the newest migration, confirms `git ls-files` shows nothing under `data`, `site`, `debug` or `.env`, and fails if any company domain from the database appears in a tracked file.

Gate 1.3: `te db init` creates every table; all Guard tests pass; a repeated fetch shows zero network requests the second time; a prompt containing a contact's name is refused; `make verify` passes.

## Phase 2 · Find (days 8 to 10)

Phase 2 turns the public job market into about 40 qualified companies, then picks twelve for the pilot. The two segments are found in two different ways, and the plan treats that as normal.

### Stage 2.1 · Signal collectors (E1)

Step 21. Build the four ATS collectors. Prompt:
```
 list[Signal] and fetches only through
guard.http. Endpoints are in the "Sourcing" table of docs/PLAN.md.

Normalise every job to the Signal model: signal_id, source, board_slug,
company_name_raw, title, location, posted_at, url, description_text.
- posted_at: Greenhouse updated_at, Lever createdAt (epoch milliseconds),
  Ashby publishedAt. Store all as ISO dates.
- Greenhouse returns descriptions only with ?content=true.
- 404 means a wrong or private board. 200 with an empty list means a real
  board with nothing open. Record both; raise on neither.
- Store the raw JSON through the cache.
Tests: one recorded fixture per ATS with invented company names. Assert field
mapping and that a second run inserts zero new rows.]]>
```

Step 22. Discover boards for the US-with-India segment. Query Exa or Tavily with a role term and an Indian city, restricted to the ATS hostnames with the API's domain filter: `job-boards.greenhouse.io`, `boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`, `apply.workable.com`. Eight role terms by ten city terms is 80 queries. Extract the slug from each result URL with a regex per host. Search only finds the board; the board's own JSON is the source of truth for what is open.

Role terms: SDR, BDR, Sales Development Representative, Account Executive, Founding AE, Revenue Operations, GTM Engineer, Growth. City terms: Bengaluru, Bangalore, Hyderabad, Pune, Gurugram, Gurgaon, Mumbai, Chennai, Noida, Remote India.

Step 23. Build the Indian SaaS path. This segment starts from a list you keep, because most of these companies do not use the four ATS products above.
- Seed list. Fill `data/seeds.csv` with 120 to 150 Indian B2B software companies: `company, site, careers_url, source`. Gather them yourself from public lists of Indian SaaS companies, funding news, communities you belong to and your own network. Budget three hours; this list is an asset in its own right.
- Careers-page collector. `careers.py` fetches each `careers_url` through Guard. If the page links to one of the four ATS hosts, it hands the slug to that collector. Otherwise it reads job titles from the page's links, headings and embedded JSON, and passes each through the title classifier. These signals have no post date, so `first_seen` stands in for it.
- Manual entry. `te signal add --company "..." --title "..." --url "..."` records a role you saw yourself. Use it freely; a signal you found by hand is as good as any other.

Step 24. Classify titles and set the offer type. `classify.py` maps a title to a `role_family`: `sdr`, `ae`, `sales_lead`, `revops`, `gtm_engineer`, `growth`, `demand_gen`, `not_gtm`. Regex rules first. Anything the rules cannot place goes to a small Groq model with the enum as the output schema, cached by the exact title string. Then `offer_type` follows from the family and from words such as "founding" or "first": `candidate` or `adjacent`, as defined in `OFFER.md`.

Step 25. Filter by freshness and, for one segment only, by place. Keep a signal posted or first seen within 60 days. Require an Indian location only when the company's head office is abroad; an Indian company's role counts wherever it sits. Since the head office is not known until Stage 2.2, apply the location rule there.

Gate 2.1: at least 120 GTM signals, of which at least 40 come from seed-list companies; each has a company, title, role family, offer type and URL. A second run adds zero duplicates.

### Stage 2.2 · Resolve (E2)

Step 26. Resolve each company's domain. Seed companies already have a site. For the rest, search for the official site, normalise the result with `tldextract`, then verify it two ways. The homepage title or `og:site_name` must fuzzy-match the company name at 85 or above, and the site's careers page must link back to the same ATS board. A company that passes one check but not both is marked `needs_review`.

Step 27. Dedupe. The registrable domain is the key. All signals for one domain attach to one company row. Two different domains with near-identical names are flagged for you, never merged automatically.

Step 28. Tag the segment and apply the location rule. Look for the head office in the footer, the contact page and the legal pages, where entity names such as "Private Limited", "Pvt Ltd", "Inc." and "Delaware" appear. India head office gives `in_saas`. Head office abroad with an India-located role gives `us_india_team`. Head office abroad with no India-located role drops as `out_of_scope`. Anything unclear is `needs_review`.

Gate 2.2: one row per real company, no duplicate domains, every company carries a verified domain and a segment or an explicit `needs_review`.

### Stage 2.3 · Qualify and pick the pilot

Step 29. Apply the ICP rules. `qualify.py` applies the rules in `protocol/ICP.md` in a fixed order and drops on the first failure, writing `drop_reason` and `drop_stage`. Rules that need a human, such as company size and conflicts, are left for the review step.

Step 30. Review by hand, and bring in the Phase 0 six. `te resolve review` shows one company at a time: name, domain, segment, signals and the homepage title. One key accepts, one drops with a reason, one edits the segment. Type the approximate headcount here. Then enter the six hand-made companies with `te company add --hand`, at status `sent`, with their touches and any replies.

Step 31. Pick the pilot twelve. Choose deliberately: six per segment, at least three `candidate` signals among them. Within each six, two where you expect a clear finding, two where you expect none, and two you cannot predict. None of the twelve may be a Phase 0 company.

Gate 2.3: at least 40 qualified companies with 12 or more in each segment; every drop has a reason; the Phase 0 six are in the database; the pilot twelve are marked `pilot = 1`.

## Phase 3 · Understand (days 11 to 14)

Phase 3 gives each company a frozen evidence bundle, a set of facts derived by rule, and a named recipient. Run it on eighteen companies: the pilot twelve and the Phase 0 six. The six need bundles because their hand-written diagnoses become gold examples.

### Stage 3.1 · Capture (E3)

Step 32. Fix the bundle. Every company gets the same artifact kinds, so facts are comparable across companies. Check the list against `HAND_NOTES.md` and add any page you relied on in Phase 0 that is missing here.
| Kind | What | Used for |
| --- | --- | --- |
| homepage | The root page | Positioning, primary CTA, stack signals |
| pricing | Pricing page if one exists | Pricing model |
| product | Main product or platform page | What they sell, to whom |
| customers | Customers or case studies | Customer geography and segment |
| about | About or company page | Head office, team size language |
| careers | Careers page | Hiring shape |
| contact | Contact page | Address, routing |
| demo_form | The page behind the primary CTA | Form fields, booking step |
| compare | Any "vs" or "alternatives" page on their own site | Competitors the company names itself |
| job_post | Full text of the signal role | What the hire is asked to do |
| ats_board | All open roles | Role counts by family |
| dns | MX and TXT records | Email provider, tool verification records |

Step 33. Discover pages; do not guess paths. Read the homepage navigation, the footer and `sitemap.xml`. Map links to kinds by URL and anchor text: "pricing" or "plans" is `pricing`; "customers", "case studies" or "stories" is `customers`; "vs", "compare" or "alternatives" is `compare`. The hero button's target is `demo_form`. A kind with no matching link is recorded as `not_found`, which is itself a fact.

Step 34. Fetch through Guard. First add the fallback browser: `uv add playwright`, then `uv run playwright install chromium`. Prompt:
```

```

Step 35. Extract text and structure. `extract_text.py` uses `selectolax` to drop scripts, styles, navigation and footers, then keeps three things: visible text, every button and link with its text and target, and every form with its field names and count. Version the extractor as `ex-1.0` and store a SHA-256 of the text. Changing the extractor bumps the version and re-derives from cache, with no refetch.

Step 36. Build the viewer. `te show <company_id>` prints the bundle summary, each artifact's outcome, and the first lines of each text. You will use this command in every interview, and from now on it is how you research your weekly hand-made teardowns.

Gate 3.1: all eighteen companies have a bundle; every artifact kind has an outcome; the blocked rate is printed; re-extracting from cache reproduces identical hashes.

### Stage 3.2 · Extract (E4)

Step 37. Build the fact extractors. Each is a pure function from the bundle to a fact with its quote. No model calls anywhere in this stage. Build first the ones `HAND_NOTES.md` says you actually used.
| Fact | Values | Rule |
| --- | --- | --- |
| pricing_model | numbers, contact_sales, free_or_trial, none | Currency patterns on the pricing page; phrases such as "contact sales", "book a demo", "start free"; none when the page is not_found |
| primary_cta | demo, signup, contact, other | Text and target of the homepage hero button |
| demo_form_fields | Integer and field names | Count of visible inputs on the demo_form page |
| has_booking_step | Boolean | A scheduling embed or link on or after the form |
| open_roles_total | Integer | Count of jobs on the board or careers page |
| gtm_roles_by_family | Map of family to count | Title classifier over all open roles |
| first_gtm_hire | Boolean | Post contains "founding" or "first" and no other sales titles are open |
| jd_tools | List | Tool names in the job post, from a fixed lexicon |
| jd_target_market | List of regions | Region and country names in the job post |
| jd_scope_flags | List | Phrases for list building, sequencing, CRM setup, closing, quota, territory |
| customer_regions | List | Country and region names on the customers page |
| segment_pages | Integer | Count of industry, use-case or "for teams" pages in the navigation |
| named_competitors | List | Company names on the compare pages |
| email_provider | google, microsoft, other | MX records |
| stack_signals | List | Script hosts and TXT verification records matched against patterns.yaml |

Step 38. Write `patterns.yaml`. One entry per tool: the script hostnames, cookie names or TXT prefixes that identify it. Cover CRM, marketing automation, chat, scheduling, analytics and sales engagement. Keep it in one file so a wrong pattern is a one-line fix.

Step 39. Test, then spot-check by hand. Fixtures for every extractor. Then open six facts for each pilot company beside the live site and mark each right or wrong. Every wrong one becomes a new test case before you fix the rule. For the Phase 0 six, compare the facts with what you found by hand; any fact you used then that the extractors miss is the next extractor to write.

Gate 3.2: facts exist for all eighteen; the `llm_calls` ledger shows zero calls for this stage; your hand check of 72 pilot facts is at least 90 percent correct.

### Stage 3.3 · Recipient

Step 40. Apply the recipient rule. Under 50 people: the founder or CEO. From 50 to 200: head of sales, VP Sales, CRO or head of growth. Above 200: the hiring manager named in the post, or a director-level sales or RevOps lead. A recruiter is never the recipient.

Step 41. Find the person. In order: the company's team or about page, a "reports to" line in the job post, press and funding announcements, then your own LinkedIn search by hand. Record the name, the title and the URL where you found them.

Step 42. Find the address. Hunter's email finder first, Apollo second. If neither returns one, infer the pattern from any public address at the same domain and check it with Hunter's verifier. Store a confidence of `high`, `medium` or `low`. Do not probe mail servers directly.

Step 43. Set the channel. `email` at high or medium confidence. Otherwise `linkedin`, sent by you by hand with the same link. With neither, drop as `no_recipient`. Contacts live only in the local database and never in a prompt.

Gate 3.3: each of the pilot twelve has a named recipient with a source URL, and either an address at medium confidence or better, or a LinkedIn channel.

## Phase 4 · Judge and pilot (days 15 to 19)

Phase 4 builds the judge, scores it on diagnoses it has never seen, and sends the first ten engine-made emails. It turns three unknowns into measured numbers: how often the judge agrees with you, your minutes per teardown, and the drop rate.

### Stage 4.1 · The gold set

Step 44. Diagnose the pilot twelve by hand. For each, using only `te show` and the facts table, write down one gap or an abstention, and the two or three quotes that support it. This is a diagnosis, not a full teardown: about 20 minutes each. Together with the Phase 0 six, you now hold eighteen gold diagnoses.

Step 45. Split them, and fix the rules. Mark five as `teach`: both segments, at least three gap types, and one abstention. Mark the other thirteen `held_out`. Enter all eighteen with `te gold add`. Fold every ruling you had to invent into `GAPS.md` and bump it to v2. From this point the held-out thirteen are never pasted into a prompt, a skill or a chat.

Gate 4.1: eighteen gold rows in the database, the split recorded, `GAPS.md` at v2.

### Stage 4.2 · Judge v1 (E5)

Step 46. Write the judge prompt. `engine/judge/prompt.md`, versioned as `j-1.0`. Six parts, in this order:
- The task: say what the hire will need and which of the five gaps the pages show, or abstain.
- The five gaps, pasted from `GAPS.md`, each with its abstain condition.
- The evidence rules: every claim quotes the supplied text verbatim; claims must come from at least two artifact kinds; describe what pages show, never what happens inside the company.
- Anchored confidence: 0.9 means two or more direct quotes and no counter-evidence; 0.6 means the evidence fits but one artifact is missing; 0.3 means a plausible reading you would not put your name to. Below 0.6, abstain.
- The five `teach` diagnoses as worked examples, including the abstention.
- The company: name, segment, signal role, offer type, the facts with their quotes, the full job post, and up to 600 words from each other artifact. No person's name or address.

Step 47. Build the judge. Prompt:
```

```

Step 48. Build the faithfulness check. `faithfulness.py` normalises whitespace, case and smart quotes, then tests each claim's quote as a substring of that artifact's stored text. A miss fails the claim. A near miss, at 95 or above on `rapidfuzz` partial ratio, is logged in its own category so you can tell a mangled apostrophe from an invented sentence. A diagnosis with fewer than two passing claims from two artifact kinds becomes an abstention.

Step 49. Build the verifier. `verify.py` sends each surviving claim to the verifier route with the quote, 300 characters of surrounding text, and the gap's definition. It answers `supported`, `not_supported` or `unsure`. One `not_supported` flags the diagnosis for your review; two turn it into an abstention. The verifier is never the model family that wrote the claim.

Step 50. Score the judge on the held-out thirteen. `te judge score` runs the judge on all eighteen and reports, for the held-out rows only:
| Measure | Target |
| --- | --- |
| Same call as you: the same gap, or both abstain | 10 of 13 or better |
| Judge names a gap where you abstained | 1 at most |
| Claims failing the faithfulness check | Zero reach a draft |

Revise the prompt and bump its version when it falls short, but allow yourself three rounds at most. Every round you tune against the held-out set makes it a little less held out. Thirteen is a small exam; report the score as what it is.

Gate 4.2: the judge meets both agreement targets on the held-out rows within three rounds, and every surviving claim passes the faithfulness check.

### Stage 4.3 · Send ten

Step 51. Set up sending. Two pieces, both new in this phase.
- Gmail drafts. `uv add google-api-python-client google-auth-oauthlib`. In Google Cloud Console: new project, enable the Gmail API, configure the OAuth consent screen, create an OAuth client of type Desktop app, and save the JSON as `data/gmail_client.json`. On Google Workspace, set the app to Internal so the token does not lapse mid-campaign. Request the `gmail.compose` scope. The engine calls only `users.drafts.create`; no send call exists anywhere in the code.
- Teardown hosting. Create a Cloudflare Pages project and deploy from the local `site/` folder. Add a `_headers` file so nothing is indexed, and set `TEARDOWN_BASE_URL` in `.env`. Each page lives at `/{company-slug}-{8 random characters}/`.
```

```

```

```

Step 52. Build renderer v1. `render.py` fills a `jinja2` template with the eight sections and writes `site/{slug}/index.html`. Plain, readable HTML with one stylesheet; a page that looks like a document. The recipient's first name and the company's contact details are placeholders until this step fills them locally. Open each deployed URL on a phone.

Step 53. Write the pilot teardowns. For the ten pilot companies the judge did not drop, write sections 3, 4, 5 and 7 yourself from the diagnosis, and build each proof slice by hand. Note which parts were mechanical; those become the slice builders and the drafter in Phase 5.

Step 54. Create the drafts. `te queue draft --pilot` fills the right email variant and calls `users.drafts.create`. Every draft lands in your Gmail drafts folder, unsent.

Step 55. Review and send ten. Run the `red-team` reader over all ten first. Then read each page and email against the live site one last time, apply for any `candidate` role, and send five on one morning and five on the next. Record your minutes with `te queue done <company_id> --minutes <n>`.

Step 56. Re-estimate, and decide. Write three measured numbers into `protocol/BUDGET.md`: minutes per teardown, the share of qualified companies the judge dropped, and how many companies an evening of building yields. Then look at every send so far, hand-made and engine-made, about twenty in all.
| What you see | Decision |
| --- | --- |
| Two or more replies from about twenty sends | Continue to Phase 5 as planned |
| One reply or none, all sends at least seven working days old | Stop building. Rework the recipient rule, the ask and the gaps with a fresh reader, send ten more by hand, and return only when something lands |
| Replies come only from candidate sends | Source toward candidate roles; treat adjacent as secondary |
| Review takes over 40 minutes each | Apply the scope ladder before Phase 5 |

Gate 4.3: ten engine-made emails sent; your final read found zero unsupported claims; the three numbers and the decision are written down.

## Phase 5 · Scale (days 20 to 24)

Phase 5 takes the engines from eighteen companies to the full cohort and automates what the pilot did by hand. Target about 150 qualified companies in, about 60 engine-made teardowns out, which with the hand-made and pilot sends brings the campaign to 75 or 80.

### Stage 5.1 · Widen find and understand

Step 57. Run E1 to E4 on everything qualified. `make signal resolve capture extract`, with `--dry-run` first to see the projected fetch count. Spread capture over two evenings so no host sees a burst. Put `candidate` signals first in every run.

Step 58. Fix what breaks at width. It will be the things eighteen companies could not show you: an ATS variant, a careers page the collector misreads, two companies sharing a name, a site that blocks you. Each fix ships with a test. A blocked site is an outcome to report, never a problem to defeat.

Step 59. Resolve recipients in batches. Fifteen a day keeps you inside the free email-finder credits and keeps the LinkedIn checks human-paced.

Gate 5.1: every qualified company is at `extracted` or `dropped`; the blocked rate and the drop counts by reason are printed.

### Stage 5.2 · Proof-slice builders

Three builders cover all five gaps. Each carries its own evidence, each has a hard budget, and each row is yours to reject in review.
| Builder | Serves | What it produces | Budget per company |
| --- | --- | --- | --- |
| icp_accounts | G1, G3, G4 | Up to ten companies that fit their ICP, each with a fit quote and a dated reason to act now | 3 searches, 45 fetches, 1 model call |
| funnel_compare | G2, G5 | The company beside two competitors: pricing model, lowest listed price, primary CTA, form fields, booking step | 1 search at most, 8 fetches |
| role_funnel | Fallback for any gap | Each open GTM role beside what that role will need and what the site shows today | No new requests |

Step 60. Build `icp_accounts`. Derive an ICP descriptor from the customers page, the product page and the job post: industry, size band and region, each backed by a quote. Find up to 30 candidates with Exa's similar-page search on two or three of their named customers, plus one keyword search. A candidate is listed only when it clears two tests:
- Fit. A quote from the candidate's own homepage that matches the descriptor.
- Reason to act now. A dated public fact from the candidate's own site: an open role in the function the company sells to, a launch, an expansion or a funding note.

No reason, no row. Exclude existing customers and named competitors. For G4, restrict candidates to the region named in the job post. Fewer than six surviving rows fails the slice. A founder knows their ICP better than you do; ten well-argued accounts will be read, and twenty-five weak ones will count against you.

Step 61. Build `funnel_compare`. Take competitors from the `named_competitors` fact first, since those are the ones the company itself compares against. Only if there are none, run one "alternatives" search. Either way, `te slice competitors <company_id>` shows you the two it chose and waits for your yes before anything is captured. Then capture each competitor's pricing page and demo path and run the same extractors you already wrote. The same extractor on all three is what makes the table fair. Report what the pages show; make no claim about conversion.

Step 62. Build `role_funnel`. Pure assembly from existing facts: the GTM roles by family, the scope flags from each post, and the presence or absence of segment pages, case studies, pricing and a booking step.

Step 63. Hold every slice to the evidence rule. Each row in a slice carries a source URL and a quote. A row without both is dropped before rendering. A failed slice falls back to `role_funnel`; if that is thin too, the company is dropped.

Gate 5.2: each builder produces a correct slice for three pilot companies, checked by you against the live sites, inside its budget.

### Stage 5.3 · Drafter, quality gates, review queue and CRM

Step 64. Build the drafter. One call per company on the drafting route writes sections 3, 4, 5 and 7 of the page and the email body, from the diagnosis, the facts and the slice. The prompt includes the spec, the banned phrases, the offer type, and three pilot emails you actually sent, with names removed, as the voice to match. Output is a schema with one field per section, so the renderer never parses prose. The recipient's name stays a placeholder.

Step 65. Build the automated quality gates. `quality.py` fails a teardown before you see it when any of these hold:
- a sentence in section 3 has no passing claim behind it
- fewer than two artifact kinds are cited
- a word limit is exceeded or a banned phrase appears
- the company name is missing from the body
- the verifier returned `not_supported` on any claim
- the offer stated in section 8 or the email does not match the signal's offer type
- a competitor is named that you did not confirm

Step 66. Build the review queue. `te queue open` serves one local HTML page per teardown: the rendered page on the left; the evidence, facts and live links on the right. Three keys: approve, edit, kill with a reason. Slice rows have their own reject switch. It records your minutes and how much text you changed. High edit distance on one section tells you which prompt to fix.

Step 67. Connect HubSpot. Create a private app or Service Key with read and write on contacts, companies and deals, and add `HUBSPOT_TOKEN` to `.env`. In the HubSpot UI, create a deal pipeline named "Teardown outreach" with stages Queued, Sent, Replied, Meeting held, In process, Won, Closed. Add deal properties by hand: `te_company_id`, `segment`, `offer_type`, `gap_type`, `slice_kind`, `teardown_url`, `made_by` (hand or engine). Build `te sync` as an idempotent upsert of company, contact and one deal, and run it once to load every send so far.

Step 68. Run a trial batch of twenty. Review twenty end to end without sending. Read the kill reasons and the most-edited sections, fix the prompts, bump their versions, and re-run only the affected stage. Run `red-team` over the survivors.

Gate 5.3: in a batch of twenty, at least 80 percent pass your review with light edits; every kill has a reason; average review time is 25 minutes or less; HubSpot shows every send to date.

## Phase 6 · Send and measure (weeks 6 to 9, rolling)

Phase 6 is the campaign at full pace: five to eight reviewed sends a day, every touch logged, every reply handled the same day. It runs alongside your normal week at about two hours a day.

### Stage 6.1 · Outreach operations (E7)

Step 69. Hold a daily rhythm. Evening: review tomorrow's batch in the queue and approve five to eight. Morning: open the drafts, read each once more, and send inside the segment's window. Tuesday to Thursday carry most sends. Never send a draft you have not read that day.

Step 70. Recheck the role and the evidence on send day. `te queue draft` re-polls the job source. If the signal role has closed, the company drops as `stale_signal`, unless another GTM role is open, in which case the teardown is re-drafted against that role. It also re-fetches, fresh, only the pages the diagnosis cites and re-runs the faithfulness check. A quote that is no longer on the page pulls the teardown back for review.

Step 71. Run the follow-ups by hand. `te queue followups` lists the threads due for touch 2 on day 4 and touch 3 on day 9, and prints the text for each. You press Reply in that thread in Gmail and paste it. This keeps threading correct with no mail-reading permission. Three touches, then the company moves to `closed`. No fourth message, on any channel.

Step 72. Honour every opt-out the same day. A "no", a "stop" or an annoyed reply goes into `suppressions` by address and by domain. The queue refuses to draft anything for a suppressed domain. Delete that company's teardown page as well.

Step 73. Use LinkedIn as the fallback channel. For companies with no usable address, send a connection note or message by hand: one sentence and the link. The same three-touch limit applies. Log it as a touch with channel `linkedin`.

Step 74. Watch for bounces. Two bounces in one day stops sending. Re-verify the remaining addresses before you resume, and tighten the confidence rule that let them through.

Step 75. Apply before you write, for candidate roles. For every `candidate` signal, submit the application through the company's own form first, then send. Record it with `te queue applied <company_id>`; the candidate email variant is refused until that flag is set.

Gate 6.1: the first twenty scaled sends are out with at most one bounce, and every one has a `touches` row.

### Stage 6.2 · CRM and metrics

Step 76. Sync HubSpot after every session. `te sync` upserts the company, the contact and one deal, and moves the deal stage from the latest touch outcome. HubSpot is the view you show people; SQLite stays the system of record.

Step 77. Log replies by hand. `te reply <company_id> --type positive|question|referral|not_now|negative|optout --note "..."`, and `te meeting <company_id> --held` after a call actually happens. At 80 companies, reading your own inbox is faster and more reliable than automating it.

Step 78. Build the metrics command. `te metrics` prints the campaign from the database alone:
| Metric | Definition |
| --- | --- |
| Delivered | Sent minus bounced |
| Reply rate | Companies with any reply, over delivered |
| Positive reply rate | Replies of type positive, question or referral, over delivered |
| Meeting rate | Calls actually held, over delivered |
| Drop rate | Companies dropped, over companies qualified, by reason |
| Minutes per teardown | Mean and median, hand-made and engine-made shown apart |
| Hours per meeting | Total research and review hours, over meetings held |
| Tool spend per meeting | Total paid spend, over meetings held |

Every rate is split by segment, offer type, gap type, slice kind, recipient role, channel, and by whether the teardown was hand-made or engine-made. That last split is the nearest thing this campaign has to a comparison group; there is no true control, and the write-up says so.

Step 79. Review weekly. Once a week, look at which offer types, gaps and slices earn replies and shift the mix toward them. Fix quality or targeting. Do not raise volume to make up for a low reply rate; that is the failure this whole design avoids.

Gate 6.2: `te metrics` reproduces every figure from `data/engine.db` with no network access, and the HubSpot pipeline matches it.

### Stage 6.3 · Conversations

Step 80. Generate a prep sheet for every meeting. `te prep <company_id>` writes one page: the diagnosis and its evidence, the secondary gaps you did not use, the full proof slice, and the open questions from section 7 of their teardown.

Step 81. Rehearse the five-minute demo. In a call, the strongest thing you can do is show the engine on their own company: `te show` for the evidence bundle, the facts with their quotes, the judge's diagnosis and the claims it rejected, then the review queue. Practise it until it runs in five minutes.

Step 82. Close each conversation in the record. After every call, log the outcome and the next step. When a founder asks for the system rather than a hire, answer with a one-page scope: the outcome, the three components, a fixed price, a two-week timeline.

Gate 6.3: 60 or more companies sent in total; every touch and reply logged; ten working days have passed since the last first touch, so the numbers are final.

## Phase 7 · Publish (three days, after Gate 6.3)

Phase 7 turns a private campaign into a public asset: one write-up with real numbers, a repo a stranger can run, and a short kit for interviews. This is the part that keeps working after the sends stop.

### Stage 7.1 · Analysis

Step 83. Compute the final numbers with intervals. At this sample size every rate is soft, so say so. A 20 percent reply rate on 70 delivered carries a 95 percent interval of roughly 12 to 31 percent. Use a Wilson interval and print it beside every rate. Report the judge's held-out score as a count out of thirteen.

Step 84. Anonymise. No company names, no people, no quotes from replies, unless a company gives written permission. Report by segment, offer type, gap type, slice kind and recipient role.

Step 85. Draw three figures from the database. A funnel from qualified to diagnosed to sent to replied to meeting held, with the drop reasons at each step. Reply rate by segment and by offer type. Minutes per teardown, hand-made beside engine-made. Each figure is produced by a script in `report/`, never by hand.

Gate 7.1: every number in the draft regenerates from one command.

### Stage 7.2 · Write-up and repo

Step 86. Write it number-first. In this order:
- The result in one sentence, with its interval.
- What the engine is, in two sentences.
- The funnel figure, including how many companies were dropped and why.
- Hand against engine: hours per teardown before and after.
- The cost, stated exactly: no paid data tools, a Claude subscription, your hours per meeting.
- One teardown, with the company's permission or rebuilt on an invented company.
- What failed. Blocked sites, wrong diagnoses you caught in review, the gap types that earned nothing, the judge's held-out misses.
- What this does not show: no control group, a small sample, one sender.
- The architecture, briefly, with a link to the repo.

Step 87. Make the repo safe to open. `make verify` must pass. Confirm `data/`, `site/` and `debug/` were never committed: `git log --all --stat -- data site debug` must return nothing. Replace any real company in tests with invented ones. Add a README stating what this is, the non-goals, and the conduct rules.

Step 88. Add `make demo` and CI. `make demo` runs the whole pipeline on three invented companies served from local fixtures and renders a teardown, with no network and no keys. A GitHub Action runs `make test`, `make verify` and `make demo` on a clean runner. That is the proof it works on a machine other than yours.

Step 89. Record three minutes of screen. The demo path from Step 81, run on an invented company. Captions are enough.

Gate 7.2: a stranger can clone the repo, run `make demo`, and see a rendered teardown without asking you anything.

### Stage 7.3 · Distribution

Step 90. Publish in order. Repo public first, then the write-up, then a LinkedIn post carrying the funnel figure and the one-sentence result, then the GTM communities where practitioners read. Post nothing until every link resolves.

Step 91. Close the loop and pack the interview kit. Send the write-up to everyone who replied during the campaign, including those who said not now. Pin the repo on your GitHub profile. Then assemble four things you can share in any hiring conversation: the write-up, the three-minute recording, a screenshot of the HubSpot pipeline with names hidden, and the one-line result with its interval.

Gate 7.3: write-up live, repo public, CI green, the post sent to every person who replied, and the interview kit in one folder.

## Claude Code operating manual

Claude Code builds all eight engines and also runs as the judge. Set it up once in Phase 0 and every later session starts with the right constraints already loaded.

### How to run a session
- One step of this plan per session. Start by naming the step; end by updating the "Current step" line in `CLAUDE.md`.
- Use the terminal session in VS Code, opened at the repo root. One session at a time.
- Keep auto mode off. Cycle with Shift+Tab until it is. For anything under `engine/outreach/`, approve each action yourself.
- Ask for the plan before the code. Review `git status --short` before every commit.
- Start a fresh session when you change engines, so one engine's conventions do not leak into another.

### The session prompt

Use this for every build step. Only the numbers change.
```

```

### `CLAUDE.md`: write this in Step 15

Standing instructions loaded into every session. Keep it under 150 lines; a long one gets skimmed.
```

```

### Keep secrets out of reach

Add a deny rule in `.claude/settings.json` so no session can open your keys or your data, even by accident:
```

```

Check the exact rule syntax against the current Claude Code settings reference when you add it. Engine code still reads these paths at run time; the rule only stops the assistant from reading them into a conversation. When a record misbehaves, run `te debug export <company_id>` yourself and point the session at `debug/`, which holds the same record with every contact field removed.

### Skills to create

Each is a folder under `.claude/skills/` with a `SKILL.md`. They keep behaviour identical from one session to the next.
| Skill | Holds | Prevents |
| --- | --- | --- |
| gap-codebook | The taxonomy from GAPS.md, the abstain conditions, the five teach examples, never the held-out thirteen | The judge prompt drifting between sessions without you noticing |
| capture-conduct | robots.txt, pacing, User-Agent, the outcome enum, the playwright limit | A helpful session "fixing" a 403 by adding evasion |
| teardown-voice | The page spec, word limits, banned phrases, three emails you actually sent | Drafts sliding back toward generic sales copy |

### Subagent split

Define these under `.claude/agents/`, or run them as separate sessions. Each has a boundary it does not cross.
| Agent | Owns | Never touches |
| --- | --- | --- |
| evidence | E1 to E4: collectors, capture, extractors | judge/, build/, outreach/ |
| judgment | E5 and E6: prompts, schemas, slices, quality gates | guard/ |
| outreach | E7: drafts, HubSpot sync, metrics | Anything that fetches company pages |
| red-team | Reads ten finished teardowns as a sceptical founder and lists every sentence that is generic, presumptuous or unsupported | Writes no code |

Run `red-team` before the pilot sends in Step 55 and again on the trial batch in Step 68. It is cheaper to be embarrassed by your own reviewer than by a recipient.

### The judge at run time

The pipeline calls Claude Code in non-interactive mode from Python, with an argument list and the prompt on standard input:
```

```

The structured result is in the `structured_output` field of the JSON response; check the field exists before using it. Run without `--bare` so the call uses your plan's login. Each call's JSON also reports its estimated cost, which the gateway writes into `llm_calls`.

### What to say about this in public

State plainly in the write-up that Claude Code built the engine and drafts the diagnosis, that a second model family verifies each claim, and that you reviewed and sent every message yourself. That is the accurate description, and it is also the strongest one.

## Budget and throughput

The whole campaign fits inside free allowances with room to spare, because volume is capped at 80 by design. The scarce resource is your time: about 145 hours over ten weeks.

### Where the 75 to 80 sends come from
| Source | Sends | When |
| --- | --- | --- |
| Phase 0, by hand | 6 | Days 1 to 3 |
| Weekly hand-made teardowns during the build | About 9 | Weeks 2 to 4 |
| Pilot, engine-made | 10 | Day 19 |
| Engine at scale | 50 to 55 | Weeks 6 to 9 |

### The funnel behind the scaled sends

These ratios are planning assumptions until the pilot replaces them with measured ones.
| Stage | Count | What removes companies |
| --- | --- | --- |
| GTM signals collected | 350 or more |  |
| Unique companies | About 190 | Several roles per company |
| Qualified | About 130 | ICP rules, your review |
| Diagnosed with a gap | About 70 | The judge abstains on roughly 45 percent |
| Approved by you | 50 to 55 | Quality gates, stale roles, weak slices, your kills |

If the market yields fewer than 130 qualified companies, that is a finding about the size of this niche, and the campaign ends at a smaller number. Do not loosen the ICP rules to hit a target.

### Calls and requests across the campaign
| Resource | Used for | Planned volume | Allowance to check on your account |
| --- | --- | --- | --- |
| Exa | Board discovery; similar-company search for icp_accounts | About 80 + 150 searches | Your credit balance |
| Tavily | Official-site lookup; competitor search when none is named | About 100 + 40 searches | 1,000 credits a month |
| Groq | Title classification, cached | About 400 distinct titles, once | Daily request limit |
| Claude Code headless | Judge, drafter, ICP descriptor | About 150 + 70 + 45, plus 10 percent retries | Your plan's usage limits |
| Gemini | Verifier, one call per diagnosis; fallback judge | About 80 | Daily limit on the tier you use |
| Cloudflare Workers AI | Failover for small tasks | Rarely | Daily allowance |
| Hunter and Apollo | Recipient addresses | About 70 lookups over two calendar months | 50 and about 75 credits a month |
| Page fetches | Capture, slices, send-day rechecks | About 1,800 + 2,200 + 200 | Your own pacing rule: 1 per second per host |
| HubSpot API | Upserts | About 500 calls | Far below the free limit |
| Gmail API | First-touch drafts only | About 65 | Far below any quota |

Spread the Claude Code calls over the build: fifteen to twenty an evening is plenty. Run `--dry-run` before every batch; it prints the projected counts and costs nothing.

### Your time
| Work | Estimate |
| --- | --- |
| Phase 0, six teardowns by hand | 3 days at about 5 hours: 15 hours |
| Build, Phases 1 to 5 | 21 days at about 3 hours: 63 hours |
| Weekly hand-made teardowns during the build | 9 at about 90 minutes, using te show: 13 hours |
| Reviewing and sending 60 engine-made teardowns | 60 at 20 minutes: 20 hours |
| Follow-ups, replies, call prep, mid-campaign fixes | About 20 hours |
| Publishing, Phase 7 | 3 days at about 4 hours: 12 hours |

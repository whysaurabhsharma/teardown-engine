# TEARDOWN_SPEC.md

Version 1.1 · 2026-11-12
Not yet corrected from HAND_NOTES.md.
1.1: every evidence line shows its source; absences are shown as "Not found".

The teardown page, the rules the renderer and quality gates enforce, the two
email variants and the follow-ups. `engine/build/` (drafter, renderer,
`quality.py`) and `engine/outreach/followups.py` follow this file. Source:
docs/PLAN.md, "The teardown", "The page", "Rules the renderer enforces", "The
email, two variants", "Follow-ups", and Steps 52, 63, 64 and 65.

The teardown is the product. It is written as help for the person being hired,
never as a grade for the company.

---

## The page

One page, under 450 words excluding the proof slice. Eight sections, always in
this order.

| # | Section | Content | Limit |
| --- | --- | --- | --- |
| 1 | Header | "{Company}: notes for your {role} hire", the date, the sender's name | One line |
| 2 | What I looked at | The pages and records read, each linked, with the capture date | A short list |
| 3 | What your {role} will need | The need, and what could not be found on the public pages, with two or three quoted lines and their links | 80 words |
| 4 | Why now | The tie to the open role | 40 words |
| 5 | What I would build | Three concrete items, each a system component | 3 bullets |
| 6 | Already built for you | The proof slice. Up to ten rows on the page | One table |
| 7 | What I might have wrong | This is an outside view from public pages; name the specific thing that cannot be seen | 40 words |
| 8 | About me, and what I am looking for | Three lines on the sender, two links, and the offer stated plainly | 60 words |

Footer: the sender's postal address, one sentence on how to opt out, and a line
saying the page is unlisted and will be deleted on request.

Plain, readable HTML with one stylesheet; a page that looks like a document.

## Rules the renderer enforces

- Every sentence in section 3 traces to a passing claim (GAPS.md, evidence
  rules). No passing claim, no sentence.
- Every piece of evidence on the page shows where it came from:
  - Found: the quoted line, then a link to the page it was quoted from.
  - Not found: "Not found: no {page kind} linked from {where it was looked
    for}", with a link to that page and the capture date. Never quoted, never
    phrased as a fact about the company beyond what was looked for.
- Section 2 lists every page looked at, including those not found, each
  marked "found" or "not found". Pages that were blocked or unreachable are
  left out of the evidence entirely.
- The words describe what the pages show. They never assert what happens inside
  the company. "Your pricing page routes to a demo form" is fine. "Your reps are
  wasting time" is a guess.
- Quotes are short, under 25 words each. No logos and no screenshots of their
  site appear on the page.
- Competitors are named only after they have been confirmed in review.
- No superlatives about the sender. No individual is named except the
  recipient, and the recipient's name is a placeholder until the renderer fills
  it locally.
- The company name appears in the header and at least twice in the body, so a
  template cannot slip through unnoticed.
- Every row in the proof slice carries a source URL and a quote. A row without
  both is dropped before rendering. Where a slice field has no source, the cell
  reads "not found", never a guess.

## Banned phrases

Any of these fails the build:

- "I hope this finds you well"
- "quick question"
- "game-changing"
- "synergy"
- "leverage" as a verb
- "circle back"
- "pick your brain"

## Quality gates (`quality.py`)

A teardown fails before human review when any of these hold:

- a sentence in section 3 has no passing claim behind it
- a piece of evidence on the page has no source link, or a "Not found" line
  rests on a page that was blocked or unreachable
- fewer than two artifact kinds are cited
- a word limit is exceeded or a banned phrase appears
- the company name is missing from the body
- the verifier returned `not_supported` on any claim
- the offer stated in section 8 or the email does not match the signal's offer
  type (see `OFFER.md`)
- a competitor is named that was not confirmed

## The email, two variants

Under 120 words. One observation, one link, the offer stated, one small ask.
The link goes to the page, with no attachment, no tracking pixel and no
shortener. `{first_name}` and every other recipient field is a placeholder
filled by the renderer locally, never model input.

For a `candidate` signal (sent only after the application is submitted):

```
Subject: Applied for {role} at {Company}, with notes

Hi {first_name},

I applied for the {role} role at {Company} today. Alongside the CV, I looked
at what that hire will need on day one. {One sentence: the specific thing
the public pages show.}

I wrote it up and built {the proof slice, named concretely} for you: {link}

If it is useful, I would value 15 minutes. If I have misread something, I
would like to hear that too.

Saurabh
{one-line identity} · {postal address}
Not relevant? Reply "no" and I will not write again.
```

For an `adjacent` signal:

```
Subject: {Company}'s {role} hire: {three-word observation}

Hi {first_name},

I saw {Company} is hiring a {role}. {One sentence: what that hire will need,
and what the public pages suggest is missing.}

I wrote up what I found and built {the proof slice, named concretely} for
you: {link}

I build outbound and enrichment systems. I am looking for a GTM engineer
role and am also open to a short project. If the notes are useful, could we
talk for 15 minutes?

Saurabh
{one-line identity} · {postal address}
Not relevant? Reply "no" and I will not write again.
```

## Follow-ups

Three touches at most, then stop for good.

| Touch | When | Content | How |
| --- | --- | --- | --- |
| 1 | Day 0 | The email above | Gmail draft made by the engine; the sender sends |
| 2 | Day 4 | One new observation from a secondary gap, two sentences | `te queue followups` prints the text; the sender presses Reply in the thread and pastes. A LinkedIn connection request with no pitch may stand in for this touch |
| 3 | Day 9 | One line offering the full proof slice as a file, and a clear close | Same as touch 2 |

Hidden pricing or a broad job description may appear once, as a question, in a
follow-up; never as the primary finding.

Anyone who replies "no", or asks to stop, goes into `suppressions` the same day
(see `CONDUCT.md`).

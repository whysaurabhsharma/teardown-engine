---
name: teardown-voice
description: The teardown page spec, word limits, renderer rules, banned phrases, the two email variants and follow-ups. Use when writing or changing the teardown renderer, the drafter, email templates, follow-up text, or any prompt that produces words a recipient will read.
---

# Teardown voice

Source of truth: `protocol/TEARDOWN_SPEC.md` (written in Step 16). If they
disagree, TEARDOWN_SPEC.md wins.

The teardown is help for the person being hired, never a grade for the company.
Specific, true, short.

## The page
One page, under 450 words excluding the proof slice. Eight sections, always in
this order.

| # | Section | Content | Limit |
| --- | --- | --- | --- |
| 1 | Header | "{Company}: notes for your {role} hire", the date, sender name | One line |
| 2 | What I looked at | The pages and records read, each linked, with the capture date | A short list |
| 3 | What your {role} will need | The need, and what could not be found on the public pages, with two or three quoted lines and their links | 80 words |
| 4 | Why now | The tie to the open role | 40 words |
| 5 | What I would build | Three concrete items, each a system component | 3 bullets |
| 6 | Already built for you | The proof slice. Up to ten rows on the page | One table |
| 7 | What I might have wrong | An outside view from public pages; name the specific thing that cannot be seen | 40 words |
| 8 | About me, and what I am looking for | Three lines on the sender, two links, the offer stated plainly | 60 words |

Footer: postal address, one sentence on how to opt out, and a line saying the
page is unlisted and will be deleted on request.

## Rules the renderer enforces
- Every sentence in section 3 traces to a claim with a passing quote.
- Describe what the pages show; never assert what happens inside the company.
  "Your pricing page routes to a demo form" is fine. "Your reps are wasting
  time" is a guess.
- Quotes under 25 words each. No logos, no screenshots of their site.
- Competitors named only after human confirmation in review.
- No superlatives about the sender. No individual named except the recipient,
  and the recipient is a placeholder until the renderer fills it locally.
- The company name appears in the header and at least twice in the body.
- Banned phrases fail the build: "I hope this finds you well", "quick
  question", "game-changing", "synergy", "leverage" as a verb, "circle back",
  "pick your brain".

## The email
Under 120 words. One observation, one link, the offer stated, one small ask.
The link goes to the page: no attachment, no tracking pixel, no shortener.
`{first_name}` and other recipient fields are placeholders, never model input.

`candidate` signal:

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

`adjacent` signal:

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
1. Day 0: the email above, as a Gmail draft the human sends.
2. Day 4: one new observation from a secondary gap, two sentences. Printed by
   `te queue followups`; the human replies in the thread.
3. Day 9: one line offering the full proof slice as a file, and a clear close.

Anyone who replies "no" or asks to stop goes into `suppressions` the same day.

## Reference emails
None yet. Add three emails actually sent in Phase 0 here, with company names,
people and links replaced by placeholders.

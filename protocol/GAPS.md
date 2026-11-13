# GAPS.md

Version 1.2 · 2026-11-13
Corrected from HAND_NOTES.md v1, which covers one teardown, not six.
1.1: claims carry `found` and `url`; absence is a `not_found` claim, not a quote.
1.2: adds provisional G6 and the `contradicted` abstention, from HAND_NOTES.md.

The five gaps the judge may name, their evidence and abstain conditions, and
the rules that decide when the judge names one or abstains. The judge prompt
(`engine/judge/prompt.md`) pastes the gap table from this file. Source:
docs/PLAN.md, "The five gaps", "Design spine" items 4 and 5, Steps 44 to 50 and
"Five rules that save the project".

This file is bumped to v2 in Step 45, when every ruling invented while writing
the gold diagnoses is folded in.

---

## The task

Say what the new GTM hire will need on day one, and which one of the five gaps
the public pages show, or abstain. A gap is framed as what the hire needs, never
as a grade of the company.

## The five gaps

Each gap starts from what the new hire will need on day one. The judge must
pick one or abstain. Each needs evidence from at least two different artifact
kinds and maps to one proof slice.

| ID | What the hire will need | What the pages suggest is missing | Evidence required | Abstain when | Proof slice |
| --- | --- | --- | --- | --- | --- |
| G1 | A defined target. Who to call first | No visible definition of the ideal customer | The post asks the hire to build lists or define the ICP; the site has no segment, industry or use-case pages; the customers page is mixed or absent | The site names clear segments, or the post names target accounts | icp_accounts |
| G2 | One path from interest to a conversation | Pricing and the main call to action point in different directions on the same site | Pricing shows self-serve prices while the only CTA is a demo form, or pricing says contact sales while the CTA is a free signup | Pricing and CTA agree | funnel_compare |
| G3 | Tooling and process around them | A first GTM hire asked to be rep, operations and tooling at once | The post says founding or first hire and lists list-building, sequencing and CRM setup; no operations role is open; no sales tooling is detected | An operations role is open, or tooling is clearly in place | icp_accounts, shown with the recipe that produced it |
| G4 | Proof that speaks to their market | Hiring to sell into a region the site shows no customers from | The post names a target region; every case study and logo is from somewhere else | Case studies from the target region exist | icp_accounts, limited to that region |
| G5 | Inbound that reaches them | A demo path that asks a lot before anyone talks | The demo form has eight or more fields, or no booking step, or a general contact form is the only route; AE or SDR roles are open | The form is short and has a booking step | funnel_compare |
| G6 (provisional) | Reputation separation between the two sides' email before outbound to the sold-to side begins | Candidate-side (or user-side) sending machinery with no employer-side (buyer-side) equivalent | Public DNS shows a single sending domain with no sending subdomains, plus public evidence of high-volume outreach to the other side | A separate outbound domain exists, or SPF already authorises distinct senders per subdomain | dns_inventory |

G6 applies to two-sided marketplaces where one side is acquired at scale and
the other is sold to. It is provisional: it rests on one hand teardown
(HAND_NOTES.md), and is confirmed or removed at v2 in Step 45. Its evidence is
the `dns` artifact, which the company's own pages cannot show.

`role_funnel` is the fallback slice for any gap when the primary slice fails
(each open GTM role beside what it will need and what the site shows today).

## Not primary findings

Hidden pricing and a broad or unclear job description are usually deliberate,
and telling a hiring manager their own post is unclear is a poor opening.
Neither is ever a primary gap. Either may appear once, as a question, in a
follow-up.

## Abstain by default

- The judge is allowed, and expected, to return no gap. A company with no
  specific, evidenced observation is dropped as `no_gap` with a logged reason.
- Plan for 40 to 60 percent of qualified companies to be dropped here. A high
  drop rate is the system working.
- When a gap's abstain condition holds, that gap is not named.
- The judge can abstain because the evidence contradicts the gap it expected,
  not only because evidence is missing. It then sets `attempted` to false and
  starts `abstain_reason` with `contradicted:`, followed by the expected gap
  and the quote or record that contradicts it. In the one hand teardown, the
  hypothesis written before the evidence was wrong, and the evidence caught it.
- `abstain_reason` starts with one of three codes: `no_evidence:`,
  `contradicted:` or `thin:` (evidence fits but is below the confidence floor).

## Evidence rules

Every claim states its source. There are two kinds of claim:

- Found (`found: true`). The claim quotes the artifact text verbatim and
  carries that artifact's URL. No quote, no claim; the database rejects a
  found claim without both.
- Not found (`found: false`). The claim says a page or element is absent, so
  there is nothing to quote: `quote` is null and the claim names the
  `artifact_kind` that was looked for. `url` is the page where it was looked
  for (the homepage, its sitemap, or the page that should have held it). It is
  valid only when that artifact's capture outcome is `not_found`. An outcome of
  `blocked`, `dead` or `robots_denied` means it could not be seen, never that
  it is absent, and no not-found claim may rest on it.

- A diagnosis needs passing claims from at least two artifact kinds, and at
  least one of them must be a found claim. Absence alone never names a gap.
- Describe what the pages show, never what happens inside the company.
- The judge prompt holds public company pages and facts only. No person's name,
  title or address, and no field from the `contacts` table.

## Confidence anchors

| Confidence | Meaning |
| --- | --- |
| 0.9 | Two or more direct quotes and no counter-evidence |
| 0.6 | The evidence fits but one artifact is missing |
| 0.3 | A plausible reading you would not put your name to |

Below 0.6, abstain.

## Several gaps

A company can show several gaps. The judge returns the one with the strongest
evidence as `gap_type` and lists the others in `secondary`. A secondary gap
supplies the new observation for follow-up touch 2.

## `attempted`

`attempted` is an explicit boolean the judge must set. Never infer abstention
from a null `gap_type`. A diagnosis without `attempted` fails validation.

## Diagnosis contract (`engine/judge/schema.json`)

| Field | Type |
| --- | --- |
| attempted | bool, required, no default |
| gap_type | enum G1..G5, or null |
| confidence | number |
| need | string: what the hire will need |
| claims | array of {text, artifact_kind, found (bool), url, quote (string, or null when found is false)} |
| secondary | array of gap ids |
| abstain_reason | string or null |
| cannot_see | string: the specific thing the public pages cannot show, used in teardown section 7 |

The result is read from `structured_output`. If that field is missing, the
diagnosis is recorded as `scoring_failed`; free text is never parsed.

## Checks after the judge

1. Faithfulness (`faithfulness.py`). Normalise whitespace, case and smart
   quotes, then test each claim's quote as a substring of that artifact's
   stored text. A miss fails the claim. A near miss, at 95 or above on
   `rapidfuzz` partial ratio, is logged in its own category, but still fails.
   A found claim's `url` must equal the URL of the artifact it quotes. A
   not-found claim passes only if that artifact kind's capture outcome is
   `not_found`. A diagnosis with fewer than two passing claims from two
   artifact kinds, or with no passing found claim, becomes an abstention.
2. Verifier (`verify.py`). Each surviving claim goes to the verifier route with
   the quote, 300 characters of surrounding text and the gap's definition. It
   answers `supported`, `not_supported` or `unsure`. Two `not_supported` turn the
   diagnosis into an abstention. One `not_supported` fails the teardown at the
   quality gate before human review (Step 65); it is never sent as is. The
   verifier is never the model family that wrote the claim.

## Gold set

- Eighteen gold diagnoses: the Phase 0 six and the pilot twelve. Five are
  `teach` (both segments, at least three gap types, and one abstention); thirteen
  are `held_out`.
- The `teach` five are the judge's worked examples.
- `held_out` rows are never shown to the judge, in a prompt, a skill, a fixture
  or a chat. They exist only to score it.

Scoring on the held-out thirteen (Step 50):

| Measure | Target |
| --- | --- |
| Same call as the human: the same gap, or both abstain | 10 of 13 or better |
| Judge names a gap where the human abstained | 1 at most |
| Claims failing the faithfulness check | Zero reach a draft |

At most three prompt-revision rounds against the held-out set. Report the score
as a count out of thirteen.

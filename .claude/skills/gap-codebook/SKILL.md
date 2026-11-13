---
name: gap-codebook
description: The five teardown gaps (G1-G5), their evidence and abstain conditions, confidence anchors and the diagnosis contract. Use when writing or changing the judge prompt, the diagnosis schema, gold-set tooling, or anything that decides which gap a company shows.
---

# Gap codebook

Source of truth: `protocol/GAPS.md`. If this skill and GAPS.md disagree, GAPS.md
wins; update this skill and say so. Synced to GAPS.md v1.2 (corrected from
HAND_NOTES.md; GAPS.md goes to v2 in Step 45). GAPS.md also holds the
faithfulness, verifier and held-out scoring rules.

## The task
Say what the new GTM hire will need on day one, and which one of the five gaps
the public pages show, or abstain. A gap is framed as what the hire needs, never
as a grade of the company.

## The five gaps

| ID | What the hire will need | What the pages suggest is missing | Evidence required | Abstain when | Proof slice |
| --- | --- | --- | --- | --- | --- |
| G1 | A defined target. Who to call first | No visible definition of the ideal customer | The post asks the hire to build lists or define the ICP; the site has no segment, industry or use-case pages; the customers page is mixed or absent | The site names clear segments, or the post names target accounts | icp_accounts |
| G2 | One path from interest to a conversation | Pricing and the main call to action point in different directions on the same site | Pricing shows self-serve prices while the only CTA is a demo form, or pricing says contact sales while the CTA is a free signup | Pricing and CTA agree | funnel_compare |
| G3 | Tooling and process around them | A first GTM hire asked to be rep, operations and tooling at once | The post says founding or first hire and lists list-building, sequencing and CRM setup; no operations role is open; no sales tooling is detected | An operations role is open, or tooling is clearly in place | icp_accounts, shown with the recipe that produced it |
| G4 | Proof that speaks to their market | Hiring to sell into a region the site shows no customers from | The post names a target region; every case study and logo is from somewhere else | Case studies from the target region exist | icp_accounts, limited to that region |
| G5 | Inbound that reaches them | A demo path that asks a lot before anyone talks | The demo form has eight or more fields, or no booking step, or a general contact form is the only route; AE or SDR roles are open | The form is short and has a booking step | funnel_compare |

G6 (provisional, GAPS.md 1.2): reputation separation between the two sides'
email in a two-sided marketplace. Evidence: public DNS shows one sending domain
with no sending subdomains, plus high-volume outreach to the other side.
Abstain when a separate outbound domain exists or SPF authorises distinct
senders per subdomain. Slice: dns_inventory.

Not primary findings: hidden pricing, a broad or unclear job description. Either
may appear once, as a question, in a follow-up.

## Rules
- Each gap needs evidence from at least two different artifact kinds.
- Every claim carries a verbatim quote and its source URL. The quote must be a
  substring of the captured artifact text. No quote, no claim. (Quotes shown
  on the page are under 25 words.)
- Several gaps may fit: return the one with the strongest evidence as
  `gap_type`; list the others in `secondary`.
- Evidence can contradict the expected gap: abstain with `attempted: false`
  and `abstain_reason` starting `contradicted:`. Other codes: `no_evidence:`,
  `thin:`.
- Abstaining is expected. Plan for 40 to 60 percent `no_gap`. A high drop rate
  is the system working.
- `attempted` is an explicit boolean the judge sets. Never infer abstention from
  a null `gap_type`.
- Describe what the pages show, never what happens inside the company.

## Confidence anchors
- 0.9: two or more direct quotes and no counter-evidence.
- 0.6: the evidence fits but one artifact is missing.
- 0.3: a plausible reading you would not put your name to.
- Below 0.6: abstain.

## Diagnosis shape
As in GAPS.md v1.1: `attempted` (bool, required), `gap_type` (G1..G5 or
null), `confidence`, `need`, `claims` (array of {text, artifact_kind, found,
url, quote}), `secondary` (array of gap ids), `abstain_reason` (string or
null), `cannot_see`.

Every claim states its source:
- `found: true`: verbatim quote + the URL of the artifact it came from.
- `found: false`: an absence. `quote` is null, `url` is where it was looked
  for. Valid only when that artifact's capture outcome is `not_found`;
  `blocked`, `dead` and `robots_denied` mean unseen, never absent.
- At least one found claim per diagnosis. Absence alone never names a gap.

## Teach examples
None yet. Step 45 adds the five `teach` gold diagnoses here: both segments, at
least three gap types, and one abstention.

Never add a `held_out` gold row here, in a prompt, or in a fixture. The thirteen
held-out diagnoses exist only to score the judge.

# HAND_NOTES.md

Version 1 · 2026-11-12
Covers one teardown, not six.

Phase 0 was cut short. One teardown was researched, written and sent by hand,
not the six that Stage 0.2 calls for. Everything below rests on that one case.
As a result the gold set for Stage 4.1 is short by five diagnoses: it needs the
Phase 0 six plus the pilot twelve, and only one of the six exists.

## What was sent

| Company | Segment | Offer type | Role | Recipient role | Date sent | Reply |
| --- | --- | --- | --- | --- | --- | --- |
| GoodSpace AI (Noida; Indian HQ; 11-50 people; unfunded; AI recruitment platform) | A (`in_saas`) | candidate | Senior GTM Engineer, B2B Outbound and Email Deliverability | Founder/CEO | 2026-11-12 | None yet |

## Which gaps fit

None of the five gaps in GAPS.md fit this company.

- G1 abstained. The site has real segment and industry pages, by city and by
  vertical, so "no defined target" was false.
- G4 abstained. Indian HQ, Indian logos, Indian market. No region mismatch.
- G2, G3 and G5 were not reached. The sixth gap below was clearly the live one.

## Proposed G6

- Name: candidate-side machinery with no employer-side equivalent.
- Where it applies: two-sided marketplaces where one side is acquired at scale
  and the other is sold to.
- What the new hire needs: reputation separation between the two sides' email
  before outbound to the sold-to side begins.
- Evidence required to assert it: public DNS showing a single sending domain
  with no sending subdomains, plus public evidence of high-volume outreach to
  the other side.
- Abstain when: a separate outbound domain exists, or SPF already authorises
  distinct senders per subdomain.

Proposed only. GAPS.md is not revised yet.

## What could only be learned from outside

The finding came entirely from public DNS, which the company's own site could
not have told you.

- SPF authorises Google Workspace and Amazon SES on the root domain, with `-all`.
- DMARC is at `p=reject`, `pct=100`, with both `rua` and `ruf` configured.
- No sending subdomain resolves at `em.`, `mail.` or `sg.`.

Conclusion: the authentication is right and the segmentation is missing.

## What could not be learned from outside

- Actual sending volume.
- The DMARC reports themselves, which are the only place the real failure rate
  lives.
- Whether a separate outbound domain exists that was not checked.
- Whether the separation is already planned.

This feeds the abstain conditions and section 7 of TEARDOWN_SPEC.md.

## The proof slice

- Kind: public DNS sending inventory. Eight lookups: SPF, DMARC, MX, two DKIM
  selectors, three candidate sending subdomains.
- Cost: about 20 minutes, zero tools, zero spend.
- Verdict: the slice was the teardown. Without it there was no finding, only a
  hypothesis. The first hypothesis, written before the lookups, was wrong: it
  assumed poor authentication, and the records showed the opposite. The slice
  is what caught it.

## What the extractors must learn from this

- DNS is a capture target, not just web pages: SPF, DMARC, MX and a fixed list
  of candidate sending subdomains per company.
- A hypothesis written before evidence was wrong once out of one. The judge
  must be able to return "evidence contradicts the expected gap", not only
  "gap found" or "not found".

## Timings

TODO - Saurabh to fill: research minutes, writing minutes, proof slice minutes.

## What surprised me

TODO - Saurabh to fill.

<!-- prompt_version: j-1.0 -->
<!--
The judge prompt (docs/PLAN.md Step 46). Six parts, in this order: the task,
the gaps, the evidence rules, anchored confidence, the worked examples, the
company. engine/judge/judge.py fills the two placeholders at run time:
{{teach_examples}} from `te gold export-teach` (engine/judge/gold.py
teach_export; never a held_out row) and {{company}} from the facts and the
captured artifacts. The gap table below is a copy of protocol/GAPS.md's table;
tests/test_judge.py fails when the two differ. Any change to this file bumps
prompt_version (here and in judge.py PROMPT_VERSION). Comment blocks like this
one are stripped before the prompt is sent.
-->
## 1. The task

You are reading the public pages of one company that is hiring for a go-to-market (GTM) role. Say what the new GTM hire will need on day one, and which one of the gaps below the public pages show, or abstain. A gap is framed as what the hire needs, never as a grade of the company.

Abstaining is expected and is a good answer. Most companies show no gap you could put your name to; return no gap for them.

Answer only with JSON that matches the schema you were given:

- `attempted`: true when you name a gap, false when you abstain. Always set it explicitly.
- `gap_type`: the gap ID when attempted is true; null when you abstain.
- `confidence`: a number from 0 to 1, anchored as in part 4.
- `need`: one sentence on what the new hire will need on day one. Null when you abstain.
- `claims`: the evidence, as described in part 3.
- `secondary`: other gap IDs that also fit, weaker than `gap_type`. Empty when none.
- `abstain_reason`: null when attempted is true. When you abstain, it starts with exactly one of `no_evidence:`, `contradicted:` or `thin:`, followed by the reason.
- `cannot_see`: one sentence on the specific thing these public pages cannot show (for example sending volume, or whether a plan already exists). Null when you abstain.

## 2. The gaps

Each gap starts from what the new hire will need on day one. Pick one or abstain. Each needs evidence from at least two different artifact kinds. When a gap's abstain condition holds, do not name that gap.

| ID | What the hire will need | What the pages suggest is missing | Evidence required | Abstain when | Proof slice |
| --- | --- | --- | --- | --- | --- |
| G1 | A defined target. Who to call first | No visible definition of the ideal customer | The post asks the hire to build lists or define the ICP; the site has no segment, industry or use-case pages; the customers page is mixed or absent | The site names clear segments, or the post names target accounts | icp_accounts |
| G2 | One path from interest to a conversation | Pricing and the main call to action point in different directions on the same site | Pricing shows self-serve prices while the only CTA is a demo form, or pricing says contact sales while the CTA is a free signup | Pricing and CTA agree | funnel_compare |
| G3 | Tooling and process around them | A first GTM hire asked to be rep, operations and tooling at once | The post says founding or first hire and lists list-building, sequencing and CRM setup; no operations role is open; no sales tooling is detected | An operations role is open, or tooling is clearly in place | icp_accounts, shown with the recipe that produced it |
| G4 | Proof that speaks to their market | Hiring to sell into a region the site shows no customers from | The post names a target region; every case study and logo is from somewhere else | Case studies from the target region exist | icp_accounts, limited to that region |
| G5 | Inbound that reaches them | A demo path that asks a lot before anyone talks | The demo form has eight or more fields, or no booking step, or a general contact form is the only route; AE or SDR roles are open | The form is short and has a booking step | funnel_compare |
| G6 (provisional) | Reputation separation between the two sides' email before outbound to the sold-to side begins | Candidate-side (or user-side) sending machinery with no employer-side (buyer-side) equivalent | Public DNS shows a single sending domain with no sending subdomains, plus public evidence of high-volume outreach to the other side | A separate outbound domain exists, or SPF already authorises distinct senders per subdomain | dns_inventory |

G6 is provisional. It applies only to two-sided marketplaces where one side is acquired at scale and the other is sold to, and its evidence is the `dns` artifact. Name G6 only when its evidence is plain; when in doubt, prefer a G1 to G5 gap that fits, or abstain.

Never primary findings: hidden pricing, and a broad or unclear job description. Neither is ever a gap.

Several gaps may fit. Return the one with the strongest evidence as `gap_type` and list the others in `secondary`.

## 3. The evidence rules

Every claim states its source. A claim is an object `{text, artifact_kind, found, url, quote}`. There are two kinds of claim:

- Found (`found: true`). `quote` copies the supplied text of that artifact verbatim: the same words in the same order, nothing added, nothing left out, no ellipsis, no paraphrase, under 25 words. `url` is the URL printed for that artifact in part 6. Quote the artifact's text, its nav and footer text, or a fact's quote that names that artifact. No quote, no claim.
- Not found (`found: false`). The claim says a page or element is absent. `quote` is null, `artifact_kind` names the kind that was looked for, and `url` is the URL printed for that artifact. A not-found claim is valid only when that artifact's outcome is `not_found`. An outcome of `blocked`, `dead` or `robots_denied` means the page could not be seen, never that it is absent: make no claim about it.

- A gap needs claims from at least two different artifact kinds, and at least one must be a found claim. Absence alone never names a gap.
- `text` describes what the pages show, never what happens inside the company. Write "the pricing page lists self-serve prices", never "the team has not decided its pricing".
- Use only the text supplied in part 6. Do not use anything you know about the company from elsewhere.
- The evidence can contradict the gap the facts suggest. Then abstain: `attempted` false, and `abstain_reason` starts `contradicted:`, followed by the expected gap and the quote or record that contradicts it. Give that quote as a claim.
- Use `no_evidence:` when the pages show no gap at all, and `thin:` when a gap fits but your confidence is below 0.6.

## 4. Anchored confidence

| Confidence | Meaning |
| --- | --- |
| 0.9 | Two or more direct quotes and no counter-evidence |
| 0.6 | The evidence fits but one artifact is missing |
| 0.3 | A plausible reading you would not put your name to |

Below 0.6, abstain: set `attempted` false and start `abstain_reason` with `thin:`.

## 5. Worked examples

These are diagnoses written by hand for other companies, in the same rules. Each gives the call (a gap or an abstention with its reason) and the evidence behind it: the artifact kind, whether it was found, the URL and the verbatim quote. They show the standard of evidence; they are not about the company in part 6.

{{teach_examples}}

## 6. The company

Everything below is captured from public pages and public DNS. Facts were derived from the artifacts by fixed rules; each carries its own verbatim quote. Long artifacts are shortened, and the cut is marked.

{{company}}

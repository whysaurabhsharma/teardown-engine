# ICP.md

Version 1.2 · 2026-11-13
Checked against HAND_NOTES.md v1 (one teardown, not six); no rule changed.
1.1: adds the `duplicate` drop reason, applied at human review.
1.2: version line only.

Who qualifies, who is excluded, and how each company is given a segment.
`engine/resolve/segment.py` and `engine/resolve/qualify.py` read this file.
Source: docs/PLAN.md, "The two segments and the two offers", "Qualification
rules", "One company, one teardown", Steps 25, 28, 29, 30, 40 and 43.

Do not loosen these rules to hit a target. If the market yields fewer qualified
companies than planned, that is a finding about the size of the niche, and the
campaign ends at a smaller number.

---

## The two segments

Same engine, two buyers, two arguments. Aim for at least 25 sends in each
segment. The segment tag set in E2 chooses the framing and the send time; the
offer type set in E1 (see `OFFER.md`) chooses what is asked for.

|  | `in_saas` (Indian SaaS) | `us_india_team` (US startups with India teams) |
| --- | --- | --- |
| Who | HQ in India, B2B software, roughly 10 to 300 people, selling to Indian SMBs or to the US from India | HQ outside India, seed to Series B, with a sales or SDR pod in Bengaluru, Hyderabad, Pune, Gurugram, Mumbai or Chennai |
| How they are found | A hand-kept seed list read by the careers-page collector, plus roles entered by hand | Greenhouse, Lever and Ashby posts whose location is an Indian city |
| Segment rule in E2 | Head office in India. The role may sit anywhere: an Indian company hiring a US account executive is a strong signal | Head office outside India and the role is located in India |
| What they usually lack | Tooling budget and targeting. One founder and one or two reps working a spreadsheet | Someone in the India time zone who thinks in systems. The tools are already paid for |
| What lands | An enrichment layer they cannot justify buying, built with no paid data tool | Research quality and measurement discipline, and proof of shipping |
| Best proof slice | Ten accounts in their ICP, each with a reason to act now | Their funnel beside two competitors, or their open roles against what each will need |
| Send window | 9 to 11 am IST, Tuesday to Thursday | 9 to 11 am in the recipient's own time zone. Many India-pod managers sit in the US |
| The cost line | Lead with it | Mention once. They care more that it works |

## Segment and location rule (E2, Step 28)

Look for the head office in the footer, the contact page and the legal pages,
where entity names such as "Private Limited", "Pvt Ltd", "Inc." and "Delaware"
appear.

| Head office | Role location | Result |
| --- | --- | --- |
| India | Anywhere | `in_saas` |
| Outside India | In India | `us_india_team` |
| Outside India | Not in India | drop as `out_of_scope` |
| Unclear | Any | `needs_review` |

The location rule applies only when the head office is abroad. An Indian
company's role counts wherever it sits. Because the head office is not known
until E2, the location rule is applied in E2, not in E1.

India locations used for discovery (Step 22): Bengaluru, Bangalore, Hyderabad,
Pune, Gurugram, Gurgaon, Mumbai, Chennai, Noida, Remote India.

## A company qualifies when all of these hold

1. It sells software or a software-enabled service to other businesses.
2. It has an open GTM role posted or updated in the last 60 days. For signals
   with no post date (careers pages, manual entry), `first_seen` stands in for
   the post date.
3. It has a working website with at least a homepage and one of: pricing,
   product, customers.
4. A recipient can be named and reached by email or LinkedIn.

## A company is dropped, with the reason logged, when any of these hold

Every drop writes `drop_reason` and `drop_stage`. Status only moves forward,
or to `dropped`.

| Drop reason | Rule | Applied at |
| --- | --- | --- |
| out_of_scope | Head office outside India and no India-located role | E2 segment tagging (Step 28) |
| staffing_or_agency | A recruiter, staffing firm or outsourcing shop posting on a client's behalf | E2 qualify |
| consumer | Sells to consumers only | E2 qualify |
| too_large | More than roughly 1,000 people; the recipient will never see it | Human review (Step 30) |
| duplicate | The same company as another row under a different domain (flagged by `te resolve dedupe`); the other row is kept | Human review (Step 30) |
| conflict | A company worked with, a direct competitor of one, or anywhere current company interests make contact awkward. The list lives in `OFFER.md`, "Companies I will not contact" | Human review (Step 30) |
| no_site_evidence | Fewer than three usable artifacts after capture | E3 capture |
| no_recipient | No named owner of the problem, or no way to reach them | People step (Step 43) |
| no_gap | The judge abstained | E5 judge |
| stale_signal | The role closed before sending, and no other GTM role is open | Send day (Step 70) |

`qualify.py` applies the automatable E2 rules in a fixed order, the order of
the table above (`out_of_scope`, `staffing_or_agency`, `consumer`), and drops on
the first failure. The later rows are applied by the stage named beside them. Rules that need a human, such as
company size and conflicts, are left for `te resolve review`, where the
approximate headcount is typed in.

## One company, one teardown

A company with several open GTM roles is still one record and one send. Keep a
`candidate` role as the signal if there is one; otherwise the most senior GTM
role. The count of roles becomes a fact the judge can use.

If the signal role closes before send day and another GTM role is open, the
teardown is re-drafted against that role instead of dropping.

## Recipient rule (Step 40)

| Company size | Recipient |
| --- | --- |
| Under 50 people | The founder or CEO |
| 50 to 200 | Head of sales, VP Sales, CRO or head of growth |
| Above 200 | The hiring manager named in the post, or a director-level sales or RevOps lead |

A recruiter is never the recipient.

Channel (Step 43): `email` when the address confidence is `high` or `medium`;
otherwise `linkedin`, sent by hand with the same link. With neither, drop as
`no_recipient`.

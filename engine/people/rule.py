"""The recipient rule (docs/PLAN.md Step 40; protocol/ICP.md "Recipient rule").

    rule = recipient_rule(headcount)

| Company size | Recipient                                                         |
| ------------ | ----------------------------------------------------------------- |
| under 50     | the founder or CEO                                                |
| 50 to 200    | head of sales, VP Sales, CRO or head of growth                    |
| above 200    | the hiring manager named in the post, or a director-level sales   |
|              | or RevOps lead                                                    |

A recruiter is never the recipient. An unknown headcount is said out loud:
the rule asks the person for it rather than guessing a band.

Pure: no database, no network, no model calls. The person picks the recipient;
title_fits only lets the CLI warn when a typed title does not look like the
band's target, and is_recruiter_title is the one hard refusal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

RULE_VERSION = "recipient-rule-1.0"  # ICP.md 1.2, "Recipient rule"

SMALL_BELOW = 50  # under 50: founder or CEO
MID_MAX = 200  # 50 to 200 inclusive: sales or growth head; above: hiring manager / director

Band = Literal["under_50", "50_to_200", "over_200", "unknown"]

RECRUITER_NOTE = "A recruiter, talent or HR person is never the recipient."


@dataclass(frozen=True)
class RecipientRule:
    band: Band
    headcount: int | None
    target: str  # who to look for, in words
    ask: str | None = None  # set when the rule cannot be applied without the person

    @property
    def known(self) -> bool:
        return self.band != "unknown"


def recipient_rule(headcount: int | None) -> RecipientRule:
    """The target recipient for a company of about `headcount` people."""
    if headcount is None:
        return RecipientRule(
            "unknown", None,
            "unknown: the company's headcount is not recorded, so the rule cannot "
            "pick a band",
            ask="About how many people work there? Record it in the human review "
                "(`te resolve review --company <id>`), then run this again.",
        )
    if isinstance(headcount, bool) or not isinstance(headcount, int) or headcount <= 0:
        raise ValueError("headcount must be a positive whole number or None")
    if headcount < SMALL_BELOW:
        return RecipientRule("under_50", headcount, "the founder or CEO")
    if headcount <= MID_MAX:
        return RecipientRule("50_to_200", headcount,
                             "the head of sales, VP Sales, CRO or head of growth")
    return RecipientRule("over_200", headcount,
                         "the hiring manager named in the job post, or a director-level "
                         "sales or RevOps lead")


# --- titles --------------------------------------------------------------------------

_RECRUITER = re.compile(
    r"\brecruit"                       # recruiter, recruiting, recruitment
    r"|\btalent\b"                     # talent acquisition, talent partner
    r"|\bsourcer\b|\bsourcing\s+(?:specialist|partner|lead)\b"
    r"|\bstaffing\b"
    r"|\bhr\b|\bhrbp\b|\bhuman\s+resources?\b"
    r"|\bpeople\s+(?:ops|operations|partner|team|officer|function|and\s+culture|&\s*culture)\b"
    r"|\b(?:head|vp|vice\s+president|director|chief|manager|lead)\s+(?:of\s+)?people\b"
    r"|\bchief\s+people\b"
    r"|\bhiring\s+(?:partner|coordinator|specialist)\b",
    re.IGNORECASE,
)

_FITS: dict[str, re.Pattern[str]] = {
    "under_50": re.compile(
        r"\b(?:co-?\s?)?founder\b|\bceo\b|\bchief\s+executive\b|\bmanaging\s+director\b",
        re.IGNORECASE),
    "50_to_200": re.compile(
        r"\bhead\s+of\s+(?:sales|growth|revenue)\b"
        r"|\b(?:vp|svp|evp|vice\s+president)\b[\s,\-]*(?:of\s+)?(?:sales|growth|revenue)\b"
        r"|\bcro\b|\bchief\s+revenue\b|\bchief\s+growth\b",
        re.IGNORECASE),
    "over_200": re.compile(
        r"\bdirector\b.*\b(?:sales|revenue|revops|rev\s*ops|go-to-market|gtm)\b"
        r"|\b(?:sales|revenue|revops|rev\s*ops)\b.*\bdirector\b"
        r"|\bhead\s+of\s+(?:sales|revenue|revops|rev\s*ops|sales\s+operations)\b"
        r"|\b(?:vp|vice\s+president)\b.*\b(?:sales|revenue)\b",
        re.IGNORECASE),
}


def is_recruiter_title(title: str) -> bool:
    """True for a recruiter, talent acquisition, HR or people-team title."""
    return bool(_RECRUITER.search(" ".join(title.split())))


def title_fits(rule: RecipientRule, title: str) -> bool | None:
    """Whether a typed title looks like the band's target.

    None when the rule cannot tell: an unknown band, or a company above 200
    where the hiring manager named in the post may hold any title.
    """
    if rule.band == "unknown":
        return None
    hit = bool(_FITS[rule.band].search(" ".join(title.split())))
    if rule.band == "over_200" and not hit:
        return None
    return hit

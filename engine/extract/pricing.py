"""pricing_model (docs/PLAN.md Step 37): what the pricing page offers.

| Value          | Rule (on the pricing page's extracted text)                         |
| -------------- | ------------------------------------------------------------------- |
| numbers        | a currency amount or a per-period price ("₹2,000 / month", "$49")   |
| free_or_trial  | no amount; "free trial", "free plan", "start free", "14-day trial"  |
| contact_sales  | neither; "contact sales", "talk to our team", "book a demo", "custom pricing", "get a quote" |
| unclassified   | the page was seen and none of the three matched (found False)       |
| none           | the pricing page is not_found (found False)                         |

The first rule that matches wins, in the order above: a page with prices and a
"contact sales for Enterprise" line is `numbers`. Nothing is written when the
pricing page was blocked, dead or robots_denied.
"""

from __future__ import annotations

import re

from engine.extract.base import Bundle, FactOut, absent, not_found, text_quote

KIND = "pricing"
FACT = "pricing_model"

NUMBERS = re.compile(
    r"(?:[$€£₹]\s?\d"
    r"|\b(?:Rs\.?|INR|USD|EUR|GBP|AED|SGD)\s?\d"
    r"|\b\d[\d,.]*\s?(?:/\s?(?:mo|month|user|seat|yr|year|agent)\b"
    r"|per\s+(?:month|user|seat|year|agent)\b))",
    re.IGNORECASE,
)
FREE = re.compile(
    r"\bfree\s+(?:trial|plan|forever|tier)\b|\bstart\s+(?:for\s+)?free\b"
    r"|\btry\s+(?:it\s+)?(?:for\s+)?free\b|\b\d+[- ]day\s+(?:free\s+)?trial\b"
    r"|\bget\s+started\s+(?:for\s+)?free\b",
    re.IGNORECASE,
)
CONTACT = re.compile(
    r"\b(?:contact|talk\s+to|speak\s+(?:to|with)|chat\s+with)\s+(?:our\s+)?"
    r"(?:sales|team|us|an?\s+expert)\b"
    r"|\b(?:book|request|schedule|get)\s+a\s+(?:demo|call|quote)\b"
    r"|\bcustom\s+(?:pricing|quote|plans?)\b|\bpricing\s+on\s+request\b",
    re.IGNORECASE,
)
RULES = (("numbers", NUMBERS), ("free_or_trial", FREE), ("contact_sales", CONTACT))


def pricing_model(b: Bundle) -> list[FactOut]:
    st = b.state(KIND)
    if st == "not_found":
        return [not_found(FACT, "none", KIND)]
    if st != "ok":
        return []
    art = b.arts[KIND]
    text = art.text or ""
    for value, rx in RULES:
        m = rx.search(text)
        if m:
            return [text_quote(FACT, value, art, m.start(), m.end())]
    return [absent(FACT, "unclassified", KIND, "price, free or contact-sales wording")]


EXTRACTORS = (("pricing_model", pricing_model, (KIND,)),)

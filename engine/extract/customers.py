"""Who the site sells to and against (docs/PLAN.md Step 37): customer_regions
and named_competitors.

customer_regions
----------------
Region and country names (lexicons.yaml regions) in the customers page's
extracted text; nav and footer are not read, so an office address there does
not count. One fact per region; "none" when the page was seen and names none
(found False, absent) or when the page is not_found (found False, not_found).

named_competitors
-----------------
Names the company sets itself against on its compare page, read from the
text by three patterns, each name being one to four capitalised words:
"A vs B" (also "vs.", "versus"), "alternative(s) to B", "B alternative(s)".
Leading filler (Best, Top, The, Why, Compare, Our, Free) is dropped, and so
is the company's own name (its name, the name's first word, or the domain's
first label). One fact per name, quoting the line. "none" as above.
"""

from __future__ import annotations

import re

from engine.extract.base import (
    Bundle,
    FactOut,
    absent,
    lexicon_hits,
    lexicons,
    not_found,
    text_quote,
)

CUSTOMERS = "customers"
COMPARE = "compare"


def customer_regions(b: Bundle) -> list[FactOut]:
    st = b.state(CUSTOMERS)
    if st == "not_found":
        return [not_found("customer_regions", "none", CUSTOMERS)]
    if st != "ok":
        return []
    art = b.arts[CUSTOMERS]
    hits = lexicon_hits("customer_regions", art, lexicons()["regions"])
    return hits or [absent("customer_regions", "none", CUSTOMERS, "region or country name")]


_NAME = r"[A-Z0-9][\w.&'+-]*(?: +[A-Z0-9][\w.&'+-]*){0,3}"  # one line only
_VS = re.compile(rf"({_NAME}) +(?:vs\.?|VS\.?|Vs\.?|versus) +({_NAME})")
_ALT_TO = re.compile(rf"\b[Aa]lternatives? +to +({_NAME})")
_ALTS = re.compile(rf"({_NAME}) +[Aa]lternatives?\b")
_FILLER = frozenset({"best", "top", "the", "why", "compare", "comparing", "our", "free",
                     "a", "an", "vs", "and", "or", "how", "see"})


def _clean(name: str) -> str:
    words = name.split()
    while words and words[0].casefold().rstrip(":.,") in _FILLER:
        words = words[1:]
    return " ".join(words).rstrip(":.,;!?")


def _own(b: Bundle) -> set[str]:
    own = {b.name.casefold()}
    if b.name.split():
        own.add(b.name.split()[0].casefold())
    if b.domain:
        own.add(b.domain.split(".")[0].casefold())
    return own


def named_competitors(b: Bundle) -> list[FactOut]:
    st = b.state(COMPARE)
    if st == "not_found":
        return [not_found("named_competitors", "none", COMPARE)]
    if st != "ok":
        return []
    art = b.arts[COMPARE]
    text = art.text or ""
    own = _own(b)
    found: dict[str, tuple[int, int]] = {}
    for rx, groups in ((_VS, (1, 2)), (_ALT_TO, (1,)), (_ALTS, (1,))):
        for m in rx.finditer(text):
            for g in groups:
                name = _clean(m.group(g))
                key = name.casefold()
                if not name or key in own or key.split()[0] in own:
                    continue
                if key not in {k.casefold() for k in found}:
                    found[name] = (m.start(g), m.end(g))
    out = [text_quote("named_competitors", name, art, s, e) for name, (s, e) in found.items()]
    return out or [absent("named_competitors", "none", COMPARE, "competitor name")]


EXTRACTORS = (
    ("customer_regions", customer_regions, (CUSTOMERS,)),
    ("named_competitors", named_competitors, (COMPARE,)),
)

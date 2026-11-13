"""E4 Extract: facts derived by rule from the frozen bundle (docs/PLAN.md Steps 37-38).

One pure function per fact, grouped by what it reads: dns.py (built first:
protocol/HAND_NOTES.md says the one hand teardown rested on DNS alone),
stack.py, pricing.py, cta.py, forms.py, roles.py, jd.py, customers.py. Tool
footprints live in patterns.yaml and word lists in lexicons.yaml. run.py
writes the facts table and moves companies captured -> extracted.

Zero network and zero model calls. The one model-backed piece, the title
classifier, is read only from its cache in llm_calls.
"""

from __future__ import annotations

from collections.abc import Callable

from engine.extract import cta, customers, dns, forms, jd, pricing, roles, stack
from engine.extract.base import Bundle, FactOut, lexicons, patterns

FACTS_VERSION = "fx-1.0"

Extractor = tuple[str, Callable[[Bundle], list[FactOut]], tuple[str, ...]]

# (fact, function, artifact kinds it reads), in run order: DNS first.
EXTRACTORS: tuple[Extractor, ...] = (
    *dns.EXTRACTORS,
    *stack.EXTRACTORS,
    *pricing.EXTRACTORS,
    *cta.EXTRACTORS,
    *forms.EXTRACTORS,
    *roles.EXTRACTORS,
    *jd.EXTRACTORS,
    *customers.EXTRACTORS,
)

FACTS: tuple[str, ...] = tuple(name for name, _, _ in EXTRACTORS)


def extractor_version() -> str:
    """What every facts row records: the rules, the patterns and the lexicons."""
    return f"{FACTS_VERSION}/{patterns()['version']}/{lexicons()['version']}"

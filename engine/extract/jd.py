"""Job post facts (docs/PLAN.md Step 37): jd_tools, jd_target_market,
jd_scope_flags. Each matches the job_post's extracted text against a fixed
lexicon in lexicons.yaml and writes one fact per entry found, value the entry
id, quoting the line (or a window of it) around the first match.

A seen post with no match writes "none" (found False, absent). A job_post
that was blocked, dead or robots_denied (a LinkedIn or Naukri URL is always
robots_denied) writes nothing.

jd_target_market reads every region the post names, including the job's own
location line ("Bengaluru, India"): the lexicon cannot tell "based in" from
"selling into". The judge reads the quote.
"""

from __future__ import annotations

from engine.extract.base import Bundle, FactOut, absent, lexicon_hits, lexicons

KIND = "job_post"


def _run(b: Bundle, fact: str, lex_key: str, what: str) -> list[FactOut]:
    if b.state(KIND) != "ok":
        return []
    art = b.arts[KIND]
    hits = lexicon_hits(fact, art, lexicons()[lex_key])
    return hits or [absent(fact, "none", KIND, what)]


def jd_tools(b: Bundle) -> list[FactOut]:
    return _run(b, "jd_tools", "jd_tools", "tool name from the lexicon")


def jd_target_market(b: Bundle) -> list[FactOut]:
    return _run(b, "jd_target_market", "regions", "region or country name")


def jd_scope_flags(b: Bundle) -> list[FactOut]:
    return _run(b, "jd_scope_flags", "scope_flags", "scope phrase")


EXTRACTORS = (
    ("jd_tools", jd_tools, (KIND,)),
    ("jd_target_market", jd_target_market, (KIND,)),
    ("jd_scope_flags", jd_scope_flags, (KIND,)),
)

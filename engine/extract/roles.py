"""Hiring facts (docs/PLAN.md Step 37): open_roles_total, gtm_roles_by_family,
first_gtm_hire.

open_roles_total
----------------
The number of jobs in the ats_board JSON (its structure's job_count), quoting
the first job title in the board text. A board with no jobs is 0, found
False. With no board (ats_board not_found), the careers page's job links are
counted instead: links outside nav and footer to a known ATS host or to a
path with a page under /jobs, /job, /positions, /openings, /roles or
/careers. A careers page with no such link writes nothing: the jobs may be
drawn by script. Blocked, dead or robots_denied boards write nothing.

gtm_roles_by_family
-------------------
Each board title through the title classifier (engine/signal/classify.py):
its rules, then a model answer already in the llm_calls cache. A title
neither places is counted as "unclassified"; this stage never calls a model.
One fact per family present, value {"family": f, "count": n}, quoting the
first title of that family.

first_gtm_hire
--------------
True when the job post says "founding", or "first" before a GTM word ("our
first sales hire"), and the board shows no other sales title open (sdr, ae
or sales_lead, other than the post itself, matched by the signal's title).
- No founding/first wording in a seen post: False (absent).
- Founding wording and another sales title open: False, quoting that title.
- Founding wording but the board is not readable, or holds unclassified
  titles that might be sales roles: nothing is written (it cannot be told).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from engine.extract.base import (
    Bundle,
    FactOut,
    absent,
    best_quote,
    exact_text_quote,
    host_path,
    host_matches,
    structure_quote,
    text_quote,
)

BOARD = "ats_board"
CAREERS = "careers"
POST = "job_post"
SALES_FAMILIES = frozenset({"sdr", "ae", "sales_lead"})
ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "keka.com",
             "zohorecruit.com", "zohorecruit.in", "freshteam.com", "darwinbox.in",
             "smartrecruiters.com", "recruitee.com", "breezy.hr", "bamboohr.com")
_JOB_DIRS = frozenset({"jobs", "job", "positions", "openings", "roles", "careers", "career"})

FOUNDING = re.compile(
    r"\bfounding\b"
    r"|\bfirst\s+(?:\w+\s+){0,2}?(?:hire|sales|gtm|go-to-market|seller|rep|ae|sdr|bdr"
    r"|account\s+executive|marketer|growth|revenue)\b",
    re.IGNORECASE,
)


def _title_key(t: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", t).split()).casefold()


def _titles(b: Bundle) -> list[str]:
    jobs = (b.arts[BOARD].structure or {}).get("jobs") or []
    return [j["title"] for j in jobs if j.get("title")]


def _title_quote(fact: str, value, b: Bundle, title: str) -> FactOut:
    art = b.arts[BOARD]
    return (exact_text_quote(fact, value, art, title)
            or structure_quote(fact, value, art, title))


def _careers_jobs(b: Bundle) -> list[dict]:
    art = b.arts[CAREERS]
    out: dict[str, dict] = {}
    for a in (art.structure or {}).get("links") or []:
        url = a.get("url")
        if not url or a.get("region") in ("nav", "footer"):
            continue
        host, path = host_path(url)
        segs = [s.lower() for s in path.split("/") if s]
        ats = any(host_matches(host, h) for h in ATS_HOSTS) and len(segs) >= 2
        own = bool(b.domain) and host_matches(host, b.domain or "") and len(segs) >= 2 \
            and segs[-2] in _JOB_DIRS
        if ats or own:
            out.setdefault(url.split("#", 1)[0].rstrip("/"), a)
    return list(out.values())


def open_roles_total(b: Bundle) -> list[FactOut]:
    st = b.state(BOARD)
    if st == "ok":
        titles = _titles(b)
        n = int((b.arts[BOARD].structure or {}).get("job_count") or 0)
        if n == 0 or not titles:
            return [absent("open_roles_total", 0, BOARD, "job")] if n == 0 else []
        return [_title_quote("open_roles_total", n, b, titles[0])]
    if st == "not_found" and b.state(CAREERS) == "ok":
        jobs = _careers_jobs(b)
        if jobs:
            a = jobs[0]
            return [best_quote("open_roles_total", len(jobs), b.arts[CAREERS],
                               a.get("text") or a["href"])]
    return []


def families(b: Bundle) -> list[tuple[str, str]]:
    """(title, family or 'unclassified') for every board title."""
    return [(t, b.classify(t) or "unclassified") for t in _titles(b)]


def gtm_roles_by_family(b: Bundle) -> list[FactOut]:
    if b.state(BOARD) != "ok":
        return []
    fams = families(b)
    counts = Counter(f for _, f in fams)
    out = []
    for fam in sorted(counts):
        first = next(t for t, f in fams if f == fam)
        out.append(_title_quote("gtm_roles_by_family", {"family": fam, "count": counts[fam]},
                                b, first))
    return out


def first_gtm_hire(b: Bundle) -> list[FactOut]:
    if b.state(POST) != "ok":
        return []
    post = b.arts[POST]
    text = post.text or ""
    m = FOUNDING.search(text)
    if m is None:
        return [absent("first_gtm_hire", False, POST, "founding or first-hire wording")]
    if b.state(BOARD) != "ok":
        return []
    lines = {_title_key(x) for x in text.split("\n")}
    own = _title_key(b.signal_title) if b.signal_title else None
    others = []
    unsure = False
    for t, fam in families(b):
        key = _title_key(t)
        if (key == own) if own is not None else (key in lines):
            continue  # the post itself (the same title in several places counts once)
        if fam in SALES_FAMILIES:
            others.append(t)
        elif fam == "unclassified":
            unsure = True
    if others:
        return [_title_quote("first_gtm_hire", False, b, others[0])]
    if unsure:
        return []
    return [text_quote("first_gtm_hire", True, post, m.start(), m.end())]


EXTRACTORS = (
    ("open_roles_total", open_roles_total, (BOARD, CAREERS)),
    ("gtm_roles_by_family", gtm_roles_by_family, (BOARD,)),
    ("first_gtm_hire", first_gtm_hire, (POST, BOARD)),
)

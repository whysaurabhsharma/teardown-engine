"""icp_accounts: up to ten accounts that fit the company's ICP, each with a fit
quote and a dated reason to act now (docs/PLAN.md Step 60; protocol/GAPS.md
G1, G3 and G4).

ICP descriptor (no network, no model)
-------------------------------------
Industry, size band, region and the function the company sells to, each read
by a fixed word list from the company's own pages (customers page first, then
product page, then homepage; the function from product page, then homepage)
and each backed by a verbatim quote. Region comes from the customer_regions
facts; for G4 it is the job post's target region (jd_target_market) that the
customers page does not name, and candidates are restricted to it. With
neither an industry nor a size band there is no descriptor and the slice fails.

Candidates
----------
At most SEARCHES web searches (search_api through Guard): Exa's similar-page
search on two of the company's customers (customer sites linked from its
customers page), then one keyword search from the descriptor. Up to
MAX_CANDIDATES distinct sites, excluding the company, its existing customers,
its competitors (named on its compare page or chosen for funnel_compare) and
directory, review and social sites. Search only finds a site; the candidate's
own pages, fetched through Guard, are the evidence.

A candidate is listed only when it clears both tests, on its own site:
- fit: its homepage text matches the descriptor's industry (or, with no
  industry, its size band), quoted; for G4 it must also name the region.
- reason to act now: a dated public fact, either a homepage line that pairs a
  launch, expansion or funding word with a date in the last REASON_DAYS days,
  or an open role in the function the company sells to, posted in the last
  ROLE_DAYS days, read from the candidate's ATS board JSON (named on its
  homepage or its careers page) with the board's own date.
No reason, no row. Fewer than MIN_ROWS rows fails the slice (role_funnel is
then the fallback).

Budget per slice: SEARCHES searches, FETCH_CEILING Guard requests (robots.txt
and redirects included); candidates are taken in order until ten rows are
found or the ceiling is reached. No model calls (the plan allows one; none is
needed).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from engine.build.slices import common as C
from engine.build.slices import store as S
from engine.build.slices.funnel_compare import NOT_COMPETITORS, _slug
from engine.capture import bundle as BUNDLE
from engine.capture.discover import discover, on_booking_host
from engine.extract.base import Art, Bundle, Lex, exact_text_quote, lexicons, text_quote
from engine.guard.budget import Budget, BudgetExceeded
from engine.resolve.domain import registrable_domain
from engine.signal.boards import COLLECTORS
from engine.signal.careers import find_boards

KIND = "icp_accounts"
VERSION = "sl-icp-1.0"
MIN_ROWS = 6
MAX_ROWS = 10
MAX_CANDIDATES = 30
SEARCHES = 3
SIMILAR_SEEDS = 2
RESULTS_PER_SEARCH = 10
FETCH_CEILING = 45
REASON_DAYS = 180
ROLE_DAYS = 60
COLUMNS = ["Account", "Fit (their homepage)", "Reason to act now", "Dated"]

_I = re.IGNORECASE
INDUSTRIES: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (i, label, re.compile(rx, _I)) for i, label, rx in (
        ("fintech", "fintech", r"\bfintech\b|\bpayments?\b|\blending\b|\bNBFCs?\b"),
        ("banking_insurance", "banking and insurance",
         r"\bbanks?\b|\bbanking\b|\binsurance\b|\binsurers?\b"),
        ("healthcare", "healthcare",
         r"\bhealthcare\b|\bhospitals?\b|\bclinics?\b|\bpharma(?:ceutical)?s?\b"),
        ("ecommerce_retail", "e-commerce and retail",
         r"\be-?commerce\b|\bD2C\b|\bretail(?:ers?)?\b|\bonline stores?\b"),
        ("logistics", "logistics",
         r"\blogistics\b|\bsupply chain\b|\bfreight\b|\bwarehous(?:e|es|ing)\b"),
        ("manufacturing", "manufacturing", r"\bmanufactur(?:ing|ers?)\b|\bfactor(?:y|ies)\b"),
        ("education", "education", r"\bedtech\b|\beducation\b|\buniversit(?:y|ies)\b"),
        ("real_estate", "real estate", r"\breal estate\b|\bproperty developers?\b"),
        ("hospitality", "hospitality", r"\bhospitality\b|\bhotels?\b|\brestaurants?\b"),
        ("staffing_hr", "recruiting and HR",
         r"\brecruit(?:ing|ment|ers)\b|\bstaffing\b|\bHR teams?\b"),
        ("saas", "software", r"\bSaaS\b|\bsoftware companies\b|\bB2B software\b"),
    ))
SIZES: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (i, label, re.compile(rx, _I)) for i, label, rx in (
        ("smb", "small and mid-sized businesses",
         r"\bsmall (?:and medium[- ]sized )?business(?:es)?\b|\bSMBs?\b|\bSMEs?\b|\bstartups?\b"),
        ("mid_market", "mid-market", r"\bmid[- ]market\b|\bmid[- ]sized? (?:companies|firms)\b"),
        ("enterprise", "enterprise",
         r"\benterprises?\b|\bFortune 500\b|\blarge (?:companies|organi[sz]ations)\b"),
    ))
# (id, label, on the company's pages, in a job title)
FUNCTIONS: tuple[tuple[str, str, re.Pattern[str], re.Pattern[str]], ...] = tuple(
    (i, label, re.compile(page, _I), re.compile(title, _I)) for i, label, page, title in (
        ("sales", "sales", r"\bsales teams?\b|\bSDRs?\b|\bsales reps?\b",
         r"\bsales\b|\baccount executive\b|\bSDR\b|\bBDR\b|\bbusiness development\b"),
        ("marketing", "marketing", r"\bmarketing teams?\b|\bmarketers\b",
         r"\bmarketing\b|\bgrowth\b|\bdemand gen"),
        ("finance", "finance", r"\bfinance teams?\b|\bCFOs?\b|\baccounting teams?\b",
         r"\bfinance\b|\baccount(?:ing|ant)\b|\bcontroller\b|\bCFO\b"),
        ("hr", "HR and recruiting", r"\bHR teams?\b|\brecruiters\b|\btalent teams?\b",
         r"\bHR\b|\bpeople\b|\btalent\b|\brecruit"),
        ("support", "customer support", r"\bsupport teams?\b|\bcustomer service teams?\b",
         r"\bsupport\b|\bcustomer success\b|\bcustomer service\b"),
        ("operations", "operations", r"\boperations teams?\b|\bops teams?\b",
         r"\boperations\b|\bops\b"),
        ("engineering", "engineering", r"\bengineering teams?\b|\bdevelopers\b",
         r"\bengineer\b|\bdeveloper\b"),
    ))

REASON_WORDS = re.compile(
    r"\blaunch(?:ed|es|ing)?\b|\bintroduc(?:es|ed|ing)\b|\bnow available\b"
    r"|\bexpan(?:ds|ded|ding|sion)\b|\bopen(?:s|ed)? (?:an? )?(?:new )?office\b"
    r"|\braise[ds]?\b|\bfunding\b|\bseries [a-e]\b|\bseed round\b", _I)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
DATE_RX = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+(?:(\d{1,2}),?\s+)?"
    r"(20\d\d)\b|\b(20\d\d)-(\d\d)-(\d\d)\b", _I)


def title(name: str) -> str:
    return f"Accounts that fit {name}'s customers, each with a dated reason to act now"


# -- descriptor --------------------------------------------------------------------------


@dataclass
class Field:
    id: str
    label: str
    src: C.Src
    rx: Any  # re.Pattern or base.Lex: what a candidate's homepage must match


@dataclass
class Descriptor:
    industry: Field | None = None
    size: Field | None = None
    region: Field | None = None
    function: Field | None = None
    function_title: re.Pattern[str] | None = None
    region_required: bool = False

    def as_meta(self) -> dict[str, Any]:
        return {k: ({"id": f.id, "label": f.label, "kind": f.src.kind, "url": f.src.url,
                     "quote": f.src.quote} if f else None)
                for k, f in (("industry", self.industry), ("size", self.size),
                             ("region", self.region), ("function", self.function))} | \
            {"region_required": self.region_required}


def _company_src(b: Bundle, art: Art, start: int, end: int) -> C.Src:
    f = text_quote("icp", None, art, start, end)
    return C.Src("company", (b.domain or "").lower(), art.kind, art.url, f.quote, "text",
                 f.char_start, True)


def _first_match(b: Bundle, kinds: tuple[str, ...], table) -> Field | None:
    for kind in kinds:
        art = b.arts.get(kind)
        if art is None or not art.ok:
            continue
        for entry in table:
            i, label, rx = entry[0], entry[1], entry[2]
            m = rx.search(art.text or "")
            if m:
                return Field(i, label, _company_src(b, art, m.start(), m.end()), rx)
    return None


def _region_field(conn: sqlite3.Connection, b: Bundle, gap: str) -> tuple[Field | None, str | None]:
    regions = {e.id: e for e in lexicons()["regions"]}
    rows = conn.execute("SELECT fact, value, quote, char_start, artifact_kind FROM facts WHERE "
                        "company_id = ? AND fact IN ('customer_regions', 'jd_target_market') "
                        "AND found = 1 ORDER BY fact_id", (b.company_id,)).fetchall()
    cust = [r for r in rows if r["fact"] == "customer_regions"]
    post = [r for r in rows if r["fact"] == "jd_target_market"]

    def field_of(r: sqlite3.Row) -> Field | None:
        art = b.arts.get(r["artifact_kind"])
        lex: Lex | None = regions.get(r["value"])
        if art is None or lex is None:
            return None
        src = C.Src("company", (b.domain or "").lower(), art.kind, art.url, r["quote"], "text",
                    r["char_start"], True)
        return Field(r["value"], r["value"].replace("_", " "), src, lex)

    if gap == "G4":
        seen = {r["value"] for r in cust}
        target = next((r for r in post if r["value"] not in seen), None)
        if target is None:
            return None, "G4: the job post names no region the customers page lacks"
        return field_of(target), None
    return (field_of(cust[0]) if cust else None), None


def descriptor(conn: sqlite3.Connection, b: Bundle, gap: str) -> tuple[Descriptor, str | None]:
    d = Descriptor()
    d.industry = _first_match(b, ("customers", "product", "homepage"), INDUSTRIES)
    d.size = _first_match(b, ("customers", "product", "homepage"), SIZES)
    d.function = _first_match(b, ("product", "homepage"), FUNCTIONS)
    if d.function is not None:
        d.function_title = next(t for i, _, _, t in FUNCTIONS if i == d.function.id)
    d.region, why = _region_field(conn, b, gap)
    d.region_required = gap == "G4"

    def checked(f: Field | None) -> Field | None:
        return f if f is not None and C.check_src(f.src, lambda s: b.arts.get(s.kind)) is None             else None

    d.industry, d.size, d.function = checked(d.industry), checked(d.size), checked(d.function)
    if d.function is None:
        d.function_title = None
    if d.region is not None and checked(d.region) is None:
        d.region = None
        if d.region_required:
            return d, "G4: the job post's region quote does not check against the post"
    if why:
        return d, why
    if d.industry is None and d.size is None:
        return d, "no ICP descriptor: no industry or size band on the customers, product or home page"
    return d, None


# -- candidates --------------------------------------------------------------------------


def _usable(domain: str | None) -> bool:
    return bool(domain) and domain not in NOT_COMPETITORS \
        and not on_booking_host(f"https://{domain}/")


def customer_domains(b: Bundle) -> list[str]:
    own = registrable_domain(f"https://{b.domain}/") if b.domain else None
    art = b.arts.get("customers")
    out: list[str] = []
    if art is None or not art.ok:
        return out
    for link in (art.structure or {}).get("links") or []:
        url = link.get("url") or ""
        if link.get("region") in ("nav", "footer") or not url.startswith(("http://", "https://")):
            continue
        d = registrable_domain(url)
        if d and d != own and _usable(d) and d not in out:
            out.append(d)
    return out


def excluded(conn: sqlite3.Connection, b: Bundle, customers: list[str]) -> tuple[set[str], set[str]]:
    """(domains, name slugs) never listed: the company, its customers, its competitors."""
    own = registrable_domain(f"https://{b.domain}/") if b.domain else None
    domains = {d for d in (own, *customers) if d}
    slugs: set[str] = set()
    for c in S.competitors(conn, b.company_id):
        if c.domain:
            domains.add(c.domain)
        slugs.add(_slug(c.name))
    for r in conn.execute("SELECT value FROM facts WHERE company_id = ? AND "
                          "fact = 'named_competitors' AND found = 1", (b.company_id,)):
        if r["value"] and r["value"] != "none":
            slugs.add(_slug(r["value"]))
    return domains, {s for s in slugs if s}


def keyword_query(d: Descriptor) -> str:
    parts = [d.industry.label if d.industry else None, d.size.label if d.size else None,
             "companies"]
    if d.region is not None:
        parts.append(f"in {d.region.label}")
    return " ".join(p for p in parts if p)


@dataclass
class Found:
    candidates: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    searches: int = 0


async def find_candidates(searcher: C.Searcher, d: Descriptor, customers: list[str],
                          skip_domains: set[str], skip_slugs: set[str]) -> Found:
    from engine.guard import search_api

    out = Found()
    seen: set[str] = set()

    def add(urls: list[str]) -> None:
        for u in urls:
            dom = registrable_domain(u)
            if not _usable(dom) or dom in skip_domains or dom in seen:
                continue
            assert dom is not None
            if dom.split(".")[0] in skip_slugs or _slug(dom.split(".")[0]) in skip_slugs:
                continue
            seen.add(dom)
            if len(out.candidates) < MAX_CANDIDATES:
                out.candidates.append(dom)

    for cust in customers[:SIMILAR_SEEDS]:
        q = f"similar:https://{cust}/"
        try:
            add(await searcher.similar(f"https://{cust}/", num_results=RESULTS_PER_SEARCH))
            out.queries.append(q)
            out.searches += 1
        except search_api.Unsupported:
            out.notes.append("similar-page search not supported by the provider")
            break
        except (search_api.SearchError, BudgetExceeded) as e:
            out.notes.append(f"{q}: {e}")
    q = keyword_query(d)
    try:
        add(await searcher.query(q, num_results=RESULTS_PER_SEARCH))
        out.queries.append(q)
        out.searches += 1
    except (search_api.SearchError, BudgetExceeded) as e:
        out.notes.append(f"{q}: {e}")
    return out


# -- the two tests -----------------------------------------------------------------------


def _slice_src(art: Art, subject: str, start: int, end: int) -> C.Src:
    f = text_quote("icp", None, art, start, end)
    return C.Src("slice", subject, art.kind, art.url, f.quote, "text", f.char_start, True)


def fit(d: Descriptor, art: Art, subject: str) -> tuple[C.Cell, C.Src] | None:
    text = art.text or ""
    if d.region_required:
        assert d.region is not None
        if d.region.rx.first(text) is None:
            return None
    f = d.industry or d.size
    assert f is not None
    m = f.rx.search(text)
    if m is None:
        return None
    s = _slice_src(art, subject, m.start(), m.end())
    return C.cell(f.label, s), s


def parse_date(m: re.Match[str]) -> date | None:
    try:
        if m.group(4):
            return date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        month = _MONTHS[m.group(1)[:3].lower()]
        return date(int(m.group(3)), month, int(m.group(2) or 1))
    except (ValueError, KeyError):
        return None


def dated_line(art: Art, subject: str, today: date) -> tuple[C.Cell, C.Cell] | None:
    """A homepage line pairing a launch, expansion or funding word with a recent date."""
    text = art.text or ""
    pos = 0
    for line in text.split("\n"):
        start, pos = pos, pos + len(line) + 1
        w = REASON_WORDS.search(line)
        dm = DATE_RX.search(line)
        if not w or not dm:
            continue
        when = parse_date(dm)
        if when is None or not today - timedelta(days=REASON_DAYS) <= when <= today:
            continue
        word = w.group(0).lower()
        what = ("funding" if re.match(r"rais|fund|series|seed", word) else
                "expansion" if re.match(r"expan|open", word) else "launch")
        s = _slice_src(art, subject, start + w.start(), start + w.end())
        return C.cell(f"{what}: {C.shorten(s).quote}", s), C.cell(when.isoformat())
    return None


async def open_role(rt: C.Runtime, guard, company_id: str, subject: str, home_body: bytes,
                    home: C.SliceArtifact, d: Descriptor,
                    conn: sqlite3.Connection) -> tuple[C.Cell, C.Cell, dict[str, Art]] | None:
    """An open role in the function the company sells to, from the candidate's
    own ATS board, posted in the last ROLE_DAYS days."""
    if d.function_title is None:
        return None
    html = home_body.decode("utf-8", errors="replace")
    boards = find_boards(html)
    arts: dict[str, Art] = {}
    if not boards:
        found = discover(html, home.url, registrable_domain(home.url) or subject, None)
        link = found.get("careers")
        if link is None:
            return None
        car, body = await C.fetch_page(guard, rt.cache, company_id=company_id, slice_kind=KIND,
                                       subject=subject, kind="careers", url=link.url)
        C.write_artifact(conn, car)
        arts["careers"] = C.to_art(rt.cache, car)
        if body is None:
            return None
        boards = find_boards(body.decode("utf-8", errors="replace"))
        if not boards:
            return None
    source, slug = boards[0]
    mod = COLLECTORS[source]
    board, body = await C.fetch_page(guard, rt.cache, company_id=company_id, slice_kind=KIND,
                                     subject=subject, kind="ats_board", url=mod.board_url(slug))
    C.write_artifact(conn, board)
    art = C.to_art(rt.cache, board)
    arts["ats_board"] = art
    if body is None or not art.ok:
        return None
    try:
        jobs, ctx = mod.jobs_of(json.loads(body))
    except (ValueError, TypeError):  # not JSON, or a board in another shape: no evidence
        return None
    floor = rt.today - timedelta(days=ROLE_DAYS)
    for job in jobs:
        if not isinstance(job, dict):
            continue
        try:
            sig = mod.to_signal(slug, job, ctx, rt.today)
        except (KeyError, TypeError, ValueError):
            continue
        if sig.posted_at is None or not floor <= sig.posted_at <= rt.today:
            continue
        if not d.function_title.search(sig.title):
            continue
        q = exact_text_quote("icp", None, art, sig.title)
        if q is None:
            continue
        s = C.Src("slice", subject, "ats_board", art.url, q.quote, "text", q.char_start, True)
        return (C.cell(f"open role: {sig.title}", s), C.cell(sig.posted_at.isoformat()), arts)
    return None


# -- build -------------------------------------------------------------------------------


def inputs(d: Descriptor, gap: str, customers: list[str], skip: set[str]) -> dict[str, Any]:
    return {"gap": gap, "descriptor": d.as_meta(), "customers": customers,
            "excluded": sorted(skip)}


async def build(conn: sqlite3.Connection, rt: C.Runtime, b: Bundle, name: str,
                gap: str) -> C.Slice:
    from engine.guard import search_api

    t = title(name)
    try:
        searcher = rt.make_searcher(Budget(max_fetches=SEARCHES))
    except search_api.NotConfigured:
        return C.failed(KIND, VERSION, t, COLUMNS, "search not configured")
    d, why = descriptor(conn, b, gap)
    if why:
        return C.failed(KIND, VERSION, t, COLUMNS, why, meta={"descriptor": d.as_meta()})
    customers = customer_domains(b)
    skip_domains, skip_slugs = excluded(conn, b, customers)
    key = C.key_of(inputs(d, gap, customers, skip_domains | skip_slugs))
    found = await find_candidates(searcher, d, customers, skip_domains, skip_slugs)
    meta: dict[str, Any] = {"descriptor": d.as_meta(), "queries": found.queries,
                            "candidates": len(found.candidates), "notes": list(found.notes),
                            "skipped": {}}

    budget = Budget(max_fetches=FETCH_CEILING)
    guard = rt.make_guard(budget)
    drafts: list[C.Draft] = []
    arts: dict[str, dict[str, Art]] = {}
    company_arts = b.arts
    try:
        for dom in found.candidates:
            if len(drafts) >= MAX_ROWS:
                break
            home, body = await C.fetch_page(guard, rt.cache, company_id=b.company_id,
                                            slice_kind=KIND, subject=dom, kind="homepage",
                                            url=BUNDLE.homepage_url(dom))
            C.write_artifact(conn, home)
            art = C.to_art(rt.cache, home)
            arts[dom] = {"homepage": art}
            if not art.ok or body is None:
                meta["skipped"][dom] = f"homepage {home.outcome}"
                continue
            f = fit(d, art, dom)
            if f is None:
                meta["skipped"][dom] = "no fit quote on the homepage"
                continue
            fit_cell, fit_src = f
            reason = dated_line(art, dom, rt.today)
            if reason is None:
                got = await open_role(rt, guard, b.company_id, dom, body, home, d, conn)
                if got is not None:
                    reason = (got[0], got[1])
                    arts[dom].update(got[2])
            if reason is None:
                meta["skipped"][dom] = "no dated reason to act now on its own site"
                continue
            drafts.append(C.Draft([C.cell(dom), fit_cell, reason[0], reason[1]], fit_src))
    except BudgetExceeded as e:
        meta["notes"].append(f"stopped: {e}")
    finally:
        await guard.aclose()

    def resolve(s: C.Src) -> Art | None:
        if s.scope == "company":
            return company_arts.get(s.kind)
        return arts.get(s.subject, {}).get(s.kind)

    return C.finalize(KIND, VERSION, t, COLUMNS, drafts, resolve, min_rows=MIN_ROWS, meta=meta,
                      inputs_key=key, fetches=budget.fetches, searches=found.searches)

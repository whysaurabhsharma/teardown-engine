"""funnel_compare: the company's pricing page and demo path beside two
competitors' (docs/PLAN.md Step 61; protocol/GAPS.md G2 and G5).

Competitors
-----------
From the company's own named_competitors facts first (the names its compare
page sets itself against); a name's domain is the off-site link on that page
whose text holds the name, or whose registrable domain's first label is the
name. Only when there are no named competitors, one "{company} alternatives"
web search (search_api; NotConfigured fails the slice, which falls back to
role_funnel). Directory, review and social sites are never taken as a
competitor. A person can add or correct one with `te slice competitors --set
NAME=DOMAIN`.

Nothing about a competitor is fetched until a person has said yes to both in
`te slice competitors <company_id>`: the choice is stored unconfirmed, and the
build waits (Pending) rather than failing.

Capture and extract
-------------------
After the yes: each competitor's homepage through Guard, its pricing page and
the page behind its primary CTA found by the same discovery rules as the
company's bundle (engine/capture/discover.py: nav, footer, hero CTA; nothing
guessed, no sitemap fetch), each fetched through Guard and stored in
slice_artifacts (kept apart from the company's own artifacts). Then the SAME
four extractor functions (pricing_model, primary_cta, demo_form_fields,
has_booking_step) run on all three bundles: the company's own and the two
competitors'. The table reports what the pages show; it makes no claim about
conversion.

Budget per slice: FETCH_CEILING Guard requests (robots.txt and redirect hops
included; 2 competitors x 3 pages is 6 page fetches), SEARCH_CEILING searches.
No model calls.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from engine.build.slices import common as C
from engine.build.slices import store as S
from engine.capture import bundle as BUNDLE
from engine.capture.discover import discover, on_booking_host
from engine.extract import cta, forms, pricing
from engine.extract.base import Art, Bundle, FactOut
from engine.guard.budget import Budget, BudgetExceeded
from engine.resolve.domain import registrable_domain

KIND = "funnel_compare"
VERSION = "sl-funnel-1.0"
MIN_ROWS = 3  # the company and both competitors
FETCH_CEILING = 12
SEARCH_CEILING = 1
SEARCH_RESULTS = 10
COLUMNS = ["Company", "Pricing page", "Primary call to action", "Demo form", "Booking step"]
PAGES = ("homepage", "pricing", "demo_form")

# The same functions the company's facts came from (engine/extract), in column order.
EXTRACTORS: tuple[tuple[str, Any], ...] = (
    ("pricing_model", pricing.pricing_model),
    ("primary_cta", cta.primary_cta),
    ("demo_form_fields", forms.demo_form_fields),
    ("has_booking_step", forms.has_booking_step),
)

# Never a competitor: directories, review sites, social and publishing hosts.
NOT_COMPETITORS = frozenset({
    "g2.com", "capterra.com", "getapp.com", "softwareadvice.com", "trustradius.com",
    "sourceforge.net", "alternativeto.net", "producthunt.com", "saasworthy.com",
    "crozdesk.com", "slashdot.org", "gartner.com", "techradar.com", "forbes.com",
    "reddit.com", "quora.com", "youtube.com", "medium.com", "linkedin.com", "twitter.com",
    "x.com", "facebook.com", "instagram.com", "wikipedia.org", "github.com", "naukri.com",
    "crunchbase.com", "tracxn.com", "zoominfo.com", "glassdoor.com", "google.com",
    "apple.com", "substack.com", "wordpress.com", "blogspot.com",
})

PRICING_LABELS = {
    "numbers": "lists prices", "free_or_trial": "free plan or trial",
    "contact_sales": "contact sales", "unclassified": "found; no price wording",
}


def title(name: str) -> str:
    return f"{name}'s pricing page and demo path beside two competitors"


@dataclass
class Pending:
    reason: str


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def _usable(domain: str | None, own: str | None) -> bool:
    if not domain or domain == own:
        return False
    return domain not in NOT_COMPETITORS and not on_booking_host(f"https://{domain}/")


def domain_for(name: str, compare: Art | None, own: str | None) -> str | None:
    """The off-site link on the compare page that names this competitor."""
    if compare is None or not compare.ok:
        return None
    slug = _slug(name)
    for link in (compare.structure or {}).get("links") or []:
        url, text = link.get("url") or "", link.get("text") or ""
        d = registrable_domain(url) if url.startswith(("http://", "https://")) else None
        if not _usable(d, own):
            continue
        assert d is not None
        if (slug and slug in _slug(text)) or d.split(".")[0] == slug:
            return d
    return None


def named(conn: sqlite3.Connection, b: Bundle) -> list[S.Competitor]:
    own = registrable_domain(f"https://{b.domain}/") if b.domain else None
    compare = b.arts.get("compare")
    out: list[S.Competitor] = []
    for r in conn.execute("SELECT value, quote FROM facts WHERE company_id = ? AND "
                          "fact = 'named_competitors' AND found = 1 ORDER BY fact_id",
                          (b.company_id,)):
        name = r["value"]
        if not name or name == "none" or any(c.name.casefold() == name.casefold() for c in out):
            continue
        out.append(S.Competitor(name, domain_for(name, compare, own), "named_competitors",
                                compare.url if compare else None, r["quote"], len(out) + 1))
    return out


async def search_alternatives(rt: C.Runtime, b: Bundle, name: str) -> tuple[list[S.Competitor],
                                                                            str]:
    """One "alternatives" search. ([], why) when it cannot run."""
    from engine.guard import search_api

    try:
        searcher = rt.make_searcher(Budget(max_fetches=SEARCH_CEILING))
    except search_api.NotConfigured:
        return [], "search not configured"
    q = f"{name} alternatives"
    try:
        urls = await searcher.query(q, num_results=SEARCH_RESULTS)
    except (search_api.SearchError, BudgetExceeded) as e:
        return [], f"search failed: {e}"
    own = registrable_domain(f"https://{b.domain}/") if b.domain else None
    out: list[S.Competitor] = []
    for u in urls:
        d = registrable_domain(u)
        if not _usable(d, own) or any(c.domain == d for c in out):
            continue
        assert d is not None
        label = d.split(".")[0]
        out.append(S.Competitor(label[:1].upper() + label[1:], d, "alternatives_search", u,
                                None, len(out) + 1))
        if len(out) == 2:
            break
    return out, f"search {q!r}: {len(out)} usable result(s)"


async def choose(conn: sqlite3.Connection, rt: C.Runtime, b: Bundle,
                 name: str) -> tuple[list[S.Competitor], str]:
    """Pick (up to) two and store them unconfirmed. Named first; a search only
    when the company names none."""
    comps = named(conn, b)
    if comps:
        note = f"{len(comps)} named on the compare page"
        comps = comps[:2]
    else:
        comps, note = await search_alternatives(rt, b, name)
    if comps:
        S.write_competitors(conn, b.company_id, comps)
    return comps, note


def ready(comps: list[S.Competitor]) -> tuple[list[S.Competitor], str | None]:
    """The two to compare, or why not yet."""
    with_domain = [c for c in comps if c.domain]
    if len(with_domain) < 2:
        missing = [c.name for c in comps if not c.domain]
        why = f"{len(with_domain)} competitor(s) with a domain; two are needed"
        if missing:
            why += f" (no domain for {', '.join(missing)}; te slice competitors --set NAME=DOMAIN)"
        return [], why
    return with_domain[:2], None


# -- capture -----------------------------------------------------------------------------


async def capture_competitor(rt: C.Runtime, guard, company_id: str,
                             domain: str) -> dict[str, C.SliceArtifact]:
    """Homepage, then the pricing and demo_form pages it links to."""
    home, body = await C.fetch_page(guard, rt.cache, company_id=company_id, slice_kind=KIND,
                                    subject=domain, kind="homepage",
                                    url=BUNDLE.homepage_url(domain))
    rows = {"homepage": home}
    readable = home.outcome == "ok" and body is not None
    found = discover(body.decode("utf-8", errors="replace"), home.url,
                     registrable_domain(home.url) or domain, None) if readable else {}
    for kind in ("pricing", "demo_form"):
        link = found.get(kind)
        if link is not None:
            a, _ = await C.fetch_page(guard, rt.cache, company_id=company_id, slice_kind=KIND,
                                      subject=domain, kind=kind, url=link.url)
            a.detail = f"found as {link.describe()}" + (f"; {a.detail}" if a.detail else "")
            rows[kind] = a
        else:
            where = "primary CTA" if kind == "demo_form" else "homepage nav or footer"
            rows[kind] = C.looked_for(company_id, KIND, domain, kind, home, readable, where)
    return rows


# -- the table ---------------------------------------------------------------------------


def run_extractors(b: Bundle) -> dict[str, list[FactOut]]:
    return {name: fn(b) for name, fn in EXTRACTORS}


def row_for(label: str, b: Bundle, got: dict[str, list[FactOut]], scope: str,
            subject: str) -> C.Draft:
    def src(f: FactOut) -> C.Src | None:
        art = b.arts.get(f.artifact_kind)
        return C.src_of(f, art, scope=scope, subject=subject) if art is not None else None

    cells: list[C.Cell] = [C.cell(label)]
    shown: list[C.Src] = []

    def one(name: str, text_of) -> None:
        fs = got.get(name) or []
        if not fs or (s := src(fs[0])) is None:
            cells.append(C.UNSEEN)
            return
        f = fs[0]
        if f.quote_source == "not_found":
            cells.append(C.not_found(s))
            return
        text = text_of(f)
        if text is None:
            cells.append(C.not_found(s))
            return
        cells.append(C.cell(text, s))
        if not s.absence:
            shown.append(s)

    one("pricing_model", lambda f: PRICING_LABELS.get(str(f.value), str(f.value)))
    one("primary_cta", lambda f: None if f.value == "none" else f"{f.quote} ({f.value})")
    one("demo_form_fields", lambda f: (f"{f.value['count']} fields" if isinstance(f.value, dict)
                                       else "no form fields in the page HTML"))
    one("has_booking_step", lambda f: "scheduling step found" if f.value is True else None)
    return C.Draft(cells, shown[0] if shown else None)


async def build(conn: sqlite3.Connection, rt: C.Runtime, b: Bundle,
                name: str) -> C.Slice | Pending:
    comps = S.competitors(conn, b.company_id)
    if not comps:
        comps, note = await choose(conn, rt, b, name)
        if not comps:
            return C.failed(KIND, VERSION, title(name), COLUMNS,
                            f"no competitors: none named on the site; {note}",
                            searches=0 if note == "search not configured" else 1)
        two, why = ready(comps)
        if why:
            return C.failed(KIND, VERSION, title(name), COLUMNS, why)
        return Pending(f"competitors chosen ({note}); confirm them with "
                       f"te slice competitors {b.company_id}")
    two, why = ready(comps)
    if why:
        return C.failed(KIND, VERSION, title(name), COLUMNS, why)
    if not all(c.confirmed for c in two):
        return Pending(f"waiting for your yes: te slice competitors {b.company_id}")

    inputs = [(c.name, c.domain) for c in two]
    budget = Budget(max_fetches=FETCH_CEILING)
    guard = rt.make_guard(budget)
    arts: dict[str, dict[str, Art]] = {}
    try:
        for c in two:
            assert c.domain is not None
            rows = await capture_competitor(rt, guard, b.company_id, c.domain)
            for a in rows.values():
                C.write_artifact(conn, a)
            arts[c.domain] = {k: C.to_art(rt.cache, a) for k, a in rows.items()}
    except BudgetExceeded as e:
        return C.failed(KIND, VERSION, title(name), COLUMNS, f"budget: {e}",
                        fetches=budget.fetches, inputs_key=C.key_of(inputs))
    finally:
        await guard.aclose()

    subject = (b.domain or "").lower()
    drafts = [row_for(name, b, run_extractors(b), "company", subject)]
    for c in two:
        assert c.domain is not None
        cb = Bundle(company_id=f"{b.company_id}/{c.domain}", domain=c.domain, name=c.name,
                    arts=arts[c.domain])
        drafts.append(row_for(c.name, cb, run_extractors(cb), "slice", c.domain))

    def resolve(s: C.Src) -> Art | None:
        if s.scope == "company":
            return b.arts.get(s.kind)
        return arts.get(s.subject, {}).get(s.kind)

    meta = {"competitors": [{"name": c.name, "domain": c.domain, "source": c.source}
                            for c in two],
            "extractors": [n for n, _ in EXTRACTORS]}
    return C.finalize(KIND, VERSION, title(name), COLUMNS, drafts, resolve, min_rows=MIN_ROWS,
                      meta=meta, inputs_key=C.key_of(inputs), fetches=budget.fetches)

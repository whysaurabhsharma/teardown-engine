"""Resolve each company's official domain (docs/PLAN.md Step 26).

Input: fresh GTM signals (fresh = 1, role_family set and not `not_gtm`) that
have no company_id yet. They are grouped into would-be companies:

- seed       a seed-list company (data/seeds.csv). Its signals are careers-page
             signals whose board_slug is the seed's domain, plus ATS signals from
             a board the seed's careers page hands off to. That hand-off is not
             stored by `te signal careers`, so it is re-derived here from the
             seed's careers_url (when it is itself a board URL) or from the
             careers page Guard cached on the last collect (any day; nothing is
             fetched for this). A board claimed by two seeds is not linked.
- ats        a Greenhouse, Lever, Ashby or Workable board, keyed source:slug.
             Manual signals whose URL is on a board join that board's group.
- careers    careers-page signals whose seed is no longer in the seed file;
             their board_slug is the site's domain.
- manual     other manual signals, keyed by the normalised company name.

Candidate domains, without a search API first: the seed's site; the board's
own metadata (Greenhouse /v1/boards/{slug} name and content links, the Lever
and Ashby board pages, the Workable account JSON), with explicit website fields
and the board page's logo link ahead of other links; URLs and email domains
written in the job descriptions; a manual signal's own URL. ATS, social and
other shared hosts are never candidates. Only when that yields nothing is the
web search hook (guard.search_api) asked; when it is not configured the
company is marked for review rather than guessed at. At most MAX_CANDIDATES
are verified, best name-like first.

Every domain is normalised with tldextract to its registrable domain, using
the suffix-list snapshot bundled with tldextract (no network, no disk cache).
`.example` (RFC 2606, reserved, never a real site) is added as a suffix so
fixtures can use invented companies.

Verification, two checks, all fetches through Guard:
(a) name     the homepage <title> or og:site_name fuzzy-matches the company
             name (rapidfuzz token_set_ratio after lower-casing, dropping
             punctuation and legal words such as Pvt, Ltd, Inc) at >= 85.
(b) linkback seed and careers groups: the careers_url sits on the same
             registrable domain. ATS groups: the homepage, or a careers page
             linked from the homepage (discovered, never guessed), links to the
             same board. Manual groups: the signal's own URL sits on the domain.
Both pass: status `resolved`. One or none: status stays `sourced` and
companies.review_reason says why ("<code>: <detail>"). Codes: name_mismatch,
no_linkback, unverified (neither), homepage_<outcome> (homepage not ok, so the
name could not be checked), homepage_redirect (homepage lands on another
domain), search_not_configured, no_candidate.

Writing: company_id is the registrable domain with dots as hyphens
(zentrico.example -> zentrico-example, the plan's acme.com -> acme-com). A
domain that already has a row gets the signals attached to it, never a second
row; a verified result moves a `sourced` row to `resolved` and clears its
review_reason. A group with no candidate domain gets a placeholder row
`unresolved-<key>` (domain NULL) so the review is explicit. Placeholders are
retried when a search provider is configured, or when named with --company;
if a domain is found, their signals move to the domain row and the empty
placeholder is deleted. Merging two different domains is Step 27, not here.

Errors (an unreadable board JSON, a failed search call) leave the group's
signals unattached so the next run tries again. GuardConfigError and
BudgetExceeded stop the run. Nothing here calls a model.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import tldextract
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from engine import db, states
from engine.guard import search_api
from engine.guard.budget import Budget, BudgetExceeded
from engine.guard.cache import Cache
from engine.guard.http import FetchResult, Guard, GuardConfigError
from engine.models import Company
from engine.signal import ats, careers, workable
from engine.signal.filter import NOT_GTM
from engine.signal.manual import company_slug
from engine.signal.seeds import Seed

MATCH_THRESHOLD = 85
MAX_CANDIDATES = 2
MAX_CAREERS_PAGES = 2  # careers pages fetched per candidate when looking for the linkback
PLACEHOLDER_PREFIX = "unresolved-"
ATS_SOURCES = ("greenhouse", "lever", "ashby", "workable")
KIND_META = "ats_meta"
KIND_HOME = "homepage"
KIND_CAREERS = "careers_page"

# The bundled public-suffix snapshot only: suffix_list_urls=() never fetches,
# cache_dir=None never writes.
_EXTRACT = tldextract.TLDExtract(
    suffix_list_urls=(), cache_dir=None, fallback_to_snapshot=True, extra_suffixes=("example",)
)

# Registrable domains that are never a company's own site.
EXCLUDED_DOMAINS = frozenset({
    # ATS and job boards
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com",
    "workday.com", "myworkdayjobs.com", "bamboohr.com", "recruitee.com", "breezy.hr",
    "keka.com", "zohorecruit.com", "zohorecruit.in", "freshteam.com", "darwinbox.in",
    "indeed.com", "glassdoor.com", "glassdoor.co.in", "naukri.com", "instahyre.com",
    "wellfound.com", "angel.co", "ycombinator.com", "cutshort.io", "iimjobs.com",
    # social and media
    "linkedin.com", "lnkd.in", "twitter.com", "x.com", "facebook.com", "fb.com",
    "instagram.com", "youtube.com", "youtu.be", "vimeo.com", "medium.com", "github.com",
    "t.me", "wa.me", "whatsapp.com", "tiktok.com", "reddit.com", "substack.com",
    # shared infrastructure, forms, scheduling, shorteners
    "google.com", "goo.gl", "googleapis.com", "gstatic.com", "bit.ly", "tinyurl.com",
    "cloudfront.net", "amazonaws.com", "cloudflare.com", "jsdelivr.net", "w3.org",
    "schema.org", "gravatar.com", "calendly.com", "typeform.com", "notion.site",
    "hubspot.com", "hsforms.com", "apple.com", "microsoft.com", "office.com",
    "gmail.com", "outlook.com", "yahoo.com", "hotmail.com", "example.com",
})

_LEGAL_WORDS = frozenset({
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "pvt", "private", "corp",
    "corporation", "co", "gmbh", "plc", "pte", "the",
})
_URL_RX = re.compile(r"""(?:https?://|\bwww\.)[^\s<>"'()\[\]{},]+""", re.IGNORECASE)
_EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
_WEBSITE_KEYS = frozenset({
    "website", "publicwebsite", "websiteurl", "website_url", "companywebsite",
    "company_website", "company_url", "companyurl", "homepage", "homepageurl",
})
_WEBSITE_RX = re.compile(
    r'"(?:website|publicWebsite|websiteUrl|website_url|companyWebsite|company_url|homepage)"'
    r'\s*:\s*"(https?:[^"]+)"', re.IGNORECASE)
_CAREERS_RX = re.compile(
    r"\b(?:careers?|jobs?|join\s+us|join\s+the\s+team|work\s+with\s+us|we'?re\s+hiring|"
    r"hiring|open\s+(?:roles|positions)|openings)\b", re.IGNORECASE)
_CAREERS_PATH_RX = re.compile(r"/(?:careers?|jobs|join-us|joinus|work-with-us|hiring)(?:/|$|\.)",
                              re.IGNORECASE)


# -- normalising -------------------------------------------------------------------


def registrable_domain(url_or_host: str | None) -> str | None:
    """The registrable domain of a URL or host ('https://www.a.co.in/x' -> 'a.co.in'),
    or None for an IP, a bare name or anything without a public suffix."""
    if not url_or_host or not url_or_host.strip():
        return None
    value = url_or_host.strip()
    host = urlsplit(value).hostname if "//" in value else value.split("/")[0].split(":")[0]
    if not host:
        return None
    r = _EXTRACT(host.lower().rstrip("."))
    if not r.suffix or not r.domain:
        return None
    return f"{r.domain}.{r.suffix}"


def company_id_for(domain: str) -> str:
    """'acme.com' -> 'acme-com' (docs/PLAN.md sample records)."""
    return re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")


def is_excluded(domain: str | None) -> bool:
    return domain is None or domain in EXCLUDED_DOMAINS


def normalise_name(name: str | None) -> str:
    """Lower case, punctuation to spaces, legal words dropped."""
    if not name:
        return ""
    words = re.sub(r"[^0-9a-z]+", " ", name.lower()).split()
    kept = [w for w in words if w not in _LEGAL_WORDS]
    return " ".join(kept or words)


def name_score(name: str | None, text: str | None) -> float:
    a, b = normalise_name(name), normalise_name(text)
    if not a or not b:
        return 0.0
    return float(fuzz.token_set_ratio(a, b))


def name_matches(name: str | None, text: str | None) -> bool:
    return name_score(name, text) >= MATCH_THRESHOLD


# -- reading pages -----------------------------------------------------------------


def page_names(markup: str) -> list[str]:
    """The homepage <title> and og:site_name, as written."""
    soup = BeautifulSoup(markup, "html.parser")
    out: list[str] = []
    if soup.title and soup.title.string and soup.title.string.strip():
        out.append(" ".join(soup.title.string.split()))
    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        content = meta.get("content")
        if prop == "og:site_name" and isinstance(content, str) and content.strip():
            out.append(" ".join(content.split()))
    return out


def _hrefs(markup: str, base: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(markup, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip() if isinstance(a["href"], str) else ""
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base, href)
        if urlsplit(url).scheme in ("http", "https"):
            out.append((url, " ".join(a.get_text(" ").split())))
    return out


def careers_links(markup: str, base: str) -> list[str]:
    """Links on a page that read as its careers page, in page order."""
    found: dict[str, None] = {}
    for url, text in _hrefs(markup, base):
        if _CAREERS_RX.search(text) or _CAREERS_PATH_RX.search(urlsplit(url).path):
            found.setdefault(url.split("#")[0], None)
    return list(found)


def text_domains(text: str | None) -> list[str]:
    """Registrable domains of URLs (http(s):// or www.) and email addresses in text."""
    if not text:
        return []
    out: list[str] = []
    for m in _URL_RX.finditer(text):
        url = m.group(0).rstrip(".,;:!?")
        d = registrable_domain(url if "//" in url else "http://" + url)
        if d:
            out.append(d)
    for m in _EMAIL_RX.finditer(text):
        d = registrable_domain(m.group(1))
        if d:
            out.append(d)
    return out


def _walk(node: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(node, dict):
        for k, v in node.items():
            yield str(k), v
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


@dataclass
class BoardMeta:
    name: str | None = None
    explicit: list[str] = field(default_factory=list)  # website fields, logo link
    links: list[str] = field(default_factory=list)  # every other domain seen, with repeats


def meta_url(source: str, slug: str) -> str:
    """The board page or JSON that describes the company behind a board."""
    s = ats.path_slug(slug)
    return {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{s}",
        "lever": f"https://jobs.lever.co/{s}",
        "ashby": f"https://jobs.ashbyhq.com/{s}",
        "workable": workable.board_url(slug),
    }[source]


def read_meta(body: str, base: str) -> BoardMeta:
    """Name and candidate domains from a board's metadata (JSON or HTML)."""
    meta = BoardMeta()
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if isinstance(payload, (dict, list)):
        if isinstance(payload, dict) and isinstance(payload.get("name"), str):
            meta.name = payload["name"].strip() or None
        for key, value in _walk(payload):
            if not isinstance(value, str):
                continue
            if key.lower() in _WEBSITE_KEYS and (d := registrable_domain(value)):
                meta.explicit.append(d)
            elif "<" in value:
                meta.links += [d for u, _ in _hrefs(html.unescape(value), base)
                               if (d := registrable_domain(u))]
                meta.links += text_domains(ats.html_to_text(value, escaped=True))
            elif key.lower() in ("description", "content", "about"):
                meta.links += text_domains(value)
        return meta
    meta.explicit += [d for u in _WEBSITE_RX.findall(body) if (d := registrable_domain(u))]
    soup = BeautifulSoup(body, "html.parser")
    for a in soup.select("a.main-header-logo[href], .main-header a[href]"):
        if (d := registrable_domain(urljoin(base, a["href"]))):
            meta.explicit.append(d)
    meta.links += [d for u, _ in _hrefs(body, base) if (d := registrable_domain(u))]
    return meta


def rank_candidates(name: str, explicit: list[str], links: list[str]) -> list[str]:
    """Explicit website domains first, in order; then other domains by how well
    their label matches the name, then by how often they appear."""
    out: dict[str, None] = {}
    for d in explicit:
        if not is_excluded(d):
            out.setdefault(d, None)
    counts = Counter(d for d in links if not is_excluded(d))
    order = {d: i for i, d in reversed(list(enumerate(links)))}
    rest = sorted(counts, key=lambda d: (-name_score(name, d.split(".")[0]), -counts[d], order[d]))
    for d in rest:
        out.setdefault(d, None)
    return list(out)


# -- groups ------------------------------------------------------------------------


@dataclass
class Group:
    key: str  # seed:<domain> | <source>:<slug> | careers:<domain> | manual:<name>
    kind: str  # seed | ats | careers | manual
    name: str
    signal_ids: list[str] = field(default_factory=list)
    seed: Seed | None = None
    board: tuple[str, str] | None = None  # (source, slug) for ats groups
    careers_url: str | None = None  # seed / careers groups: the page that lists roles
    urls: list[str] = field(default_factory=list)  # the signals' own URLs
    descriptions: list[str] = field(default_factory=list)
    retry_of: str | None = None  # placeholder company_id being retried

    @property
    def placeholder_id(self) -> str:
        return PLACEHOLDER_PREFIX + company_id_for(self.key.replace(":", "-"))

    @property
    def target_id(self) -> str | None:
        """The company_id this group will most likely get, if known before fetching."""
        if self.seed is not None and (d := registrable_domain(self.seed.site)):
            return company_id_for(d)
        if self.kind == "careers" and (d := registrable_domain(self.key.split(":", 1)[1])):
            return company_id_for(d)
        return None


def seed_boards(seeds: list[Seed], cache: Cache) -> dict[tuple[str, str], Seed]:
    """ATS boards each seed's careers page hands off to, re-derived without a fetch:
    from the careers_url itself, else from the careers page in Guard's cache.
    A board claimed by two seeds is left out."""
    claims: dict[tuple[str, str], list[Seed]] = {}
    for seed in seeds:
        found = careers.find_boards(seed.careers_url)
        if not found:
            try:
                entry = cache.latest(seed.careers_url.split("#")[0].strip())
            except ValueError:
                entry = None
            if entry is not None and entry.outcome == "ok" and entry.sha256:
                body = cache.get_body(entry.sha256)
                if body is not None:
                    found = careers.find_boards(
                        entry.final_url, body.decode("utf-8", errors="replace"))
        for source, slug in found[: careers.MAX_HANDOFFS]:
            claims.setdefault((source, slug.lower()), []).append(seed)
    return {k: v[0] for k, v in claims.items() if len({s.domain for s in v}) == 1}


def gtm_signals(conn: sqlite3.Connection, retry_ids: Iterable[str] = ()) -> list[sqlite3.Row]:
    """Fresh GTM signals with no company, plus those attached to the placeholder
    companies named in retry_ids."""
    retry = list(retry_ids)
    where = "s.company_id IS NULL"
    if retry:
        where = f"(s.company_id IS NULL OR s.company_id IN ({', '.join('?' for _ in retry)}))"
    return conn.execute(
        "SELECT s.signal_id, s.source, s.board_slug, s.company_name_raw, s.url, "
        "s.description_text, s.company_id FROM signals s "
        f"WHERE s.fresh = 1 AND s.role_family IS NOT NULL AND s.role_family != ? AND {where} "
        "ORDER BY s.signal_id",
        (NOT_GTM, *retry),
    ).fetchall()


def build_groups(rows: Iterable[sqlite3.Row], seeds: list[Seed], cache: Cache) -> list[Group]:
    by_domain = {s.domain: s for s in seeds}
    by_board = seed_boards(seeds, cache)
    groups: dict[str, Group] = {}
    for r in rows:
        source, slug = r["source"], (r["board_slug"] or "").strip()
        url = r["url"]
        board: tuple[str, str] | None = None
        if source in ATS_SOURCES and slug:
            board = (source, slug)
        elif source != "careers" and url and (found := careers.find_boards(url)):
            board = found[0]
        seed: Seed | None = None
        if source == "careers":
            seed = by_domain.get(slug.lower())
        elif board is not None:
            seed = by_board.get((board[0], board[1].lower()))

        if seed is not None:
            key, kind = f"seed:{seed.domain}", "seed"
        elif board is not None:
            key, kind = f"{board[0]}:{board[1].lower()}", "ats"
        elif source == "careers":
            key, kind = f"careers:{slug.lower()}", "careers"
        else:
            key, kind = f"manual:{company_slug(r['company_name_raw'])}", "manual"

        g = groups.get(key)
        if g is None:
            g = groups[key] = Group(
                key=key, kind=kind,
                name=seed.company if seed else r["company_name_raw"],
                seed=seed,
                board=board if kind == "ats" else None,
                careers_url=(seed.careers_url if seed else (url if kind == "careers" else None)),
            )
        if r["company_id"] and r["company_id"].startswith(PLACEHOLDER_PREFIX):
            g.retry_of = r["company_id"]
        g.signal_ids.append(r["signal_id"])
        if url:
            g.urls.append(url)
        if r["description_text"]:
            g.descriptions.append(r["description_text"])
    return sorted(groups.values(), key=lambda g: g.key)


# -- verification ------------------------------------------------------------------


@dataclass
class Check:
    domain: str
    name_ok: bool = False
    score: float = 0.0
    title: str | None = None  # the homepage title or site name that scored best
    linkback_ok: bool = False
    linkback: str = ""  # how the linkback passed or why it failed
    homepage_outcome: str = "ok"
    redirected_to: str | None = None

    @property
    def passes(self) -> int:
        return int(self.name_ok) + int(self.linkback_ok)

    @property
    def verified(self) -> bool:
        return self.name_ok and self.linkback_ok

    def reason(self) -> str:
        if self.verified:
            return ""
        if self.redirected_to:
            return (f"homepage_redirect: https://{self.domain}/ lands on "
                    f"{self.redirected_to}; {self.linkback}")
        if self.homepage_outcome != "ok":
            return (f"homepage_{self.homepage_outcome}: name not checked; "
                    f"linkback {'ok' if self.linkback_ok else 'failed'} ({self.linkback})")
        title = f"'{self.title}'" if self.title else "no title or og:site_name"
        if self.linkback_ok:
            return f"name_mismatch: {title} scored {self.score:.0f} < {MATCH_THRESHOLD}"
        if self.name_ok:
            return f"no_linkback: {self.linkback}"
        return (f"unverified: {title} scored {self.score:.0f} < {MATCH_THRESHOLD}; "
                f"{self.linkback}")


def _has_board(found: list[tuple[str, str]], board: tuple[str, str]) -> bool:
    return (board[0], board[1].lower()) in {(s, b.lower()) for s, b in found}


async def _linkback_to_board(
    board: tuple[str, str], home: FetchResult, domain: str, guard: Guard
) -> tuple[bool, str]:
    label = f"{board[0]}:{board[1]}"
    if _has_board(careers.find_boards(home.final_url, home.text), board):
        return True, f"homepage links to {label}"
    tried = 0
    for link in careers_links(home.text, home.final_url):
        if _has_board(careers.find_boards(link), board):
            return True, f"careers link {link} is {label}"
        if registrable_domain(link) != domain or tried >= MAX_CAREERS_PAGES:
            continue
        tried += 1
        page = await guard.fetch(link, KIND_CAREERS)
        if page.ok and _has_board(careers.find_boards(page.final_url, page.text), board):
            return True, f"careers page {link} links to {label}"
    if tried == 0:
        return False, f"no careers page linked from the homepage; {label} not found"
    return False, f"careers page does not link to {label}"


async def verify(
    domain: str,
    name: str,
    guard: Guard,
    *,
    homepage: str | None = None,
    board: tuple[str, str] | None = None,
    careers_url: str | None = None,
    on_site_urls: Iterable[str] = (),
) -> Check:
    """Check (a) the name on the homepage and (b) the linkback for one domain."""
    c = Check(domain=domain)
    home = await guard.fetch(homepage or f"https://{domain}/", KIND_HOME)
    c.homepage_outcome = home.outcome
    landed = registrable_domain(home.final_url)
    if home.ok and landed != domain:
        c.redirected_to = landed
    if home.ok and not c.redirected_to:
        scored = [(name_score(name, t), t) for t in page_names(home.text)]
        if scored:
            c.score, c.title = max(scored, key=lambda st: st[0])
        c.name_ok = c.score >= MATCH_THRESHOLD

    if careers_url is not None:
        cd = registrable_domain(careers_url)
        c.linkback_ok = cd == domain
        c.linkback = (f"careers_url is on {domain}" if c.linkback_ok
                      else f"careers_url is on {cd}, not {domain}")
    elif board is not None:
        if home.ok and not c.redirected_to:
            c.linkback_ok, c.linkback = await _linkback_to_board(board, home, domain, guard)
        else:
            c.linkback = f"homepage {home.outcome}; {board[0]}:{board[1]} linkback not checked"
    else:
        on_site = [u for u in on_site_urls if registrable_domain(u) == domain]
        c.linkback_ok = bool(on_site)
        c.linkback = (f"the role's own URL is on {domain}" if on_site
                      else "no ATS board to link back to and the role's URL is elsewhere")
    return c


# -- one group ---------------------------------------------------------------------


@dataclass
class Outcome:
    group: Group
    company_id: str | None = None
    domain: str | None = None
    name: str = ""
    verified: bool = False
    reason: str = ""  # "<code>: <detail>" when not verified
    error: str = ""
    candidates: list[str] = field(default_factory=list)
    attached: int = 0

    @property
    def code(self) -> str:
        return self.reason.split(":", 1)[0] if self.reason else ""


async def candidates_for(
    g: Group, guard: Guard, *, cache: Cache, budget: Budget | None
) -> tuple[str, list[str], str]:
    """(name to match, candidate domains, review reason if there are none)."""
    name = g.name
    if g.seed is not None:
        d = registrable_domain(g.seed.site)
        return name, [d] if d else [], "" if d else f"no_candidate: seed site {g.seed.site}"
    if g.kind == "careers":
        d = registrable_domain(g.key.split(":", 1)[1])
        return name, [d] if d else [], "" if d else "no_candidate: careers domain unreadable"

    explicit: list[str] = []
    links: list[str] = []
    if g.board is not None:
        url = meta_url(*g.board)
        r = await guard.fetch(url, KIND_META)
        if r.ok:
            meta = read_meta(r.text, r.final_url)
            if meta.name:
                name = meta.name
            explicit += meta.explicit
            links += meta.links
    for text in g.descriptions:
        links += text_domains(text)
    explicit += [d for u in g.urls if (d := registrable_domain(u)) and not is_excluded(d)]
    found = rank_candidates(name, explicit, links)
    if found:
        return name, found[:MAX_CANDIDATES], ""

    try:
        urls = await search_api.search_site(name, cache=cache, budget=budget)
    except search_api.NotConfigured:
        return name, [], ("search_not_configured: no candidate from the board or the job "
                          "posts, and no search provider is set (EXA_API_KEY, TAVILY_API_KEY)")
    found = rank_candidates(name, [], [d for u in urls if (d := registrable_domain(u))])
    if not found:
        return name, [], "no_candidate: neither the board, the job posts nor search named a site"
    return name, found[:MAX_CANDIDATES], ""


async def resolve_group(
    g: Group, guard: Guard, *, cache: Cache, budget: Budget | None = None
) -> Outcome:
    """Find and verify one group's domain. Writes nothing."""
    out = Outcome(group=g, name=g.name)
    try:
        name, cands, reason = await candidates_for(g, guard, cache=cache, budget=budget)
    except search_api.SearchError as e:
        out.error = f"search failed: {e}"
        return out
    out.name, out.candidates = name, cands
    if not cands:
        out.reason = reason
        return out
    board, careers_url = g.board, g.careers_url
    if g.seed is not None and (direct := careers.find_boards(g.seed.careers_url)):
        # The seed's careers_url is the board itself: check the site links to it.
        board, careers_url = direct[0], None
    best: Check | None = None
    for d in cands:
        c = await verify(
            d, name, guard,
            homepage=g.seed.site if g.seed is not None else None,
            board=board,
            careers_url=careers_url,
            on_site_urls=g.urls,
        )
        if best is None or c.passes > best.passes:
            best = c
        if c.verified:
            break
    assert best is not None
    out.domain = best.domain
    out.verified = best.verified
    out.reason = best.reason()
    return out


# -- writing -----------------------------------------------------------------------


class Collision(RuntimeError):
    pass


def _attach(conn: sqlite3.Connection, company_id: str, g: Group) -> int:
    marks = ", ".join("?" for _ in g.signal_ids)
    sql = f"UPDATE signals SET company_id = ? WHERE signal_id IN ({marks}) AND "
    params: list[object] = [company_id, *g.signal_ids]
    if g.retry_of:
        sql += "(company_id IS NULL OR company_id = ?)"
        params.append(g.retry_of)
    else:
        sql += "company_id IS NULL"
    return conn.execute(sql, params).rowcount


def write(conn: sqlite3.Connection, out: Outcome) -> None:
    """Create or reuse the company row and attach the group's signals."""
    g = out.group
    if out.domain is None:
        if g.retry_of:  # retried placeholder, still nothing: leave it as it is
            out.company_id = g.retry_of
            return
        cid = g.placeholder_id
        with conn:
            if conn.execute("SELECT 1 FROM companies WHERE company_id = ?", (cid,)).fetchone() is None:
                db.insert(conn, Company(company_id=cid, name=out.name, review_reason=out.reason))
            out.company_id = cid
            out.attached = _attach(conn, cid, g)
        return

    row = conn.execute("SELECT company_id, status, review_reason FROM companies WHERE domain = ?",
                       (out.domain,)).fetchone()
    if row is None:
        cid = company_id_for(out.domain)
        taken = conn.execute("SELECT domain FROM companies WHERE company_id = ?", (cid,)).fetchone()
        if taken is not None:
            raise Collision(f"company_id {cid} already belongs to {taken['domain']}")
        with conn:
            db.insert(conn, Company(company_id=cid, domain=out.domain, name=out.name,
                                    review_reason=out.reason or None))
    else:
        cid = row["company_id"]
    if out.verified:
        status = conn.execute("SELECT status FROM companies WHERE company_id = ?",
                              (cid,)).fetchone()["status"]
        if status == states.Status.SOURCED:
            with conn:
                conn.execute("UPDATE companies SET review_reason = NULL WHERE company_id = ?",
                             (cid,))
            states.advance(cid, states.Status.RESOLVED, conn)
    out.company_id = cid
    with conn:
        out.attached = _attach(conn, cid, g)
        if g.retry_of:
            left = conn.execute("SELECT 1 FROM signals WHERE company_id = ? LIMIT 1",
                                (g.retry_of,)).fetchone()
            if left is None:
                try:
                    conn.execute("DELETE FROM companies WHERE company_id = ? AND domain IS NULL "
                                 "AND status = 'sourced'", (g.retry_of,))
                except sqlite3.IntegrityError:
                    pass  # something else already points at it; review keeps it


# -- the run -----------------------------------------------------------------------


@dataclass
class Report:
    groups: int = 0
    verified: int = 0
    needs_review: Counter[str] = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    attached: int = 0
    outcomes: list[Outcome] = field(default_factory=list)


def placeholder_ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT company_id FROM companies WHERE company_id LIKE ? AND domain IS NULL "
        "AND status = 'sourced' ORDER BY company_id", (PLACEHOLDER_PREFIX + "%",))]


def select_groups(
    conn: sqlite3.Connection,
    seeds: list[Seed],
    cache: Cache,
    *,
    company_id: str | None = None,
    limit: int | None = None,
) -> list[Group]:
    """Groups a run would resolve. Placeholders are retried only when a search
    provider is configured, or when named with company_id."""
    retry: list[str] = []
    all_placeholders = placeholder_ids(conn)
    if company_id is not None and company_id in all_placeholders:
        retry = [company_id]
    elif company_id is None and search_api.is_configured():
        retry = all_placeholders
    groups = build_groups(gtm_signals(conn, retry), seeds, cache)
    if company_id is not None:
        groups = [g for g in groups if company_id in (g.placeholder_id, g.target_id, g.retry_of,
                                                      g.key)]
    if limit is not None:
        groups = groups[:limit]
    return groups


def projected_urls(groups: list[Group]) -> list[str]:
    """Pages a run is sure to request (for --dry-run): board metadata for ATS
    groups and the seed homepages. Candidate homepages and careers pages for the
    rest are found only during the run."""
    urls = []
    for g in groups:
        if g.board is not None:
            urls.append(meta_url(*g.board))
        if g.seed is not None:
            urls.append(g.seed.site)
        elif g.kind == "careers":
            urls.append(f"https://{g.key.split(':', 1)[1]}/")
    return urls


async def run(
    conn: sqlite3.Connection,
    groups: list[Group],
    guard: Guard,
    *,
    cache: Cache,
    budget: Budget | None = None,
) -> Report:
    rep = Report(groups=len(groups))
    for g in groups:
        try:
            out = await resolve_group(g, guard, cache=cache, budget=budget)
            if not out.error:
                write(conn, out)
        except (GuardConfigError, BudgetExceeded):
            raise
        except (Collision, ValueError, sqlite3.Error, states.TransitionError) as e:
            out = Outcome(group=g, error=f"{type(e).__name__}: {e}")
        rep.outcomes.append(out)
        if out.error:
            rep.errors.append(f"{g.key}: {out.error}")
        elif out.verified:
            rep.verified += 1
        else:
            rep.needs_review[out.code] += 1
        rep.attached += out.attached
    return rep

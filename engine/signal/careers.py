"""Careers-page collector for seed companies (docs/PLAN.md Step 23).

For each seed, fetch its careers_url through Guard, then:

1. ATS hand-off. If the careers URL itself, the URL it redirected to, or the
   page links to a Greenhouse, Lever, Ashby or Workable board (public or API
   host), the slug is read with one regex per host and the board is read by
   that collector. The board's JSON is the source of truth, so the page's own
   text is not read. At most MAX_HANDOFFS distinct boards per page.
2. Otherwise, job titles are read from the page itself, in this order:
   JSON-LD JobPosting objects, other embedded JSON objects shaped like a job,
   links, then headings. Navigation, header and footer blocks, social links,
   mail/phone links and generic link text ("Apply", "Learn more") are skipped.
   A link or heading counts only if its text names a role (a word such as
   engineer, manager, executive, SDR) or, for a link, its URL looks like a job
   page (/jobs/<id>, ?gh_jid=). Whether a role is GTM is not decided here; the
   title classifier (Step 24) does that.

Page signals have source="careers", board_slug = the seed's site domain,
posted_at = None (first_seen stands in, docs/PLAN.md Step 23; a JSON-LD
datePosted is not used), url = the job's own link when there is one, else the
careers page. Their id is "cr-{domain}-{sha1(title\\nurl)[:12]}", so a re-run
over the same page inserts nothing.

Per-seed outcome: ok | blocked | not_found | dead | robots_denied (Guard's
outcome for the careers page) or ats_handoff. Nothing here touches the
network itself or calls a model.
"""

from __future__ import annotations

import hashlib
import html as htmllib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from engine.guard.http import Guard, default_guard
from engine.models import Signal
from engine.signal import ats, boards
from engine.signal.seeds import Seed

SOURCE = "careers"
PREFIX = "cr"
FETCH_KIND = "careers_page"
MAX_HANDOFFS = 3
MAX_TITLE_CHARS = 120
MAX_TITLE_WORDS = 14

SeedOutcome = Literal["ok", "blocked", "not_found", "dead", "robots_denied", "ats_handoff"]

# -- ATS hosts -------------------------------------------------------------------

_SLUG = r"([A-Za-z0-9][A-Za-z0-9._-]*)"
_P = r"(?:https?:)?//"
ATS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (source, re.compile(rx, re.IGNORECASE))
    for source, rx in (
        ("greenhouse", _P + r"(?:job-boards|boards)\.greenhouse\.io/embed/job_board(?:/js)?"
                       r"\?(?:[^\"'\s<>#]*&)?for=" + _SLUG),
        ("greenhouse", _P + r"(?:job-boards|boards)\.greenhouse\.io/" + _SLUG),
        ("greenhouse", _P + r"boards-api\.greenhouse\.io/v1/boards/" + _SLUG),
        ("lever", _P + r"jobs\.lever\.co/" + _SLUG),
        ("lever", _P + r"api\.lever\.co/v0/postings/" + _SLUG),
        ("ashby", _P + r"jobs\.ashbyhq\.com/" + _SLUG),
        ("ashby", _P + r"api\.ashbyhq\.com/posting-api/job-board/" + _SLUG),
        ("workable", _P + r"apply\.workable\.com/api/v1/widget/accounts/" + _SLUG),
        ("workable", _P + r"apply\.workable\.com/" + _SLUG),
    )
)
# Path words on those hosts that are not board slugs.
RESERVED_SLUGS = frozenset({"embed", "api", "v0", "v1", "j", "jobs", "static", "assets"})


def find_boards(*texts: str) -> list[tuple[str, str]]:
    """Distinct (source, slug) ATS boards named in the texts, in order of appearance."""
    found: dict[tuple[str, str], tuple[str, str]] = {}
    for text in texts:
        if not text:
            continue
        hits: list[tuple[int, str, str]] = []
        for source, rx in ATS_PATTERNS:
            for m in rx.finditer(htmllib.unescape(text)):
                slug = m.group(1).rstrip(".")
                if slug.lower() in RESERVED_SLUGS:
                    continue
                try:
                    slug = ats.check_slug(slug)
                except ValueError:
                    continue
                hits.append((m.start(), source, slug))
        for _, source, slug in sorted(hits):
            found.setdefault((source, slug.lower()), (source, slug))
    return list(found.values())


# -- page reading -------------------------------------------------------------------

ROLE_WORD = re.compile(
    r"\b(?:manager|engineer|executive|representative|developer|designer|analyst|lead|head|"
    r"director|specialist|associate|intern|internship|consultant|architect|officer|"
    r"scientist|writer|recruiter|coordinator|administrator|strategist|advisor|adviser|"
    r"marketer|evangelist|vp|vice president|president|chief|cto|cfo|coo|cmo|cro|ceo|"
    r"sdr|bdr|bdm|ae|csm|revops|gtm|hacker|owner|tester|programmer|accountant|counsel|"
    r"editor|trainer|agent|technician|planner|controller|auditor|researcher|apprentice|"
    r"trainee|leader|operations|ops)s?\b",
    re.IGNORECASE,
)
GENERIC_TEXT = frozenset({
    "apply", "apply now", "apply here", "apply today", "view", "view job", "view role",
    "view all", "view all jobs", "view openings", "see all", "see openings", "see all jobs",
    "learn more", "read more", "more", "details", "view details", "careers", "jobs",
    "open positions", "open roles", "home", "about", "about us", "blog", "contact",
    "contact us", "privacy", "privacy policy", "terms", "login", "log in", "sign in",
    "sign up", "here", "click here",
})
SOCIAL_HOSTS = (
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "glassdoor.com", "glassdoor.co.in", "naukri.com", "github.com",
    "medium.com", "angel.co", "wellfound.com", "t.me", "wa.me", "whatsapp.com",
)
_JOB_PATH_SEGMENTS = frozenset({
    "job", "jobs", "jobdetails", "job-details", "jobdetail", "job-detail", "position",
    "positions", "opening", "openings", "vacancy", "vacancies", "requisition",
    "requisitions", "job-openings", "jobopenings",
})
_JOB_QUERY_KEYS = frozenset({"gh_jid", "jobid", "job_id", "jid", "job", "requisitionid"})
_JSON_TITLE_KEYS = ("title", "jobTitle", "job_title", "positionTitle", "position_title")
_JSON_STRONG_KEYS = frozenset({
    "jobId", "job_id", "jobID", "requisitionId", "requisition_id", "applyUrl", "apply_url",
    "jobUrl", "job_url", "employmentType", "employment_type", "datePosted", "hostedUrl",
})
_JSON_WEAK_KEYS = frozenset({
    "department", "location", "locations", "team", "jobType", "job_type",
    "workplaceType", "workplace_type", "remote",
})
_JSON_URL_KEYS = ("applyUrl", "apply_url", "jobUrl", "job_url", "hostedUrl", "absolute_url", "url")


def clean_text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def looks_like_title(text: str) -> bool:
    return (
        2 <= len(text) <= MAX_TITLE_CHARS
        and len(text.split()) <= MAX_TITLE_WORDS
        and text.lower().strip(" .:!>»→") not in GENERIC_TEXT
    )


def names_a_role(text: str) -> bool:
    return bool(ROLE_WORD.search(text))


def _host_in(host: str, hosts: tuple[str, ...]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def looks_like_job_url(url: str) -> bool:
    parts = urlsplit(url)
    segments = [s.lower() for s in parts.path.split("/") if s]
    if any(seg in _JOB_PATH_SEGMENTS for seg in segments[:-1]):
        return True
    keys = {k.lower() for k in parse_qs(parts.query)}
    return bool(keys & _JOB_QUERY_KEYS)


def _abs_url(base: str, href: object) -> str | None:
    if not isinstance(href, str) or not href.strip():
        return None
    href = href.strip()
    if href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    url, _ = urldefrag(urljoin(base, href))
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return url


@dataclass(frozen=True)
class Found:
    """A role read from the page before it becomes a Signal."""

    title: str
    url: str | None  # the job's own page, if the page gives one
    how: str  # jsonld | json | link | heading
    location: str | None = None
    description_text: str | None = None


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _types(obj: dict[str, Any]) -> set[str]:
    t = obj.get("@type")
    items = t if isinstance(t, list) else [t]
    return {i.lower() for i in items if isinstance(i, str)}


def _ld_location(posting: dict[str, Any]) -> str | None:
    names: list[str | None] = []
    locs = posting.get("jobLocation")
    for loc in locs if isinstance(locs, list) else [locs]:
        if isinstance(loc, str):
            names.append(loc)
            continue
        if not isinstance(loc, dict):
            continue
        addr = loc.get("address")
        if isinstance(addr, str):
            names.append(addr)
        elif isinstance(addr, dict):
            country = addr.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name")
            parts = [clean_text(addr.get("addressLocality")), clean_text(addr.get("addressRegion")),
                     clean_text(country)]
            names.append(", ".join(p for p in parts if p) or None)
        elif isinstance(loc.get("name"), str):
            names.append(loc["name"])
    if str(posting.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        names.append("Remote")
    return ats.join_locations(*names)


def _json_scripts(soup: BeautifulSoup, ld: bool) -> Iterator[Any]:
    for script in soup.find_all("script"):
        stype = (script.get("type") or "").lower().strip()
        is_ld = stype == "application/ld+json"
        is_json = stype == "application/json" or script.get("id") == "__NEXT_DATA__"
        if (ld and not is_ld) or (not ld and not is_json):
            continue
        raw = script.string or script.get_text() or ""
        try:
            yield json.loads(raw)
        except ValueError:
            continue  # malformed embedded JSON is skipped, not guessed at


def from_jsonld(soup: BeautifulSoup, base: str) -> list[Found]:
    out: list[Found] = []
    for data in _json_scripts(soup, ld=True):
        for obj in _walk(data):
            if "jobposting" not in _types(obj):
                continue
            title = clean_text(obj.get("title")) or clean_text(obj.get("name"))
            if not title or not looks_like_title(title):
                continue
            desc = obj.get("description")
            out.append(Found(
                title=title,
                url=_abs_url(base, obj.get("url")),
                how="jsonld",
                location=_ld_location(obj),
                description_text=ats.html_to_text(desc, escaped=True) if isinstance(desc, str) else None,
            ))
    return out


def from_embedded_json(soup: BeautifulSoup, base: str) -> list[Found]:
    """Objects in application/json scripts that are shaped like a job: a title
    plus one strong job key (jobId, applyUrl, employmentType...) or two weak
    ones (department, location, team...)."""
    out: list[Found] = []
    for data in _json_scripts(soup, ld=False):
        for obj in _walk(data):
            title = next((clean_text(obj.get(k)) for k in _JSON_TITLE_KEYS
                          if clean_text(obj.get(k))), "")
            if not title or not looks_like_title(title):
                continue
            keys = set(obj)
            if not (keys & _JSON_STRONG_KEYS or len(keys & _JSON_WEAK_KEYS) >= 2):
                continue
            url = next((u for k in _JSON_URL_KEYS if (u := _abs_url(base, obj.get(k)))), None)
            loc = obj.get("location")
            if isinstance(loc, dict):
                loc = loc.get("name")
            out.append(Found(title=title, url=url, how="json",
                             location=clean_text(loc) or None))
    return out


def _strip_chrome(soup: BeautifulSoup) -> None:
    """Remove navigation, page header and footer, which list pages, not roles."""
    for el in soup.find_all(["nav", "footer", "script", "style", "noscript", "template"]):
        el.decompose()
    for el in soup.find_all(attrs={"role": ["navigation", "contentinfo", "banner"]}):
        el.decompose()
    for el in soup.find_all("header"):
        if el.find_parent(["main", "article", "section", "li"]) is None:
            el.decompose()


def from_links(soup: BeautifulSoup, base: str) -> list[Found]:
    out: list[Found] = []
    for a in soup.find_all("a", href=True):
        url = _abs_url(base, a["href"])
        if url is None:
            continue
        if _host_in((urlsplit(url).hostname or "").lower(), SOCIAL_HOSTS):
            continue
        heading = a.find(["h1", "h2", "h3", "h4", "h5", "h6"])
        text = clean_text(heading.get_text(" ")) if heading else clean_text(a.get_text(" "))
        if not text or not looks_like_title(text):
            continue
        if names_a_role(text) or looks_like_job_url(url):
            out.append(Found(title=text, url=url, how="link"))
    return out


def from_headings(soup: BeautifulSoup) -> list[Found]:
    out: list[Found] = []
    for h in soup.find_all(["h2", "h3", "h4", "h5"]):
        text = clean_text(h.get_text(" "))
        if text and looks_like_title(text) and names_a_role(text):
            out.append(Found(title=text, url=None, how="heading"))
    return out


def read_page(markup: str, base_url: str) -> list[Found]:
    """Roles named on a careers page, de-duplicated.

    The same (title, url) is kept once. A title found without a URL of its own
    (a heading, or a JSON object without a link) is dropped when the same
    title was also found with one.
    """
    soup = BeautifulSoup(markup, "html.parser")
    base_tag = soup.find("base", href=True)
    base = urljoin(base_url, base_tag["href"]) if isinstance(base_tag, Tag) else base_url
    found = from_jsonld(soup, base) + from_embedded_json(soup, base)
    _strip_chrome(soup)
    found += from_links(soup, base) + from_headings(soup)

    with_url = {f.title.lower() for f in found if f.url}
    out: dict[tuple[str, str | None], Found] = {}
    for f in found:
        if f.url is None and f.title.lower() in with_url:
            continue
        out.setdefault((f.title.lower(), f.url), f)
    return list(out.values())


# -- signals -----------------------------------------------------------------------


def page_signal_id(domain: str, title: str, url: str) -> str:
    digest = hashlib.sha1(f"{title}\n{url}".encode()).hexdigest()[:12]
    return f"{PREFIX}-{domain}-{digest}"


def to_signal(seed: Seed, f: Found, today: date) -> Signal:
    url = f.url or seed.careers_url
    return Signal(
        signal_id=page_signal_id(seed.domain, f.title, url),
        source=SOURCE,
        board_slug=seed.domain,
        company_name_raw=seed.company,
        title=f.title,
        location=f.location,
        posted_at=None,
        url=url,
        first_seen=today,
        description_text=f.description_text,
    )


@dataclass
class SeedResult:
    seed: Seed
    outcome: SeedOutcome
    status: int | None = None
    final_url: str | None = None
    from_cache: bool = False
    requests: int = 0
    detail: str = ""
    signals: list[Signal] = field(default_factory=list)
    found: list[Found] = field(default_factory=list)  # page roles (empty on a hand-off)
    boards: list[ats.BoardResult] = field(default_factory=list)
    board_errors: list[str] = field(default_factory=list)
    boards_skipped: list[tuple[str, str]] = field(default_factory=list)  # over MAX_HANDOFFS

    @property
    def note(self) -> str:
        if self.outcome == "ats_handoff":
            parts = [f"{b.source}:{b.board_slug} {b.state}"
                     + (f" ({len(b.signals)})" if b.signals else "") for b in self.boards]
            parts += [f"error {e}" for e in self.board_errors]
            if self.boards_skipped:
                parts.append(f"{len(self.boards_skipped)} more board(s) not read")
            return "; ".join(parts)
        if self.outcome == "ok":
            return f"{len(self.signals)} roles on page"
        return self.detail


async def collect_seed(
    seed: Seed, guard: Guard | None = None, *, today: date | None = None
) -> SeedResult:
    """Fetch one seed's careers page and turn it into signals. Never raises on
    a site's answer; BudgetExceeded and GuardConfigError propagate."""
    g = guard or default_guard()
    today = today or datetime.now(UTC).date()

    direct = find_boards(seed.careers_url)
    if direct:
        res = SeedResult(seed=seed, outcome="ats_handoff", final_url=seed.careers_url,
                         detail="careers_url is an ATS board")
        await _hand_off(res, direct, g, today)
        return res

    r = await g.fetch(seed.careers_url, FETCH_KIND)
    res = SeedResult(seed=seed, outcome=r.outcome, status=r.status, final_url=r.final_url,
                     from_cache=r.from_cache, requests=r.requests, detail=r.detail)
    if r.outcome != "ok":
        return res
    markup = r.text
    found_boards = find_boards(r.final_url, markup)
    if found_boards:
        res.outcome = "ats_handoff"
        await _hand_off(res, found_boards, g, today)
        return res
    res.found = read_page(markup, r.final_url)
    res.signals = ats.dedupe(to_signal(seed, f, today) for f in res.found)
    return res


async def _hand_off(
    res: SeedResult, found: list[tuple[str, str]], g: Guard, today: date
) -> None:
    res.boards_skipped = found[MAX_HANDOFFS:]
    for source, slug in found[:MAX_HANDOFFS]:
        try:
            b = await boards.collect_board(source, slug, g, today=today)
        except ats.BoardFormatError as e:
            res.board_errors.append(str(e))
            continue
        res.boards.append(b)
        res.requests += b.requests
        res.signals.extend(b.signals)
    res.signals = ats.dedupe(res.signals)


def page_urls(seeds: list[Seed]) -> list[str]:
    """The careers pages a run would fetch (for --dry-run). Seeds whose
    careers_url is itself an ATS board are counted by their board URL."""
    urls = []
    for s in seeds:
        direct = find_boards(s.careers_url)
        if direct:
            urls += boards.board_urls(direct[:MAX_HANDOFFS])
        else:
            urls.append(s.careers_url)
    return urls

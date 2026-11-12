"""Tag the segment and apply the location rule (docs/PLAN.md Step 28).

Rule (protocol/ICP.md, "Segment and location rule"):

    head office India                      -> segment in_saas (role anywhere)
    head office abroad, an India role      -> segment us_india_team
    head office abroad, no India role      -> dropped, out_of_scope (stage resolve)
    unclear                                -> segment NULL, review_reason
                                              "segment_unclear: <code>: <detail>"

Input: companies at status `resolved` with a domain and no segment. Companies
already marked "segment_unclear" are skipped unless named with --company (a
re-run with the same pages would only reach the same verdict).

Pages, all through Guard (robots, pacing and today's cache apply): the
homepage (the seed's own site for a seed company, else https://<domain>/),
then at most MAX_EXTRA_PAGES pages linked from it on the same registrable
domain and discovered from its links, never guessed: one contact page, then
imprint / legal / terms / privacy / about, in that order of preference. A page
that is not `ok` (robots_denied, blocked, not_found, dead) is recorded in the
report and skipped.

Evidence, read deterministically from the page text (scripts and styles
removed, whitespace collapsed; quotes are verbatim from that text). No model.

  entity         a company name followed by a legal suffix. Only entities that
                 carry the company's own name (a distinctive token of its name
                 or its domain label) or follow a copyright sign count, so a
                 customer's or vendor's "Inc." is ignored.
                   india   Private Limited, Pvt. Ltd., Pvt Ltd, LLP
                   abroad  Inc., Incorporated, LLC, Corp., Corporation, GmbH,
                           Pte. Ltd., Pty Ltd, B.V., AG, SAS; and Ltd/Limited
                           only on a page with UK markers (England and Wales,
                           Companies House, United Kingdom)
                   (Ltd/Limited without UK markers is ignored: India uses it too)
  incorporation  CIN (e.g. U72900KA2015PTC123456) or "Companies Act, 2013/1956"
                 -> india; "Delaware" -> abroad.
  address        6-digit PIN next to an Indian city/state or "India" -> india;
                 ", <US state> 12345" -> abroad; UK postcode near London /
                 United Kingdom / England; "Singapore 123456" -> abroad.
  phone          +91 -> india; +1, +44, +65, +49, +61, +33, +31, +971 -> abroad.

Decision (conservative; anything else is unclear):
1. Entity-level evidence (entity + incorporation) on one side only: that side,
   unless every address found is on the other side (entity_address_conflict).
2. On both sides: a parent with an Indian subsidiary is common for US
   companies with India teams. The page says which is which by an entity
   named "<Name> India ..." (india side is the subsidiary), "<Name> USA/US/UK
   ..." (abroad side is the subsidiary), or "subsidiary of <entity>" (that
   entity is the parent). The side holding the parent is the head office
   (basis abroad_parent_india_subsidiary or india_parent_abroad_subsidiary).
   No such words: conflicting_entities.
3. No entity evidence: addresses on one side decide; both sides is
   addresses_both.
4. Phones alone never decide (phone_only).
5. Nothing at all: no_evidence.
Seed companies (data/seeds.csv) are Indian SaaS by construction, so a seed
whose readable pages show no head-office evidence, or only +91 phones, is
India (basis seed_default). Evidence for abroad still wins, and conflicting
evidence still goes to review. A seed whose homepage could not be read is
not defaulted: nothing was checked.

Location rule (abroad only): the company's fresh GTM signals (fresh = 1,
role_family set and not not_gtm). Any with india_located = 1 gives
us_india_team; if some have not been through `te signal filter` (fresh or
india_located NULL) the company goes to review (location_unknown) rather than
being dropped; otherwise it drops as out_of_scope.

Storage: companies.hq, hq_basis and hq_evidence (migration 0007), the markers
with quote, url and char_start. Not the facts table: facts are E4 rows tied to
captured artifacts and an extractor version, and E2 runs before capture.
Status does not change here except to dropped; Step 29 moves companies on.
Idempotent: only rows whose values differ are written.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from engine import states
from engine.guard.budget import BudgetExceeded
from engine.guard.http import Guard, GuardConfigError
from engine.models import HqMarker
from engine.resolve.domain import _hrefs, normalise_name, registrable_domain
from engine.signal.filter import NOT_GTM

SEGMENT_VERSION = "seg-1.0"  # bump when the markers or the decision change
IN_SAAS = "in_saas"
US_INDIA_TEAM = "us_india_team"
DROP_REASON = "out_of_scope"
DROP_STAGE = "resolve"
REVIEW_CODE = "segment_unclear"
MAX_EXTRA_PAGES = 3
KIND_HOME = "homepage"
KIND_CONTACT = "contact_page"
KIND_LEGAL = "legal_page"
QUOTE_CONTEXT = 60  # characters of context either side of a match
MAX_MARKERS = 24

# -- page discovery ------------------------------------------------------------------

# (kind, priority, link-text pattern, path pattern). Lower priority is fetched first.
_PAGE_KINDS: tuple[tuple[str, int, re.Pattern[str], re.Pattern[str]], ...] = tuple(
    (k, i, re.compile(t, re.IGNORECASE), re.compile(p, re.IGNORECASE))
    for i, (k, t, p) in enumerate((
        ("contact", r"\b(?:contact(?:\s+us)?|get\s+in\s+touch|reach\s+us|our\s+offices?)\b",
         r"/contact(?:[-_]?us)?(?:/|$|\.)"),
        ("imprint", r"\b(?:imprint|impressum)\b", r"/(?:imprint|impressum)(?:/|$|\.)"),
        ("legal", r"^\s*legal(?:\s+notice)?\s*$", r"/legal(?:/|$|\.)"),
        ("terms", r"\bterms\b", r"/(?:terms|tos|terms[-_]of[-_](?:service|use)|"
                                r"terms[-_]and[-_]conditions)(?:/|$|\.)"),
        ("privacy", r"\bprivacy\b", r"/privacy(?:[-_]policy)?(?:/|$|\.)"),
        ("about", r"\b(?:about(?:\s+us)?|company)\b", r"/(?:about(?:[-_]us)?|company)(?:/|$|\.)"),
    ))
)


def discover_pages(markup: str, base: str, domain: str) -> list[tuple[str, str]]:
    """Contact and legal pages linked from a page, as (kind, url), best first.

    Links must sit on the company's registrable domain. One link per kind, in
    page order; at most MAX_EXTRA_PAGES. Nothing is guessed.
    """
    home = base.split("#")[0].rstrip("/")
    first: dict[str, tuple[int, str]] = {}
    for url, text in _hrefs(markup, base):
        url = url.split("#")[0]
        if registrable_domain(url) != domain or url.rstrip("/") == home:
            continue
        path = urlsplit(url).path or "/"
        # The link text names the page better than its path (/legal/terms is terms).
        kind_prio = next(((k, p) for k, p, text_rx, _ in _PAGE_KINDS if text_rx.search(text)),
                         None)
        if kind_prio is None:
            kind_prio = next(((k, p) for k, p, _, path_rx in _PAGE_KINDS
                              if path_rx.search(path)), None)
        if kind_prio is None or kind_prio[0] in first:
            continue
        if url not in {u for _, u in first.values()}:
            first[kind_prio[0]] = (kind_prio[1], url)
    ranked = sorted(first.items(), key=lambda kv: kv[1][0])
    return [(k, u) for k, (_, u) in ranked][:MAX_EXTRA_PAGES]


def page_text(markup: str) -> str:
    """Visible text with scripts and styles removed and whitespace collapsed."""
    soup = BeautifulSoup(markup, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


# -- markers ---------------------------------------------------------------------------

_SUFFIXES: tuple[tuple[str, str, str | None], ...] = (
    # (pattern, marker, side); longest and most specific first. side None: Ltd, decided below.
    (r"(?i:private\s+limited)", "pvt_ltd", "india"),
    (r"(?i:pvt\.?\s*ltd\.?)", "pvt_ltd", "india"),
    (r"(?i:pte\.?\s*ltd\.?)", "pte_ltd", "abroad"),
    (r"(?i:pty\.?\s*ltd\.?)", "pty_ltd", "abroad"),
    (r"LLP", "llp", "india"),
    (r"(?i:incorporated)", "inc", "abroad"),
    (r"Inc\.?|INC\.?", "inc", "abroad"),
    (r"L\.L\.C\.|LLC", "llc", "abroad"),
    (r"(?i:corporation)|Corp\.?", "corp", "abroad"),
    (r"GmbH", "gmbh", "abroad"),
    (r"B\.V\.", "bv", "abroad"),
    (r"AG", "ag", "abroad"),
    (r"S\.A\.S\.?|SAS", "sas", "abroad"),
    (r"(?i:limited)|Ltd\.?|LTD\.?", "ltd", None),
)
_SUFFIX_RX = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(f"(?P<s{i}>{p})" for i, (p, _, _) in enumerate(_SUFFIXES))
    + r")(?![A-Za-z])"
)
# The words of an entity name just before its suffix: up to six capitalised
# words, optionally ending in a comma ("Zentrico, Inc."). A word starts with a
# capital letter, so a year or a phone number before the name is not part of it.
_NAME_BEFORE_RX = re.compile(
    r"((?:[A-Z][A-Za-z0-9&'.\-]*\s+){0,5}[A-Z][A-Za-z0-9&'.\-]*)\s*,?\s*$")
_COPYRIGHT_RX = re.compile(
    r"(?:©|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?\s*,?\s*$", re.IGNORECASE)
_SUBSIDIARY_OF_BEFORE_RX = re.compile(r"subsidiary\s+of\s+(?:the\s+)?$", re.IGNORECASE)
_SUBSIDIARY_AFTER_RX = re.compile(
    r"^\s*[,(]?\s*(?:is\s+)?an?\s+(?:wholly[\s-]+owned\s+)?subsidiary\s+of\b", re.IGNORECASE)
_INDIA_WORD_RX = re.compile(r"\bIndia\b", re.IGNORECASE)
_ABROAD_SUB_WORD_RX = re.compile(r"\b(?:USA|US|U\.S\.|America|UK|Europe|EU|Singapore)\b")
_UK_PAGE_RX = re.compile(r"England\s+and\s+Wales|Companies\s+House|United\s+Kingdom",
                         re.IGNORECASE)

_CIN_RX = re.compile(
    r"(?<![A-Z0-9])[LU]\d{5}[A-Z]{2}\d{4}"
    r"(?:PTC|PLC|FTC|FLC|GOI|GAP|GAT|NPL|OPC|SGC|ULL|ULT)\d{6}(?![A-Z0-9])")
_COMPANIES_ACT_RX = re.compile(r"Companies\s+Act,?\s+(?:2013|1956)", re.IGNORECASE)
_DELAWARE_RX = re.compile(r"\bDelaware\b")

_IN_PLACES = (
    "Bengaluru", "Bangalore", "Karnataka", "Mumbai", "Navi Mumbai", "Thane", "Maharashtra",
    "Pune", "Hyderabad", "Secunderabad", "Telangana", "Chennai", "Tamil Nadu", "Coimbatore",
    "Gurugram", "Gurgaon", "Haryana", "Noida", "Greater Noida", "Uttar Pradesh", "New Delhi",
    "Delhi", "Kolkata", "West Bengal", "Ahmedabad", "Gujarat", "Kochi", "Kerala",
    "Thiruvananthapuram", "Jaipur", "Rajasthan", "Chandigarh", "Punjab", "Indore",
    "Madhya Pradesh", "Bhubaneswar", "Odisha", "Goa", "India",
)
_IN_PLACE = "(?:" + "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in _IN_PLACES) + ")"
_PIN = r"[1-9]\d{2}\s?\d{3}"
_IN_ADDRESS_RX = re.compile(
    rf"\b{_IN_PLACE}\b[\s,.\-–]*(?<!\d){_PIN}(?!\d)"
    rf"|(?<!\d){_PIN}(?!\d)[\s,.\-–]*\b{_IN_PLACE}\b")

_US_STATE_CODES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE "
    "NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY").split()
_US_STATE_NAMES = (
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
    "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire",
    "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington", "West Virginia",
    "Wisconsin", "Wyoming",
)
_US_ADDRESS_RX = re.compile(
    r",\s*(?:" + "|".join(_US_STATE_CODES) + "|"
    + "|".join(re.escape(n).replace(r"\ ", r"\s+") for n in _US_STATE_NAMES)
    + r")\.?\s+\d{5}(?:-\d{4})?(?!\d)")
_UK_POSTCODE_RX = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s+\d[A-Z]{2}\b")
_UK_PLACE_RX = re.compile(r"\b(?:London|United\s+Kingdom|UK|England|Scotland|Wales)\b")
_SG_ADDRESS_RX = re.compile(r"\bSingapore\s+\d{6}(?!\d)")

_PHONE_SEP = r"[\s\-.]*"
_IN_PHONE_RX = re.compile(rf"(?<![\w+])\+\s?91{_PHONE_SEP}\(?\d[\d\s\-.()]{{7,14}}\d")
_US_PHONE_RX = re.compile(
    rf"(?<![\w+])\+\s?1{_PHONE_SEP}\(?\d{{3}}\)?{_PHONE_SEP}\d{{3}}{_PHONE_SEP}\d{{4}}(?!\d)")
_OTHER_PHONE_RX = re.compile(
    rf"(?<![\w+])\+\s?(44|65|49|61|33|31|971){_PHONE_SEP}\(?\d[\d\s\-.()]{{6,14}}\d")

_GENERIC_NAME_WORDS = frozenset({
    "technologies", "technology", "tech", "software", "softwares", "solutions", "systems",
    "labs", "lab", "india", "global", "group", "services", "digital", "and", "app", "apps",
    "company", "data", "cloud", "online", "ventures", "infotech", "info", "networks",
    "platforms", "platform", "hq", "studio", "studios", "works", "consulting",
})


def name_anchors(name: str, domain: str | None) -> set[str]:
    """Distinctive lower-case tokens of the company's name and its domain label."""
    out = {t for t in normalise_name(name).split()
           if len(t) >= 3 and t not in _GENERIC_NAME_WORDS}
    if domain:
        label = domain.split(".", 1)[0].lower()
        if len(label) >= 3:
            out.add(label)
    return out


def _anchored(entity_name: str, anchors: set[str]) -> bool:
    tokens = re.findall(r"[a-z0-9]+", entity_name.lower())
    joined = "".join(tokens)
    return any(a in tokens or (len(a) >= 5 and a in joined) for a in anchors)


def _quote(text: str, start: int, end: int) -> tuple[str, int]:
    """The match with up to QUOTE_CONTEXT characters either side, cut at word
    boundaries, verbatim from text; returns (quote, char_start)."""
    a = max(0, start - QUOTE_CONTEXT)
    if a > 0:
        sp = text.find(" ", a, start)
        a = sp + 1 if sp != -1 else start
    b = min(len(text), end + QUOTE_CONTEXT)
    if b < len(text):
        sp = text.rfind(" ", end, b)
        b = sp if sp != -1 else end
    q = text[a:b]
    lead = len(q) - len(q.lstrip())
    return q.strip(), a + lead


def _mk(text: str, url: str, m_start: int, m_end: int, **kw: object) -> HqMarker:
    q, cs = _quote(text, m_start, m_end)
    return HqMarker(quote=q, url=url, char_start=cs, **kw)  # type: ignore[arg-type]


def entity_markers(text: str, url: str, anchors: set[str]) -> list[HqMarker]:
    out: list[HqMarker] = []
    uk_page = bool(_UK_PAGE_RX.search(text))
    for m in _SUFFIX_RX.finditer(text):
        idx = next(i for i in range(len(_SUFFIXES)) if m.group(f"s{i}") is not None)
        _, marker, side = _SUFFIXES[idx]
        if side is None:
            if not uk_page:
                continue
            side, marker = "abroad", "ltd_uk"
        before = text[max(0, m.start() - 90):m.start()]
        nm = _NAME_BEFORE_RX.search(before)
        if nm is None:
            continue
        name = nm.group(1).strip()
        lead = before[:nm.start(1)]
        if not (_anchored(name, anchors) or _COPYRIGHT_RX.search(lead)):
            continue
        entity = f"{name} {m.group(0).strip()}" if not before.rstrip().endswith(",") \
            else f"{name}, {m.group(0).strip()}"
        role: str | None = None
        after = text[m.end():m.end() + 40]
        if _SUBSIDIARY_OF_BEFORE_RX.search(lead):
            role = "parent"
        elif _SUBSIDIARY_AFTER_RX.search(after):
            role = "subsidiary"
        elif side == "india" and _INDIA_WORD_RX.search(name):
            role = "subsidiary"
        elif side == "abroad" and _ABROAD_SUB_WORD_RX.search(name):
            role = "subsidiary"
        start = m.start() - (len(before) - nm.start(1))
        out.append(_mk(text, url, start, m.end(), marker=f"entity:{marker}", side=side,
                       kind="entity", entity=entity, role=role))
    return out


def find_markers(text: str, url: str, anchors: set[str]) -> list[HqMarker]:
    """Every head-office marker on one page's text."""
    out = entity_markers(text, url, anchors)
    for m in _CIN_RX.finditer(text):
        out.append(_mk(text, url, m.start(), m.end(), marker="cin", side="india",
                       kind="incorporation"))
    for m in _COMPANIES_ACT_RX.finditer(text):
        out.append(_mk(text, url, m.start(), m.end(), marker="companies_act", side="india",
                       kind="incorporation"))
    for m in _DELAWARE_RX.finditer(text):
        out.append(_mk(text, url, m.start(), m.end(), marker="delaware", side="abroad",
                       kind="incorporation"))
    for m in _IN_ADDRESS_RX.finditer(text):
        out.append(_mk(text, url, m.start(), m.end(), marker="address:in", side="india",
                       kind="address"))
    for m in _US_ADDRESS_RX.finditer(text):
        out.append(_mk(text, url, m.start() + 1, m.end(), marker="address:us", side="abroad",
                       kind="address"))
    for m in _UK_POSTCODE_RX.finditer(text):
        if _UK_PLACE_RX.search(text[max(0, m.start() - 40):m.end() + 40]):
            out.append(_mk(text, url, m.start(), m.end(), marker="address:uk", side="abroad",
                           kind="address"))
    for m in _SG_ADDRESS_RX.finditer(text):
        out.append(_mk(text, url, m.start(), m.end(), marker="address:sg", side="abroad",
                       kind="address"))
    for m in _IN_PHONE_RX.finditer(text):
        out.append(_mk(text, url, m.start(), m.end(), marker="phone:+91", side="india",
                       kind="phone"))
    for m in _US_PHONE_RX.finditer(text):
        out.append(_mk(text, url, m.start(), m.end(), marker="phone:+1", side="abroad",
                       kind="phone"))
    for m in _OTHER_PHONE_RX.finditer(text):
        out.append(_mk(text, url, m.start(), m.end(), marker=f"phone:+{m.group(1)}",
                       side="abroad", kind="phone"))
    return out


def dedupe_markers(markers: Iterable[HqMarker]) -> list[HqMarker]:
    """One marker per (marker, side, entity or quote) across pages, first page first."""
    seen: set[tuple[str, str, str]] = set()
    out: list[HqMarker] = []
    for m in markers:
        key = (m.marker, m.side, (m.entity or m.quote).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out[:MAX_MARKERS]


# -- decision ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    hq: str | None  # "india" | "abroad" | None
    basis: str | None
    code: str = ""  # review code when hq is None
    detail: str = ""

    @property
    def review_reason(self) -> str | None:
        if self.hq is not None:
            return None
        return f"{REVIEW_CODE}: {self.code}: {self.detail}"


def _show(ms: list[HqMarker], n: int = 2) -> str:
    return "; ".join(f"'{m.entity or m.quote}' ({m.url})" for m in ms[:n])


def decide(markers: list[HqMarker], *, seed: bool = False) -> Decision:
    """Head office from markers. See the module docstring for the rules."""
    def of(kinds: tuple[str, ...], side: str) -> list[HqMarker]:
        return [m for m in markers if m.kind in kinds and m.side == side]

    ent = {s: of(("entity", "incorporation"), s) for s in ("india", "abroad")}
    addr = {s: of(("address",), s) for s in ("india", "abroad")}
    phone = {s: of(("phone",), s) for s in ("india", "abroad")}

    if ent["india"] and ent["abroad"]:
        def roles(side: str) -> tuple[bool, bool]:
            names = [m for m in ent[side] if m.kind == "entity"]
            parent = any(m.role == "parent" for m in names)
            all_sub = bool(names) and all(m.role == "subsidiary" for m in names)
            return parent, all_sub

        in_parent, in_sub = roles("india")
        ab_parent, ab_sub = roles("abroad")
        if (ab_parent and not in_parent) or (in_sub and not ab_sub and not in_parent):
            return Decision("abroad", "abroad_parent_india_subsidiary")
        if (in_parent and not ab_parent) or (ab_sub and not in_sub and not ab_parent):
            return Decision("india", "india_parent_abroad_subsidiary")
        return Decision(None, None, "conflicting_entities",
                        f"india {_show(ent['india'])} vs abroad {_show(ent['abroad'])}")

    for side, other in (("india", "abroad"), ("abroad", "india")):
        if ent[side]:
            if addr[other] and not addr[side]:
                return Decision(None, None, "entity_address_conflict",
                                f"{side} {_show(ent[side])} but only {other} addresses "
                                f"{_show(addr[other])}")
            return Decision(side, "entity")

    if addr["india"] and addr["abroad"]:
        return Decision(None, None, "addresses_both",
                        f"india {_show(addr['india'])} and abroad {_show(addr['abroad'])}")
    for side in ("india", "abroad"):
        if addr[side]:
            return Decision(side, "address")

    if phone["abroad"]:
        return Decision(None, None, "phone_only",
                        "only phone numbers: " + _show(phone["abroad"] + phone["india"]))
    if seed:
        return Decision("india", "seed_default")
    if phone["india"]:
        return Decision(None, None, "phone_only", "only phone numbers: " + _show(phone["india"]))
    return Decision(None, None, "no_evidence",
                    "no entity name, incorporation, address or phone on the pages read")


# -- one company -------------------------------------------------------------------------


@dataclass
class Target:
    company_id: str
    name: str
    domain: str
    homepage: str
    seed: bool
    review_reason: str | None = None


@dataclass
class Outcome:
    target: Target
    decision: Decision | None = None
    markers: list[HqMarker] = field(default_factory=list)
    pages: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, url, outcome)
    segment: str | None = None
    dropped: bool = False
    code: str = ""  # review code, when not decided
    detail: str = ""
    changed: bool = False
    error: str = ""

    @property
    def state(self) -> str:
        if self.error:
            return "error"
        if self.dropped:
            return DROP_REASON
        return self.segment or "needs_review"


async def gather(t: Target, guard: Guard) -> tuple[list[HqMarker], list[tuple[str, str, str]],
                                                   str | None]:
    """Fetch the homepage and its contact/legal pages; return the markers, the
    pages tried, and the homepage outcome when it was not ok."""
    anchors = name_anchors(t.name, t.domain)
    pages: list[tuple[str, str, str]] = []
    home = await guard.fetch(t.homepage, KIND_HOME)
    pages.append(("homepage", home.url, home.outcome))
    if not home.ok:
        return [], pages, home.outcome
    markers = find_markers(page_text(home.text), home.final_url, anchors)
    for kind, url in discover_pages(home.text, home.final_url, t.domain):
        page = await guard.fetch(url, KIND_CONTACT if kind == "contact" else KIND_LEGAL)
        pages.append((kind, page.url, page.outcome))
        if page.ok:
            markers += find_markers(page_text(page.text), page.final_url, anchors)
    return dedupe_markers(markers), pages, None


def india_roles(conn: sqlite3.Connection, company_id: str) -> tuple[int, int]:
    """(fresh GTM signals located in India, fresh-or-unfiltered GTM signals not yet
    through the filter)."""
    row = conn.execute(
        "SELECT "
        "  SUM(CASE WHEN fresh = 1 AND india_located = 1 THEN 1 ELSE 0 END) AS india, "
        "  SUM(CASE WHEN fresh IS NULL OR (fresh = 1 AND india_located IS NULL) "
        "      THEN 1 ELSE 0 END) AS unknown "
        "FROM signals WHERE company_id = ? AND role_family IS NOT NULL AND role_family != ?",
        (company_id, NOT_GTM),
    ).fetchone()
    return int(row["india"] or 0), int(row["unknown"] or 0)


def _write(conn: sqlite3.Connection, t: Target, segment: str | None, d: Decision,
           markers: list[HqMarker], review_reason: str | None) -> bool:
    evidence = json.dumps([m.model_dump(mode="json", exclude_none=True) for m in markers],
                          ensure_ascii=False)
    cur = conn.execute(
        "SELECT segment, hq, hq_basis, hq_evidence, review_reason FROM companies "
        "WHERE company_id = ?", (t.company_id,)).fetchone()
    keep_reason = cur["review_reason"]
    if keep_reason is not None and not keep_reason.startswith(REVIEW_CODE + ":"):
        review_reason = keep_reason  # someone else's reason: not ours to clear
    new = (segment, d.hq, d.basis, evidence, review_reason)
    old = (cur["segment"], cur["hq"], cur["hq_basis"], cur["hq_evidence"], cur["review_reason"])
    if new == old:
        return False
    with conn:
        conn.execute(
            "UPDATE companies SET segment = ?, hq = ?, hq_basis = ?, hq_evidence = ?, "
            "review_reason = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE company_id = ?", (*new, t.company_id))
    return True


def apply(conn: sqlite3.Connection, out: Outcome) -> None:
    """Turn a decision into a segment, a drop or a review, and write it."""
    t, d = out.target, out.decision
    assert d is not None
    segment: str | None = None
    review = d.review_reason
    if d.hq == "india":
        segment = IN_SAAS
    elif d.hq == "abroad":
        india, unknown = india_roles(conn, t.company_id)
        if india:
            segment = US_INDIA_TEAM
        elif unknown:
            # Head office stays recorded as abroad; only the role check is open.
            out.code = "location_unknown"
            out.detail = (f"head office abroad ({d.basis}); {unknown} GTM signal(s) not yet "
                          "through `te signal filter`")
            review = f"{REVIEW_CODE}: {out.code}: {out.detail}"
        else:
            out.dropped = True
    else:
        out.code, out.detail = d.code, d.detail
    out.segment = segment
    out.changed = _write(conn, t, segment, d, out.markers, review)
    if out.dropped:
        out.changed = states.drop(t.company_id, DROP_REASON, DROP_STAGE, conn) or out.changed


async def tag(t: Target, guard: Guard) -> Outcome:
    out = Outcome(target=t)
    markers, pages, home_fail = await gather(t, guard)
    out.markers, out.pages = markers, pages
    if home_fail is not None:
        out.decision = Decision(None, None, f"homepage_{home_fail}",
                                f"{t.homepage} was {home_fail}; nothing read")
    else:
        out.decision = decide(markers, seed=t.seed)
    return out


# -- the run -------------------------------------------------------------------------------


def select(
    conn: sqlite3.Connection,
    seed_sites: dict[str, str] | None = None,
    *,
    company_id: str | None = None,
    limit: int | None = None,
) -> list[Target]:
    """Resolved companies with a domain and no segment. seed_sites maps a seed's
    registrable domain to its site URL. Earlier segment_unclear rows are retried
    only when named."""
    seed_sites = seed_sites or {}
    sql = ("SELECT company_id, name, domain, review_reason FROM companies "
           "WHERE status = 'resolved' AND domain IS NOT NULL AND segment IS NULL")
    params: list[object] = []
    if company_id is not None:
        sql += " AND company_id = ?"
        params.append(company_id)
    else:
        sql += " AND (review_reason IS NULL OR review_reason NOT LIKE ?)"
        params.append(REVIEW_CODE + ":%")
    sql += " ORDER BY company_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    out = []
    for r in conn.execute(sql, params):
        site = seed_sites.get(r["domain"])
        out.append(Target(company_id=r["company_id"], name=r["name"], domain=r["domain"],
                          homepage=site or f"https://{r['domain']}/", seed=site is not None,
                          review_reason=r["review_reason"]))
    return out


@dataclass
class Report:
    companies: int = 0
    segments: Counter[str] = field(default_factory=Counter)
    dropped: int = 0
    needs_review: Counter[str] = field(default_factory=Counter)
    changed: int = 0
    pages: Counter[str] = field(default_factory=Counter)  # outcome -> count, extra pages too
    errors: list[str] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)


async def run(conn: sqlite3.Connection, targets: list[Target], guard: Guard) -> Report:
    rep = Report(companies=len(targets))
    for t in targets:
        try:
            out = await tag(t, guard)
            apply(conn, out)
        except (GuardConfigError, BudgetExceeded):
            raise
        except (ValueError, sqlite3.Error, states.TransitionError) as e:
            out = Outcome(target=t, error=f"{type(e).__name__}: {e}")
        rep.outcomes.append(out)
        for _, _, outcome in out.pages:
            rep.pages[outcome] += 1
        if out.error:
            rep.errors.append(f"{t.company_id}: {out.error}")
            continue
        rep.changed += int(out.changed)
        if out.dropped:
            rep.dropped += 1
        elif out.segment:
            rep.segments[out.segment] += 1
        else:
            rep.needs_review[out.code] += 1
    return rep

"""Homepage facts (docs/PLAN.md Step 37): primary_cta and segment_pages.

primary_cta
-----------
The hero link, found by the same rule capture used to pick the demo_form page
(engine/capture/discover.py primary_cta): the first link whose text reads like
a call to action (discover._CTA_RX, at most MAX_ANCHOR_CHARS long) outside nav,
header and footer; else the first one in nav or header. Its target must be on
the company's domain or a fixed booking host, and not the homepage itself.
The structure does not keep CSS classes, so discover's "button-styled wins"
tie-break is not applied here.

| Value   | Text or target                                                      |
| ------- | ------------------------------------------------------------------- |
| demo    | demo, call, meeting, schedule; or a booking-host target             |
| signup  | sign up, get started, start free, trial, try, get access            |
| contact | contact, talk to, get in touch, quote                               |
| other   | any other CTA-worded link                                           |
| none    | no CTA-worded link (found False, absent)                            |

segment_pages
-------------
Count of distinct pages linked from the homepage's navigation (nav, header and
footer) that are industry, use-case or "for teams" pages: a path under
/industries, /industry, /use-cases, /usecases, /use-case, /verticals,
/segments, /teams, /who-we-serve or /for (with a page below it), a first path
segment starting "for-", or link text starting "For " or ending " teams".
/solutions pages count only when their text says "For ...": most are product
pages. Value an integer; 0 is found False.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from engine.capture.bundle import BOOKING_HOSTS
from engine.capture.discover import _CTA_RX, MAX_ANCHOR_CHARS
from engine.extract.base import Bundle, FactOut, absent, best_quote, host_matches, host_path

KIND = "homepage"

_DEMO = re.compile(r"\bdemo\b|\bcall\b|\bmeeting\b|\bschedule\b", re.I)
_SIGNUP = re.compile(r"\bsign\s*up\b|\bget\s+started\b|\bstart\b|\btrial\b|\btry\b"
                     r"|\bget\s+access\b|\bcreate\s+(?:an\s+)?account\b", re.I)
_CONTACT = re.compile(r"\bcontact\b|\btalk\s+to\b|\bget\s+in\s+touch\b|\bquote\b", re.I)


def _on_domain(url: str, domain: str | None) -> bool:
    host, _ = host_path(url)
    return bool(domain) and host_matches(host, domain or "")


def _booking(url: str) -> bool:
    host, _ = host_path(url)
    return any(host_matches(host, h) for h in BOOKING_HOSTS)


def _is_home(url: str) -> bool:
    p = urlsplit(url)
    return (p.path or "/") == "/" and not p.query


def cta_link(b: Bundle) -> dict | None:
    """The homepage's primary CTA link from its structure, or None."""
    art = b.arts.get(KIND)
    if art is None or not art.ok:
        return None
    ranked = []
    for i, a in enumerate((art.structure or {}).get("links") or []):
        url, text = a.get("url"), a.get("text") or ""
        if not url or urlsplit(url).scheme not in ("http", "https") or _is_home(url):
            continue
        if not (_on_domain(url, b.domain) or _booking(url)):
            continue
        if not text or len(text) > MAX_ANCHOR_CHARS or not _CTA_RX.search(text):
            continue
        region = a.get("region")
        if region == "footer":
            continue
        ranked.append((1 if region in ("nav", "header") else 0, i, a))
    return min(ranked, key=lambda r: r[:2])[2] if ranked else None


def cta_type(text: str, url: str) -> str:
    if _DEMO.search(text) or _booking(url):
        return "demo"
    if _SIGNUP.search(text):
        return "signup"
    if _CONTACT.search(text):
        return "contact"
    return "other"


def primary_cta(b: Bundle) -> list[FactOut]:
    if b.state(KIND) != "ok":
        return []
    art = b.arts[KIND]
    link = cta_link(b)
    if link is None:
        return [absent("primary_cta", "none", KIND, "call-to-action link")]
    return [best_quote("primary_cta", cta_type(link["text"], link["url"]), art, link["text"])]


_SEG_DIRS = frozenset({"industries", "industry", "use-cases", "usecases", "use-case",
                       "verticals", "segments", "teams", "who-we-serve", "for"})
_FOR_TEXT = re.compile(r"^for\s+\S|\S\s+teams$", re.I)


def is_segment_link(url: str, text: str) -> bool:
    segs = [s.lower() for s in urlsplit(url).path.split("/") if s]
    if segs and segs[0] in _SEG_DIRS and len(segs) >= 2:
        return True
    if segs and segs[0].startswith("for-"):
        return True
    if segs and segs[0] == "solutions":
        return bool(re.match(r"^for\s+\S", text, re.I))
    return bool(text) and bool(_FOR_TEXT.search(text))


def segment_pages(b: Bundle) -> list[FactOut]:
    if b.state(KIND) != "ok":
        return []
    art = b.arts[KIND]
    pages: dict[str, dict] = {}
    for a in (art.structure or {}).get("links") or []:
        url = a.get("url")
        if not url or a.get("region") not in ("nav", "header", "footer"):
            continue
        if not _on_domain(url, b.domain):
            continue
        if is_segment_link(url, a.get("text") or ""):
            pages.setdefault(url.split("#", 1)[0].rstrip("/"), a)
    if not pages:
        return [absent("segment_pages", 0, KIND, "industry, use-case or for-teams link")]
    first = next(iter(pages.values()))
    stored = first.get("text") or first.get("href") or ""
    return [best_quote("segment_pages", len(pages), art, stored)]


EXTRACTORS = (
    ("primary_cta", primary_cta, (KIND,)),
    ("segment_pages", segment_pages, (KIND,)),
)

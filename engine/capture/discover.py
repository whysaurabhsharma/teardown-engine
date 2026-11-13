"""Discover the bundle's pages (docs/PLAN.md Step 33). Pure functions; no network.

Pages are found, never guessed. The sources, in priority order:

1. nav     links inside <nav>, <header> or role="navigation" on the homepage
2. footer  links inside <footer> or role="contentinfo"
3. body    other links on the homepage (sites without semantic nav markup)
4. sitemap <loc> entries of /sitemap.xml (path only; a sitemap index is not
           followed, so the fetch count stays fixed)

A link maps to a kind by its anchor text first, then by its path. Anchor text:
"pricing" or "plans" is pricing; "customers", "case studies" or "stories" is
customers; "vs", "compare" or "alternatives" is compare; and so on (_RULES).
Path matching only looks at the first two path segments, as whole segments
(or a "-vs-" slug for compare), so /blog/pricing-tips is not pricing.

Only links on the company's registrable domain count. demo_form is the target
of the homepage's primary CTA (primary_cta): it may also sit on one of the
fixed BOOKING_HOSTS in bundle.py, and only when the CTA itself links there.
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from engine.capture.bundle import BOOKING_HOSTS
from engine.resolve.domain import registrable_domain

Source = Literal["nav", "footer", "body", "sitemap", "cta"]
SOURCE_RANK: dict[str, int] = {"nav": 0, "footer": 1, "body": 2, "sitemap": 3}
MAX_ANCHOR_CHARS = 40  # longer anchor text is prose, not a menu item

# (kind, anchor-text pattern, path-segment pattern). Checked in this order; the
# first rule that matches a link names it.
_RULES: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = tuple(
    (k, re.compile(t, re.IGNORECASE), re.compile(p, re.IGNORECASE))
    for k, t, p in (
        ("compare", r"\b(?:vs\.?|versus|compare|comparisons?|alternatives?)\b",
         r"^(?:vs|versus|compare|comparisons?|alternatives?)$|.-vs-."),
        ("pricing", r"\b(?:pricing|plans|prices)\b", r"^(?:pricing|plans|prices)$"),
        ("customers", r"\b(?:customers?|case\s+stud(?:y|ies)|(?:customer\s+|success\s+)?stories"
                      r"|clients)\b",
         r"^(?:customers?|case[-_]stud(?:y|ies)|success[-_]stories|customer[-_]stories"
         r"|stories|clients)$"),
        ("careers", r"\b(?:careers?|jobs|join\s+us|join\s+the\s+team|work\s+with\s+us"
                    r"|we'?re\s+hiring|open\s+(?:roles|positions))\b",
         r"^(?:careers?|jobs|join[-_]us|work[-_]with[-_]us|hiring)$"),
        ("contact", r"\b(?:contact(?:\s+us)?|get\s+in\s+touch)\b",
         r"^(?:contact(?:[-_]us)?|get[-_]in[-_]touch)$"),
        ("about", r"\b(?:about(?:\s+us)?|company|our\s+story|who\s+we\s+are)\b",
         r"^(?:about(?:[-_]us)?|company|our[-_]story|who[-_]we[-_]are)$"),
        ("product", r"\b(?:products?|platform|features?|solutions?)\b",
         r"^(?:products?|platform|features?|solutions?)$"),
    )
)

# A primary CTA reads like one of these.
_CTA_RX = re.compile(
    r"\b(?:book|schedule|request|get|see|watch)\s+(?:a\s+|your\s+|free\s+|live\s+)?(?:demo|call|meeting)"
    r"|\bdemo\b|\btalk\s+to\s+(?:sales|us|an?\s+expert)\b|\bcontact\s+sales\b"
    r"|\bget\s+started\b|\bstart\s+(?:for\s+)?free\b|\bstart\s+(?:your\s+)?(?:free\s+)?trial\b"
    r"|\bfree\s+trial\b|\btry\s+(?:it\s+)?(?:for\s+)?(?:free|now)\b|\bsign\s+up\b"
    r"|\bget\s+(?:a\s+)?(?:quote|access|in\s+touch)\b",
    re.IGNORECASE,
)
_BUTTONISH_RX = re.compile(r"btn|button|cta", re.IGNORECASE)


@dataclass(frozen=True)
class Link:
    url: str  # absolute, fragment removed
    text: str  # anchor text, whitespace collapsed
    source: Source

    def describe(self) -> str:
        how = f"'{self.text}'" if self.text else "its path"
        return f"{self.source} link {how}"


def _region(a: Tag) -> Source:
    for parent in a.parents:
        if not isinstance(parent, Tag):
            continue
        role = str(parent.get("role") or "").lower()
        if parent.name in ("nav", "header") or role == "navigation":
            return "nav"
        if parent.name == "footer" or role == "contentinfo":
            return "footer"
    return "body"


def _abs(base: str, href: object) -> str | None:
    if not isinstance(href, str):
        return None
    href = href.strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    url = urljoin(base, href).split("#", 1)[0]
    return url if urlsplit(url).scheme in ("http", "https") else None


def homepage_links(markup: str, base: str) -> list[Link]:
    """Every http(s) link on the page with its region, in page order."""
    soup = BeautifulSoup(markup, "html.parser")
    out: list[Link] = []
    for a in soup.find_all("a", href=True):
        url = _abs(base, a.get("href"))
        if url:
            out.append(Link(url, " ".join(a.get_text(" ").split()), _region(a)))
    return out


_LOC_RX = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


def sitemap_urls(body: bytes | None) -> list[str]:
    """<loc> URLs of a urlset sitemap, in order. A sitemap index yields nothing
    (its children are not followed)."""
    if not body:
        return []
    text = body.decode("utf-8", errors="replace")
    if re.search(r"<sitemapindex\b", text, re.IGNORECASE):
        return []
    return [htmllib.unescape(m.group(1)) for m in _LOC_RX.finditer(text)]


def _segments(url: str) -> list[str]:
    return [s for s in urlsplit(url).path.split("/") if s][:2]


def classify(url: str, text: str) -> str | None:
    """The bundle kind a link names, by anchor text then path; None if none."""
    t = text.strip()
    if t and len(t) <= MAX_ANCHOR_CHARS:
        for kind, text_rx, _ in _RULES:
            if text_rx.search(t):
                return kind
    segs = [s.lower().rsplit(".", 1)[0] for s in _segments(url)]
    for kind, _, seg_rx in _RULES:
        if any(seg_rx.search(s) for s in segs):
            return kind
    return None


def _is_home(url: str) -> bool:
    p = urlsplit(url)
    return (p.path or "/") == "/" and not p.query


def on_domain(url: str, domain: str) -> bool:
    return registrable_domain(url) == domain


def on_booking_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in BOOKING_HOSTS)


def primary_cta(markup: str, base: str, domain: str) -> Link | None:
    """The homepage's primary call to action: the first CTA-worded link outside
    nav and footer (the hero), else the first one in the nav. Button-styled links
    win within a region. Its target must be on the company's domain or a fixed
    booking host; the homepage itself does not count."""
    soup = BeautifulSoup(markup, "html.parser")
    ranked: list[tuple[int, int, int, Link]] = []
    for i, a in enumerate(soup.find_all("a", href=True)):
        url = _abs(base, a.get("href"))
        if not url or _is_home(url):
            continue
        if not (on_domain(url, domain) or on_booking_host(url)):
            continue
        text = " ".join(a.get_text(" ").split())
        if not text or len(text) > MAX_ANCHOR_CHARS or not _CTA_RX.search(text):
            continue
        region = _region(a)
        if region == "footer":
            continue
        classes = " ".join(a.get("class") or []) if isinstance(a.get("class"), list) \
            else str(a.get("class") or "")
        buttonish = 0 if _BUTTONISH_RX.search(classes) or a.get("role") == "button" else 1
        ranked.append((0 if region == "body" else 1, buttonish, i, Link(url, text, "cta")))
    if not ranked:
        return None
    return min(ranked, key=lambda r: r[:3])[3]


def discover(markup: str | None, base: str, domain: str,
             sitemap: bytes | None = None) -> dict[str, Link]:
    """kind -> the link that names it. Kinds with no matching link are absent."""
    candidates: list[Link] = []
    if markup:
        candidates += homepage_links(markup, base)
    candidates += [Link(u, "", "sitemap") for u in sitemap_urls(sitemap)]
    best: dict[str, tuple[tuple[int, int, int], Link]] = {}
    for order, link in enumerate(candidates):
        if not on_domain(link.url, domain) or _is_home(link.url):
            continue
        kind = classify(link.url, link.text)
        if kind is None:
            continue
        depth = len([s for s in urlsplit(link.url).path.split("/") if s])
        # sitemap entries carry no anchor text; prefer the shallowest path there
        key = (SOURCE_RANK[link.source], depth if link.source == "sitemap" else 0, order)
        if kind not in best or key < best[kind][0]:
            best[kind] = (key, link)
    found = {k: v[1] for k, v in best.items()}
    if markup:
        cta = primary_cta(markup, base, domain)
        if cta is not None:
            found["demo_form"] = cta
    return found

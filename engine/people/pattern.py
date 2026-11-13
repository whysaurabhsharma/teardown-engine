"""Address pattern from public addresses: `te people pattern <company_id>` (Step 42).

Lists the addresses at the company's own registrable domain that already
appear in its captured artifacts (extracted text, and mailto: links in the
structure), splits them into role addresses (sales@, hello@...) and
person-shaped ones (ilsa@, ilsa.marrow@...), and proposes a pattern from the
person-shaped ones when they agree on a shape.

An address built from the pattern is always `low` confidence: only Hunter's
verifier (engine/guard/email_finder.py) may lift it, and mail servers are
never contacted to check it. Nothing is stored: the person types the address
into `te people add` with --email-source pattern.

Reads the database and the cache only: no request, no model call.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from engine.capture import extract_text as X
from engine.guard.cache import Cache
from engine.people.contacts import company
from engine.resolve.domain import registrable_domain

INFERRED_CONFIDENCE = "low"

_ADDRESS = re.compile(r"(?<![\w.%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")

ROLE_LOCALS = frozenset({
    "sales", "info", "hello", "hi", "hey", "contact", "contactus", "support", "help",
    "helpdesk", "care", "customercare", "customers", "customer", "success", "service",
    "careers", "career", "jobs", "hiring", "recruitment", "recruiting", "talent", "hr",
    "people", "team", "admin", "office", "press", "media", "pr", "news", "newsletter",
    "partners", "partner", "partnerships", "alliances", "billing", "accounts", "finance",
    "invoices", "payments", "legal", "privacy", "dpo", "gdpr", "compliance", "grievance",
    "grievances", "security", "abuse", "dmarc", "postmaster", "hostmaster", "webmaster",
    "noreply", "no-reply", "donotreply", "do-not-reply", "marketing", "growth",
    "enquiries", "enquiry", "inquiries", "inquiry", "business", "bd", "founders", "demo",
    "community", "events", "feedback", "ops", "operations", "investors", "ir", "sales-team",
    "mail", "email", "general", "it", "tech", "engineering", "dev", "product", "orders",
})


@dataclass(frozen=True)
class PublicAddress:
    address: str
    kind: str  # artifact kind it was seen in
    url: str
    role: bool
    shape: str | None  # pattern for a person-shaped address, None for role or unreadable


@dataclass
class Proposal:
    pattern: str | None
    note: str
    shapes: Counter[str] = field(default_factory=Counter)


def shape(local: str) -> str | None:
    """The pattern a person-shaped local part suggests, or None if unreadable."""
    local = local.lower()
    m = re.fullmatch(r"([a-z]+)(?:([._-])([a-z]+))?", local)
    if m is None:
        return None
    a, sep, b = m.groups()
    if sep is None:
        return "{first}" if len(a) > 1 else None
    if len(a) == 1 and len(b) > 1:
        return "{f}" + sep + "{last}"
    if len(b) == 1 and len(a) > 1:
        return "{first}" + sep + "{l}"
    if len(a) > 1 and len(b) > 1:
        return "{first}" + sep + "{last}"
    return None


def _texts(conn: sqlite3.Connection, cache: Cache, company_id: str
           ) -> tuple[list[tuple[str, str, str]], int]:
    """[(kind, url, text)] from ok artifacts, and how many ok ones had no text yet."""
    out: list[tuple[str, str, str]] = []
    missing = 0
    for r in conn.execute("SELECT * FROM artifacts WHERE company_id = ? AND outcome = 'ok' "
                          "ORDER BY artifact_id", (company_id,)):
        text = X.stored_text(cache, r)
        if text is None:
            missing += 1
            continue
        out.append((r["kind"], r["url"], text))
        st = X.stored_structure(cache, r) or {}
        hrefs = [str(link.get("href") or "") for link in st.get("links") or []
                 if isinstance(link, dict)]
        mailto = [h[len("mailto:"):].split("?", 1)[0] for h in hrefs
                  if h.lower().startswith("mailto:")]
        if mailto:
            out.append((r["kind"], r["url"], "\n".join(mailto)))
    return out, missing


def public_addresses(conn: sqlite3.Connection, cache: Cache, company_id: str
                     ) -> tuple[list[PublicAddress], int]:
    """Addresses at the company's registrable domain in its captured artifacts,
    first sighting of each, and the count of ok artifacts not yet extracted."""
    c = company(conn, company_id)
    want = registrable_domain(c.domain) if c.domain else None
    texts, missing = _texts(conn, cache, company_id)
    if want is None:
        return [], missing
    seen: dict[str, PublicAddress] = {}
    for kind, url, text in texts:
        for m in _ADDRESS.finditer(text):
            addr = m.group(1).rstrip(".").lower()
            local, _, dom = addr.rpartition("@")
            if registrable_domain(dom) != want or addr in seen:
                continue
            role = local in ROLE_LOCALS or local.split("+", 1)[0] in ROLE_LOCALS
            seen[addr] = PublicAddress(addr, kind, url, role, None if role else shape(local))
    return list(seen.values()), missing


def propose(addresses: list[PublicAddress]) -> Proposal:
    shapes = Counter(a.shape for a in addresses if not a.role and a.shape)
    if not shapes:
        return Proposal(None, "no person-shaped address at this domain; no pattern to infer")
    if len(shapes) > 1:
        return Proposal(None, "the person-shaped addresses disagree on a shape; no single "
                              "pattern", shapes)
    pattern, n = shapes.most_common(1)[0]
    note = f"{n} person-shaped address(es) agree"
    if pattern == "{first}":
        note += "; one word cannot say first name from last name, so check against a known name"
    return Proposal(pattern, note, shapes)


def _ascii(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z]", "", s.lower())


def render(pattern: str, first: str, last: str, domain: str) -> str:
    """The address a pattern gives for a name. Raises ValueError on an empty part."""
    f, la = _ascii(first), _ascii(last)
    if not f or (("{last}" in pattern or "{l}" in pattern) and not la):
        raise ValueError("the name has no letters to build an address from")
    local = (pattern.replace("{first}", f).replace("{last}", la)
             .replace("{f}", f[:1]).replace("{l}", la[:1]))
    return f"{local}@{domain}"

"""The evidence bundle (docs/PLAN.md Step 32): the same artifact kinds for every company.

Every company gets exactly one artifacts row per kind in KINDS, each with an
outcome, so facts are comparable across companies. Checked against
protocol/HAND_NOTES.md: the one hand teardown relied on public DNS, which the
plan's table already lists as `dns`; HAND_NOTES and CONDUCT 1.1 fix its
contents (below). No other page it used is missing.

The DNS lists are fixed and versioned here, once (CONDUCT.md "DNS"). Changing
any list, a kind, a threshold or the discovery rules in discover.py bumps
BUNDLE_VERSION, which re-captures every company.
"""

from __future__ import annotations

BUNDLE_VERSION = "bundle-1.0"

KINDS: tuple[str, ...] = (
    "homepage",   # the root page
    "pricing",    # pricing page if one exists
    "product",    # main product or platform page
    "customers",  # customers or case studies
    "about",      # about or company page
    "careers",    # careers page
    "contact",    # contact page
    "demo_form",  # the page behind the primary CTA
    "compare",    # a "vs" or "alternatives" page on their own site
    "job_post",   # full text of the signal role
    "ats_board",  # all open roles: the board's own JSON
    "dns",        # the fixed DNS inventory below, as JSON
)

# Kinds found by reading the homepage nav, footer and sitemap.xml (discover.py).
DISCOVERED_KINDS: tuple[str, ...] = (
    "pricing", "product", "customers", "about", "careers", "contact", "demo_form", "compare",
)
WEB_KINDS: tuple[str, ...] = tuple(k for k in KINDS if k != "dns")

TEXT_FLOOR = 500  # visible characters; an ok HTML page under this gets one playwright retry
MAX_PLAYWRIGHT_PAGES = 2  # per company
MIN_USABLE = 3  # ok artifacts; fewer drops the company as no_site_evidence (ICP.md)
DROP_REASON = "no_site_evidence"
DROP_STAGE = "capture"

# Scheduling and form hosts a primary CTA may point to. The demo_form kind may
# be fetched from one of these when, and only when, the homepage's primary CTA
# links there; nothing else off the company's registrable domain is fetched.
BOOKING_HOSTS: tuple[str, ...] = (
    "calendly.com", "cal.com", "meetings.hubspot.com", "chilipiper.com",
    "savvycal.com", "zcal.co", "youcanbook.me", "typeform.com", "tally.so",
)

# -- DNS: the fixed list (CONDUCT.md v1.1, HAND_NOTES.md) ------------------------
# Root: SPF and verification records (TXT), MX. DMARC: TXT at _dmarc.
DKIM_SELECTORS: tuple[str, ...] = (
    "google", "selector1", "selector2", "k1", "default", "s1", "s2", "mail",
)
SENDING_SUBDOMAINS: tuple[str, ...] = (
    "em", "mail", "sg", "email", "send", "mg", "news", "mailer", "outbound",
)
# Order matters: CNAME first. NXDOMAIN on it means the name has no records of
# any type (RFC 8020), so the other three are recorded as not_found unsent.
SUBDOMAIN_TYPES: tuple[str, ...] = ("CNAME", "A", "MX", "TXT")


def dns_lookups(domain: str) -> list[tuple[str, str, str]]:
    """Every (name, type, purpose) the dns artifact may query for a domain, in order.

    This is the whole list. Nothing outside it is ever looked up.
    """
    d = domain.strip().lower().rstrip(".")
    out: list[tuple[str, str, str]] = [
        (d, "TXT", "spf"),
        (f"_dmarc.{d}", "TXT", "dmarc"),
        (d, "MX", "mx"),
    ]
    out += [(f"{s}._domainkey.{d}", "TXT", "dkim") for s in DKIM_SELECTORS]
    out += [(f"{sub}.{d}", t, "sending_subdomain") for sub in SENDING_SUBDOMAINS
            for t in SUBDOMAIN_TYPES]
    return out


def homepage_url(domain: str) -> str:
    return f"https://{domain.strip().lower().rstrip('.')}/"

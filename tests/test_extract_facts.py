"""Steps 37-38: E4 fact extractors, patterns.yaml, `te extract facts`.

Invented companies only (Quorvane, Brindlecart; .example domains). Every
extractor has a fixture; every fact's quote is checked here, independently of
base.verify, against the artifact's text, removed_text or structure. No
network and no model calls: the database tests block sockets and the gateway
and check the llm_calls ledger.
"""

from __future__ import annotations

import json
import socket
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from engine import extract as E
from engine.capture import bundle as B
from engine.capture import extract_text as X
from engine.capture.capture import write_rows
from engine.cli import main
from engine.extract import base
from engine.extract import cta, customers, dns, forms, jd, pricing, roles, stack
from engine.extract import run as R
from engine.extract.base import Art, Bundle, FactOut
from engine.guard.cache import Cache
from engine.models import Artifact
from engine.signal import classify as C
from tests.conftest import add_company

FIX = Path(__file__).parent / "fixtures" / "facts"
WHEN = datetime(2026, 11, 12, 8, 0, tzinfo=UTC)
Q_DOMAIN = "quorvane.example"
B_DOMAIN = "brindlecart.example"


# -- fixture builders --------------------------------------------------------------------


def dns_doc(domain: str, answers: dict[tuple[str, str], list[str]],
            exists: frozenset[str] = frozenset(),
            failed: frozenset[tuple[str, str]] = frozenset()) -> dict[str, Any]:
    """A dns artifact JSON as capture writes it, over the fixed lookup list.
    A name with no answer is NXDOMAIN unless it is in `exists` (then NODATA)."""
    have = {n for n, _ in answers} | set(exists)
    lookups, nx = [], set()
    for name, rtype, purpose in B.dns_lookups(domain):
        e = {"name": name, "type": rtype, "purpose": purpose, "records": [], "detail": ""}
        if (name, rtype) in failed:
            e.update(outcome="dead", detail="timeout after 3s")
        elif (name, rtype) in answers:
            e.update(outcome="ok", records=answers[(name, rtype)])
        elif name in nx:
            e.update(outcome="not_found", detail="not sent: name is NXDOMAIN")
        elif name in have:
            e.update(outcome="not_found", detail=f"no {rtype} record")
        else:
            e.update(outcome="not_found", detail="NXDOMAIN")
            nx.add(name)
        lookups.append(e)
    return {"bundle_version": B.BUNDLE_VERSION, "domain": domain, "lookups": lookups}


# Modelled on protocol/HAND_NOTES.md, invented domain: SPF Google + SES with -all,
# DMARC p=reject pct=100 with rua and ruf, no sending subdomain resolves.
Q_DNS = dns_doc(Q_DOMAIN, {
    (Q_DOMAIN, "TXT"): ["v=spf1 include:_spf.google.com include:amazonses.com -all",
                        "google-site-verification=qrv-abc123"],
    (f"_dmarc.{Q_DOMAIN}", "TXT"): [
        f"v=DMARC1; p=reject; pct=100; rua=mailto:dmarc-rua@{Q_DOMAIN}; "
        f"ruf=mailto:dmarc-ruf@{Q_DOMAIN}"],
    (Q_DOMAIN, "MX"): ["1 aspmx.l.google.com", "5 alt1.aspmx.l.google.com"],
    (f"google._domainkey.{Q_DOMAIN}", "TXT"): ["v=DKIM1; k=rsa; p=MIIBIjQRV"],
})

B_DNS = dns_doc(B_DOMAIN, {
    (B_DOMAIN, "TXT"): ["v=spf1 include:spf.protection.outlook.com include:sendgrid.net ~all",
                        "MS=ms44120033"],
    (B_DOMAIN, "MX"): ["0 brindlecart-example.mail.protection.outlook.com"],
    (f"em.{B_DOMAIN}", "CNAME"): ["u7781.wl.sendgrid.net"],
}, exists=frozenset({f"_dmarc.{B_DOMAIN}"}),
    failed=frozenset({(f"selector1._domainkey.{B_DOMAIN}", "TXT"), (f"sg.{B_DOMAIN}", "CNAME")}))


def _bytes(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


def art(kind: str, body: bytes | None = None, url: str | None = None,
        outcome: str = "ok") -> Art:
    url = url or f"https://{Q_DOMAIN}/{kind}"
    if outcome != "ok":
        return Art(kind, url, outcome)
    text, s = X.extract(kind, url, body or b"")
    return Art(kind, url, "ok", text, json.loads(X.structure_bytes(s)))


Q_BODIES = {
    "homepage": ("quorvane_home.html", f"https://{Q_DOMAIN}/"),
    "pricing": ("quorvane_pricing.html", f"https://{Q_DOMAIN}/pricing"),
    "demo_form": ("quorvane_demo.html", f"https://{Q_DOMAIN}/demo"),
    "customers": ("quorvane_customers.html", f"https://{Q_DOMAIN}/customers"),
    "compare": ("quorvane_compare.html", f"https://{Q_DOMAIN}/compare"),
    "job_post": ("quorvane_job.html", f"https://jobs.{Q_DOMAIN}/sdr"),
    "ats_board": ("quorvane_board.json", "https://boards-api.greenhouse.example/quorvane"),
}
SIGNAL = "Founding Sales Development Representative"


def quorvane(classify=C.rule_family, **override: Art) -> Bundle:
    arts = {k: art(k, fx(f), u) for k, (f, u) in Q_BODIES.items()}
    arts["dns"] = art("dns", _bytes(Q_DNS), f"dns:{Q_DOMAIN}")
    arts["product"] = art("product", None, outcome="not_found")
    arts["about"] = art("about", b"<main><p>We started in Pune.</p></main>")
    arts["careers"] = art("careers", b"<main><p>Join us.</p></main>")
    arts["contact"] = art("contact", b"<main><p>Write to us.</p></main>")
    arts.update(override)
    return Bundle("quorvane-example", Q_DOMAIN, "Quorvane", arts, SIGNAL, classify)


B_HOME = b"""<html><head><script src="https://cdn.segment.com/analytics.js/v1/k/analytics.min.js">
</script></head><body><header><nav><a href="/pricing">Pricing</a><a href="/blog">Blog</a>
</nav></header><main><h1>Carts that check themselves out</h1>
<a class="btn" href="/signup">Get started</a></main></body></html>"""
B_PRICING = b"<main><h1>Plans</h1><p>Every plan is built for you. Contact sales for pricing.</p></main>"
B_CAREERS = b"""<main><h1>Careers</h1><ul>
<li><a href="https://jobs.lever.example/x">ignored</a></li>
<li><a href="/careers/account-executive">Account Executive</a></li>
<li><a href="/careers/backend-engineer">Backend Engineer</a></li>
<li><a href="https://apply.workable.com/brindlecart/j/AB12/">Growth Marketer</a></li>
</ul></main><footer><a href="/careers/old-role">Old role</a></footer>"""


def brindlecart(**override: Art) -> Bundle:
    u = f"https://{B_DOMAIN}"
    arts = {
        "homepage": art("homepage", B_HOME, f"{u}/"),
        "pricing": art("pricing", B_PRICING, f"{u}/pricing"),
        "product": art("product", None, f"{u}/product", "not_found"),
        "customers": art("customers", None, f"{u}/customers", "not_found"),
        "about": art("about", None, f"{u}/about", "dead"),
        "careers": art("careers", B_CAREERS, f"{u}/careers"),
        "contact": art("contact", None, f"{u}/contact", "not_found"),
        "demo_form": art("demo_form", None, f"{u}/signup", "blocked"),
        "compare": art("compare", None, f"{u}/compare", "not_found"),
        "job_post": art("job_post", None, "https://www.linkedin.example/jobs/1", "robots_denied"),
        "ats_board": art("ats_board", None, f"{u}/careers", "not_found"),
        "dns": art("dns", _bytes(B_DNS), f"dns:{B_DOMAIN}"),
    }
    arts.update(override)
    return Bundle("brindlecart-example", B_DOMAIN, "Brindlecart", arts, "Account Executive",
                  C.rule_family)


# -- the independent quote check ---------------------------------------------------------


def _strings(o: Any):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def assert_quoted(f: FactOut, b: Bundle) -> None:
    a = b.arts[f.artifact_kind]
    assert f.quote.strip(), f
    q = X.normalise_ws(f.quote)
    if f.quote_source == "text":
        assert f.found or f.fact in ("email_provider", "spf_senders", "dmarc_policy",
                                     "sending_subdomains", "dkim_selectors"), f
        assert a.text[f.char_start:f.char_start + len(f.quote)] == f.quote, f
        assert q in X.normalise_ws(a.text), f
    elif f.quote_source == "removed_text":
        removed = "\n".join(r["text"] for r in a.structure["removed_text"])
        assert q in X.normalise_ws(removed), f
    elif f.quote_source == "structure":
        assert any(q in X.normalise_ws(s) for s in _strings(a.structure)), f
    elif f.quote_source == "not_found":
        assert a.outcome == "not_found" and not f.found and f.quote == f"not_found: {a.kind}"
    elif f.quote_source == "absent":
        assert a.outcome == "ok" and not f.found and f.quote.startswith("absent: ")
    else:
        raise AssertionError(f"unknown quote_source {f.quote_source}")
    assert base.verify(f, a), f


def run_one(fn, b: Bundle) -> list[FactOut]:
    out = fn(b)
    for f in out:
        assert_quoted(f, b)
        assert f.artifact_kind in B.KINDS
    return out


def one(fn, b: Bundle) -> FactOut:
    [f] = run_one(fn, b)
    return f


# -- DNS (built first: HAND_NOTES) -------------------------------------------------------


def test_dns_hand_notes_model() -> None:
    b = quorvane()
    lines = b.arts["dns"].text.split("\n")
    f = one(dns.email_provider, b)
    assert (f.value, f.found) == ("google", True)
    assert f.quote == f"MX {Q_DOMAIN} 1 aspmx.l.google.com" and f.quote in lines

    f = one(dns.spf_senders, b)
    assert f.value == {"senders": ["include:_spf.google.com", "include:amazonses.com"],
                       "ips": [], "all": "-all", "tools": ["google_workspace", "amazon_ses"],
                       "records": 1}
    assert f.quote == (f'TXT {Q_DOMAIN} "v=spf1 include:_spf.google.com '
                       f'include:amazonses.com -all"')

    f = one(dns.dmarc_policy, b)
    assert f.value == {"p": "reject", "sp": None, "pct": 100, "rua": True, "ruf": True}
    assert f.quote.startswith(f'TXT _dmarc.{Q_DOMAIN} "v=DMARC1; p=reject; pct=100;')

    f = one(dns.sending_subdomains, b)
    assert (f.value, f.found, f.quote_source) == ("none", False, "text")
    for sub in B.SENDING_SUBDOMAINS:  # every checked name is in the one quoted block
        assert f"NXDOMAIN {sub}.{Q_DOMAIN}" in f.quote.split("\n")
    assert len(f.quote.split("\n")) == len(B.SENDING_SUBDOMAINS)

    [f] = run_one(dns.dkim_selectors, b)
    assert (f.value, f.found) == ("google", True)
    assert f.quote == f'TXT google._domainkey.{Q_DOMAIN} "v=DKIM1; k=rsa; p=MIIBIjQRV"'


def test_dns_second_company_microsoft_sendgrid_no_dmarc() -> None:
    b = brindlecart()
    assert one(dns.email_provider, b).value == "microsoft"
    f = one(dns.spf_senders, b)
    assert f.value["all"] == "~all"
    assert f.value["tools"] == ["microsoft_365", "sendgrid"]
    f = one(dns.dmarc_policy, b)
    assert (f.value, f.found, f.quote) == ("none", False, f"NODATA TXT _dmarc.{B_DOMAIN}")
    [f] = run_one(dns.sending_subdomains, b)  # em resolves; sg failed but em is enough
    assert (f.value, f.quote) == ("em", f"CNAME em.{B_DOMAIN} u7781.wl.sendgrid.net")
    # selector1 failed: no DKIM selector resolves, but one was unseen, so no absence
    assert dns.dkim_selectors(b) == []


def test_dns_failed_lookup_is_never_an_absence() -> None:
    doc = dns_doc(B_DOMAIN, {}, failed=frozenset({(B_DOMAIN, "TXT"), (B_DOMAIN, "MX"),
                                                  (f"_dmarc.{B_DOMAIN}", "TXT")}))
    b = brindlecart(dns=art("dns", _bytes(doc), f"dns:{B_DOMAIN}"))
    assert dns.spf_senders(b) == []
    assert dns.email_provider(b) == []
    assert dns.dmarc_policy(b) == []


def test_dns_absent_records_quote_the_negative_line() -> None:
    doc = dns_doc(B_DOMAIN, {(B_DOMAIN, "TXT"): ["google-site-verification=zz"]})
    b = brindlecart(dns=art("dns", _bytes(doc), f"dns:{B_DOMAIN}"))
    f = one(dns.spf_senders, b)
    assert (f.value, f.found, f.quote_source) == ("none", False, "absent")
    f = one(dns.email_provider, b)
    assert (f.value, f.found, f.quote) == ("none", False, f"NODATA MX {B_DOMAIN}")
    f = one(dns.dmarc_policy, b)
    assert (f.value, f.quote) == ("none", f"NXDOMAIN _dmarc.{B_DOMAIN}")


@pytest.mark.parametrize("outcome", ["blocked", "dead", "robots_denied", None])
def test_dns_unseen_artifact_writes_nothing(outcome) -> None:
    b = quorvane()
    arts = dict(b.arts)
    if outcome is None:
        del arts["dns"]
    else:
        arts["dns"] = art("dns", None, f"dns:{Q_DOMAIN}", outcome)
    b = Bundle(b.company_id, b.domain, b.name, arts, b.signal_title, b.classify)
    for _, fn, _ in dns.EXTRACTORS:
        assert fn(b) == []


# -- stack_signals and patterns.yaml -----------------------------------------------------


def test_patterns_yaml_is_versioned_and_covers_the_categories() -> None:
    p = base.patterns()
    assert p["version"].startswith("pat-")
    ids = {t["id"] for t in p["tools"]}
    for tool in ("google_workspace", "microsoft_365", "amazon_ses", "sendgrid", "mailgun",
                 "postmark", "mailchimp", "mandrill", "brevo", "zoho_mail", "hubspot",
                 "salesforce", "marketo", "pardot", "intercom", "drift", "calendly",
                 "chili_piper", "outreach", "salesloft", "apollo", "lemlist", "instantly",
                 "smartlead", "segment", "ga4", "mixpanel", "amplitude", "clearbit", "sixsense"):
        assert tool in ids, tool
    cats = {t["category"] for t in p["tools"]}
    assert {"crm", "marketing_automation", "chat", "scheduling", "analytics",
            "sales_engagement", "email_sending"} <= cats
    keys = {"id", "name", "category", "script_hosts", "link_hosts", "txt_prefixes",
            "spf_includes", "mx_hosts", "cname_targets", "cookies"}
    for t in p["tools"]:
        assert set(t) <= keys, t["id"]


def test_stack_signals_from_dns_and_scripts() -> None:
    b = quorvane()
    out = run_one(stack.stack_signals, b)
    got = {f.value["tool"]: (f.artifact_kind, f.value["via"], f.quote) for f in out}
    assert got["google_workspace"][:2] == ("dns", "spf_includes")
    assert got["amazon_ses"][:2] == ("dns", "spf_includes")
    assert got["google_search_console"] == (
        "dns", "txt_prefixes", f'TXT {Q_DOMAIN} "google-site-verification=qrv-abc123"')
    assert got["hubspot"] == ("homepage", "script", "//js.hs-scripts.com/4410021.js")
    assert got["ga4"][:2] == ("homepage", "script")
    assert got["drift"][:2] == ("homepage", "script")
    assert got["calendly"] == ("demo_form", "script",
                               "https://assets.calendly.com/assets/external/widget.js")
    assert "google_tag_manager" not in got  # gtag/js is GA4, not the GTM container
    assert len(out) == len(got)  # one fact per tool


def test_stack_signals_second_company() -> None:
    out = run_one(stack.stack_signals, brindlecart())
    tools = {f.value["tool"] for f in out}
    assert tools == {"microsoft_365", "sendgrid", "segment"}
    ms = next(f for f in out if f.value["tool"] == "microsoft_365")
    assert ms.value["via"] == "spf_includes"  # first evidence in text order


def test_url_matching_is_by_host_suffix_and_path_prefix() -> None:
    assert base.url_matches("https://js.hs-scripts.com/1.js", "hs-scripts.com")
    assert not base.url_matches("https://noths-scripts.com/1.js", "hs-scripts.com")
    assert base.url_matches("https://www.googletagmanager.com/gtag/js?id=G-1",
                            "www.googletagmanager.com/gtag/js")
    assert not base.url_matches("https://www.googletagmanager.com/gtm.js?id=GTM-1",
                                "www.googletagmanager.com/gtag/js")
    assert base.url_matches("//assets.calendly.com/x.js", "calendly.com")


# -- pricing -----------------------------------------------------------------------------


def test_pricing_numbers_beats_contact_sales() -> None:
    f = one(pricing.pricing_model, quorvane())
    assert (f.value, f.artifact_kind, f.found) == ("numbers", "pricing", True)
    assert "₹4,999 / month" in f.quote


def test_pricing_contact_sales() -> None:
    f = one(pricing.pricing_model, brindlecart())
    assert f.value == "contact_sales"
    assert f.quote == "Every plan is built for you. Contact sales for pricing."


def test_pricing_free_or_trial_and_unclassified() -> None:
    b = brindlecart(pricing=art("pricing", b"<main><p>Start free. No card needed.</p></main>"))
    assert one(pricing.pricing_model, b).value == "free_or_trial"
    b = brindlecart(pricing=art("pricing", b"<main><p>Plans that grow with you.</p></main>"))
    f = one(pricing.pricing_model, b)
    assert (f.value, f.found, f.quote_source) == ("unclassified", False, "absent")


def test_pricing_not_found_is_none_and_blocked_is_nothing() -> None:
    b = quorvane(pricing=art("pricing", None, outcome="not_found"))
    f = one(pricing.pricing_model, b)
    assert (f.value, f.found, f.quote, f.quote_source) == (
        "none", False, "not_found: pricing", "not_found")
    for outcome in ("blocked", "dead", "robots_denied"):
        assert pricing.pricing_model(quorvane(pricing=art("pricing", None,
                                                          outcome=outcome))) == []


# -- homepage: primary_cta, segment_pages ------------------------------------------------


def test_primary_cta_demo_from_hero() -> None:
    f = one(cta.primary_cta, quorvane())
    assert (f.value, f.quote, f.quote_source) == ("demo", "Book a demo", "text")


def test_primary_cta_signup_and_none() -> None:
    assert one(cta.primary_cta, brindlecart()).value == "signup"
    home = art("homepage", b"<main><h1>Hello</h1><a href='/about'>About us</a></main>",
               f"https://{B_DOMAIN}/")
    f = one(cta.primary_cta, brindlecart(homepage=home))
    assert (f.value, f.found) == ("none", False)


def test_primary_cta_booking_host_is_demo() -> None:
    home = art("homepage", b"<main><a href='https://calendly.com/brindle/intro'>Get started"
               b"</a></main>", f"https://{B_DOMAIN}/")
    assert one(cta.primary_cta, brindlecart(homepage=home)).value == "demo"


def test_segment_pages_counts_distinct_nav_pages() -> None:
    f = one(cta.segment_pages, quorvane())
    # /industries/dental (nav and footer, once), /industries/diagnostics,
    # /for-staffing-agencies; /solutions/ats is a product page
    assert f.value == 3
    assert (f.quote, f.quote_source) == ("Dental chains", "removed_text")
    f = one(cta.segment_pages, brindlecart())
    assert (f.value, f.found) == (0, False)


# -- demo path: demo_form_fields, has_booking_step ---------------------------------------


def test_demo_form_fields() -> None:
    f = one(forms.demo_form_fields, quorvane())
    assert f.value["count"] == 9  # hidden and submit not counted; footer form ignored
    assert f.value["fields"][:3] == ["first_name", "last_name", "email"]
    assert (f.quote, f.quote_source) == ("First name", "text")


def test_demo_form_fields_absent_not_found_blocked() -> None:
    b = quorvane(demo_form=art("demo_form", b"<main><div id='app'></div></main>"))
    f = one(forms.demo_form_fields, b)
    assert (f.value, f.found, f.quote_source) == ("no_form", False, "absent")
    f = one(forms.demo_form_fields, quorvane(demo_form=art("demo_form", None,
                                                           outcome="not_found")))
    assert (f.value, f.quote_source) == ("none", "not_found")
    assert forms.demo_form_fields(brindlecart()) == []  # blocked


def test_has_booking_step() -> None:
    f = one(forms.has_booking_step, quorvane())
    assert (f.value, f.artifact_kind, f.quote_source) == (True, "demo_form", "structure")
    assert "calendly.com" in f.quote
    # a CTA straight to a booking host counts, quoted from the homepage
    home = art("homepage", b"<main><a href='https://calendly.com/brindle/intro'>Book a demo"
               b"</a></main>", f"https://{B_DOMAIN}/")
    f = one(forms.has_booking_step, brindlecart(homepage=home))
    assert (f.value, f.artifact_kind, f.quote) == (True, "homepage",
                                                   "https://calendly.com/brindle/intro")
    # a seen page with no scheduling tool
    b = quorvane(demo_form=art("demo_form", fx("quorvane_pricing.html")))
    f = one(forms.has_booking_step, b)
    assert (f.value, f.found, f.quote_source) == (False, False, "absent")
    f = one(forms.has_booking_step, quorvane(demo_form=art("demo_form", None,
                                                           outcome="not_found")))
    assert (f.value, f.quote_source) == (False, "not_found")
    assert forms.has_booking_step(brindlecart()) == []  # blocked demo_form


# -- hiring: open_roles_total, gtm_roles_by_family, first_gtm_hire -----------------------


def test_open_roles_total_from_board() -> None:
    f = one(roles.open_roles_total, quorvane())
    assert (f.value, f.quote, f.artifact_kind) == (4, SIGNAL, "ats_board")


def test_open_roles_total_from_careers_when_no_board() -> None:
    f = one(roles.open_roles_total, brindlecart())
    assert (f.value, f.artifact_kind, f.quote) == (3, "careers", "Account Executive")


def test_open_roles_total_empty_board_and_unseen_board() -> None:
    empty = art("ats_board", b'{"jobs": []}', "https://boards.example/x")
    f = one(roles.open_roles_total, quorvane(ats_board=empty))
    assert (f.value, f.found, f.quote_source) == (0, False, "absent")
    assert roles.open_roles_total(quorvane(ats_board=art("ats_board", None,
                                                         outcome="blocked"))) == []


def test_gtm_roles_by_family_rules_only() -> None:
    out = run_one(roles.gtm_roles_by_family, quorvane())
    got = {f.value["family"]: (f.value["count"], f.quote) for f in out}
    assert got == {"sales_lead": (1, SIGNAL),
                   "not_gtm": (2, "Senior Software Engineer"),
                   "unclassified": (1, "Partnerships Lead")}


def test_first_gtm_hire_depends_on_the_other_open_titles() -> None:
    # rules only: "Partnerships Lead" is unplaced, so it cannot be told
    assert roles.first_gtm_hire(quorvane()) == []

    def cached(title: str) -> str | None:
        return C.rule_family(title) or {"Partnerships Lead": "not_gtm"}.get(title)

    f = one(roles.first_gtm_hire, quorvane(classify=cached))
    assert (f.value, f.artifact_kind, f.quote_source) == (True, "job_post", "text")
    assert "Founding" in f.quote

    def sales(title: str) -> str | None:
        return C.rule_family(title) or {"Partnerships Lead": "ae"}.get(title)

    f = one(roles.first_gtm_hire, quorvane(classify=sales))
    assert (f.value, f.artifact_kind, f.quote) == (False, "ats_board", "Partnerships Lead")


def test_first_gtm_hire_absent_and_unseen() -> None:
    post = art("job_post", b"<main><h1>Account Executive</h1><p>Close deals.</p></main>")
    f = one(roles.first_gtm_hire, quorvane(job_post=post))
    assert (f.value, f.found, f.quote_source) == (False, False, "absent")
    assert roles.first_gtm_hire(brindlecart()) == []  # robots_denied post
    # founding wording but the board was blocked: cannot be told
    b = quorvane(ats_board=art("ats_board", None, outcome="blocked"))
    assert roles.first_gtm_hire(b) == []


# -- the job post: jd_tools, jd_target_market, jd_scope_flags ----------------------------


def test_jd_tools() -> None:
    out = run_one(jd.jd_tools, quorvane())
    assert {f.value for f in out} == {"hubspot", "apollo", "excel", "google_sheets"}
    # "outreach to clinic owners" is English, not the tool


def test_jd_tools_lexicon_edges() -> None:
    post = art("job_post", b"<main><p>Our stack: Apollo, Outreach, Clay and Gong.</p>"
               b"<p>Lead outreach to founders. Excel at discovery.</p></main>")
    out = run_one(jd.jd_tools, quorvane(job_post=post))
    assert {f.value for f in out} == {"apollo", "outreach", "clay", "gong"}


def test_jd_target_market_and_scope_flags() -> None:
    b = quorvane()
    regions = {f.value: f.quote for f in run_one(jd.jd_target_market, b)}
    assert set(regions) == {"india", "middle_east"}
    assert "Middle East" in regions["middle_east"]
    flags = {f.value for f in run_one(jd.jd_scope_flags, b)}
    assert flags == {"list_building", "icp_definition", "sequencing", "crm_setup", "quota"}


def test_jd_none_and_unseen() -> None:
    post = art("job_post", b"<main><p>Help us grow.</p></main>")
    b = quorvane(job_post=post)
    for fn in (jd.jd_tools, jd.jd_target_market, jd.jd_scope_flags):
        f = one(fn, b)
        assert (f.value, f.found, f.quote_source) == ("none", False, "absent")
        assert fn(brindlecart()) == []  # robots_denied


# -- customers and compare ---------------------------------------------------------------


def test_customer_regions_reads_text_not_footer() -> None:
    out = run_one(customers.customer_regions, quorvane())
    assert {f.value for f in out} == {"india", "singapore", "uae"}  # footer "UK" not read
    f = one(customers.customer_regions, brindlecart())
    assert (f.value, f.quote_source) == ("none", "not_found")
    b = quorvane(customers=art("customers", None, outcome="robots_denied"))
    assert customers.customer_regions(b) == []


def test_named_competitors() -> None:
    out = run_one(customers.named_competitors, quorvane())
    assert [f.value for f in out] == ["Routely", "Fieldnest"]
    assert out[0].quote == "Quorvane vs Routely: which fits a clinic chain?"
    f = one(customers.named_competitors, brindlecart())
    assert (f.value, f.quote_source) == ("none", "not_found")
    page = art("compare", b"<main><h1>How we compare</h1><p>Quorvane is different.</p></main>")
    f = one(customers.named_competitors, quorvane(compare=page))
    assert (f.value, f.quote_source) == ("none", "absent")


# -- every extractor, together -----------------------------------------------------------


def test_every_extractor_has_a_fixture_and_every_quote_checks() -> None:
    def cached(title: str) -> str | None:
        return C.rule_family(title) or {"Partnerships Lead": "not_gtm"}.get(title)

    b = quorvane(classify=cached)
    facts, unseen, rejected = R.compute(b)
    assert rejected == [] and unseen == []
    assert {f.fact for f in facts} == set(E.FACTS)
    for f in facts:
        assert_quoted(f, b)
    facts, unseen, rejected = R.compute(brindlecart())
    assert rejected == []
    assert set(unseen) == {"demo_form_fields", "has_booking_step", "first_gtm_hire",
                           "jd_tools", "jd_target_market", "jd_scope_flags"}
    # gtm_roles_by_family: the board is not_found, not unseen; dkim_selectors: the
    # artifact was seen, one lookup failed, so no absence and no count here
    for f in facts:
        assert_quoted(f, brindlecart())
        # nothing is ever recorded as absent from an artifact that was not seen
        assert brindlecart().arts[f.artifact_kind].outcome in ("ok", "not_found")


def test_verify_rejects_a_quote_that_is_not_there() -> None:
    b = quorvane()
    a = b.arts["pricing"]
    assert not base.verify(FactOut("pricing_model", "numbers", "pricing", "$99 per seat",
                                   "text", True, 0), a)
    assert not base.verify(FactOut("pricing_model", "none", "pricing", "not_found: pricing",
                                   "not_found", False, 0), a)  # the page was ok
    assert not base.verify(FactOut("x", "y", "pricing", "absent: x in pricing", "absent",
                                   False, 0), art("pricing", None, outcome="blocked"))


def test_lexicons_are_versioned() -> None:
    assert base.lexicons()["version"].startswith("lex-")
    assert E.extractor_version() == (f"{E.FACTS_VERSION}/{base.patterns()['version']}/"
                                     f"{base.lexicons()['version']}")


# -- over the database -------------------------------------------------------------------


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "cache")


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.guard.budget import Budget
    from engine.llm import gateway

    def no_net(*a, **kw):
        raise AssertionError("extract touched the network")

    def no_model(*a, **kw):
        raise AssertionError("extract made a model call")

    monkeypatch.setattr(socket.socket, "connect", no_net)
    monkeypatch.setattr(socket, "create_connection", no_net)
    monkeypatch.setattr(gateway, "call", no_model)
    monkeypatch.setattr(Budget, "charge_model_call", no_model)


QID, BID = "quorvane-example", "brindlecart-example"


def _row(cid: str, kind: str, url: str, outcome: str = "ok",
         body: bytes | None = None, cache: Cache | None = None) -> Artifact:
    sha = cache.put_body(body) if body is not None and cache is not None else None
    return Artifact(company_id=cid, kind=kind, url=url, outcome=outcome, fetched_at=WHEN,
                    body_sha256=sha, fetcher="dns" if kind == "dns" else "http",
                    status=200 if sha else None, bundle_version=B.BUNDLE_VERSION)


@pytest.fixture
def bundles(conn: sqlite3.Connection, cache: Cache) -> None:
    from engine import db
    from engine.models import Signal

    add_company(conn, QID, name="Quorvane", domain=Q_DOMAIN, status="captured", pilot=True)
    add_company(conn, BID, name="Brindlecart", domain=B_DOMAIN, status="captured", pilot=True)
    with conn:
        db.insert(conn, Signal(signal_id="gh-quorvane-1", source="greenhouse",
                               company_name_raw="Quorvane", title=SIGNAL, first_seen=WHEN.date(),
                               company_id=QID))
        conn.execute("UPDATE companies SET best_signal_id = 'gh-quorvane-1' "
                     "WHERE company_id = ?", (QID,))
    rows = [_row(QID, k, u, body=fx(f), cache=cache) for k, (f, u) in Q_BODIES.items()]
    rows += [_row(QID, "dns", f"dns:{Q_DOMAIN}", body=_bytes(Q_DNS), cache=cache),
             _row(QID, "product", f"https://{Q_DOMAIN}/product", "not_found"),
             _row(QID, "about", f"https://{Q_DOMAIN}/about", "blocked",
                  body=b"<p>denied</p>", cache=cache)]
    u = f"https://{B_DOMAIN}"
    rows += [_row(BID, "homepage", f"{u}/", body=B_HOME, cache=cache),
             _row(BID, "pricing", f"{u}/pricing", body=B_PRICING, cache=cache),
             _row(BID, "careers", f"{u}/careers", body=B_CAREERS, cache=cache),
             _row(BID, "customers", f"{u}/customers", "not_found"),
             _row(BID, "compare", f"{u}/compare", "not_found"),
             _row(BID, "demo_form", f"{u}/signup", "blocked", body=b"denied", cache=cache),
             _row(BID, "job_post", "https://www.linkedin.example/jobs/1", "robots_denied"),
             _row(BID, "ats_board", f"{u}/careers", "not_found"),
             _row(BID, "dns", f"dns:{B_DOMAIN}", body=_bytes(B_DNS), cache=cache)]
    write_rows(conn, rows)
    X.run(conn, cache, [QID, BID])


def _cache_title(conn: sqlite3.Connection, title: str, family: str) -> None:
    with conn:
        conn.execute("INSERT INTO llm_calls (stage, company_id, prompt_version, provider, model, "
                     "output) VALUES (?, NULL, ?, 'groq', 'test-model', ?)",
                     (C.STAGE, C.prompt_version_for(title), json.dumps({"role_family": family})))


def _facts(conn: sqlite3.Connection, cid: str | None = None) -> list[tuple]:
    sql = "SELECT * FROM facts" + (" WHERE company_id = ?" if cid else "")
    rows = conn.execute(sql + " ORDER BY fact_id", (cid,) if cid else ()).fetchall()
    return [tuple(r)[1:] for r in rows]


def test_run_writes_quoted_facts_and_advances(conn, cache, bundles, offline) -> None:
    _cache_title(conn, "Partnerships Lead", "not_gtm")
    calls = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    rep = R.run(conn, cache, R.select(conn))
    assert rep.model_calls == 0 and rep.rejected == [] and rep.not_ready == []
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == calls
    assert (rep.changed, rep.advanced) == (2, 2)
    statuses = dict(conn.execute("SELECT company_id, status FROM companies"))
    assert statuses == {QID: "extracted", BID: "extracted"}

    q = {r["fact"] for r in conn.execute("SELECT fact FROM facts WHERE company_id = ?", (QID,))}
    assert q == set(E.FACTS)  # the cached title answer made first_gtm_hire decidable
    first = conn.execute("SELECT * FROM facts WHERE company_id = ? AND fact = 'first_gtm_hire'",
                         (QID,)).fetchone()
    assert first["value"] == "true"

    # every stored quote, checked against what the cache holds for that artifact
    arts = {(r["company_id"], r["kind"]): r for r in conn.execute("SELECT * FROM artifacts")}
    for f in conn.execute("SELECT * FROM facts"):
        a = arts[(f["company_id"], f["artifact_kind"])]
        assert f["extractor_version"] == E.extractor_version()
        assert a["outcome"] in ("ok", "not_found")  # never blocked, dead, robots_denied
        if f["quote_source"] == "text":
            text = X.stored_text(cache, a)
            assert text[f["char_start"]:f["char_start"] + len(f["quote"])] == f["quote"]
        elif f["quote_source"] == "removed_text":
            s = X.stored_structure(cache, a)
            assert X.normalise_ws(f["quote"]) in X.normalise_ws(
                "\n".join(r["text"] for r in s["removed_text"]))
        elif f["quote_source"] == "structure":
            s = X.stored_structure(cache, a)
            assert any(X.normalise_ws(f["quote"]) in X.normalise_ws(x) for x in _strings(s))
        elif f["quote_source"] == "not_found":
            assert a["outcome"] == "not_found" and f["found"] == 0
        else:
            assert f["quote_source"] == "absent" and a["outcome"] == "ok" and f["found"] == 0

    # nothing from Brindlecart's blocked demo_form or robots_denied job post
    kinds = {r["artifact_kind"] for r in conn.execute(
        "SELECT artifact_kind FROM facts WHERE company_id = ?", (BID,))}
    assert not kinds & {"demo_form", "job_post"}
    assert rep.unseen["demo_form_fields"] == 1 and rep.unseen["jd_tools"] == 1

    spf = conn.execute("SELECT * FROM facts WHERE company_id = ? AND fact = 'spf_senders'",
                       (QID,)).fetchone()
    assert json.loads(spf["value"])["all"] == "-all"
    sub = conn.execute("SELECT * FROM facts WHERE company_id = ? "
                       "AND fact = 'sending_subdomains'", (QID,)).fetchone()
    assert (sub["value"], sub["found"]) == ("none", 0)


def test_run_without_cached_titles_counts_unclassified(conn, cache, bundles, offline) -> None:
    rep = R.run(conn, cache, [QID])
    assert rep.model_calls == 0 and rep.unclassified_titles == 1
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM facts WHERE fact = 'first_gtm_hire'").fetchone()[0] \
        == 0
    assert rep.unseen["first_gtm_hire"] == 0  # not unseen: undecidable, and left out


def test_run_is_idempotent(conn, cache, bundles, offline) -> None:
    R.run(conn, cache, R.select(conn))
    before = _facts(conn)
    rep = R.run(conn, cache, R.select(conn))
    assert (rep.changed, rep.unchanged, rep.rows) == (0, 2, 0)
    assert _facts(conn) == before  # same rows, same ids
    assert rep.advanced == 0


def test_changed_extractor_version_rewrites_once(conn, cache, bundles, offline,
                                                 monkeypatch) -> None:
    R.run(conn, cache, [QID])
    n = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    monkeypatch.setattr(E, "FACTS_VERSION", "fx-test-bump")
    rep = R.run(conn, cache, [QID])
    assert rep.changed == 1
    versions = {r[0] for r in conn.execute("SELECT DISTINCT extractor_version FROM facts")}
    assert versions == {E.extractor_version()} and E.extractor_version().startswith("fx-test")
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == n
    assert R.run(conn, cache, [QID]).changed == 0


def test_dry_run_writes_nothing(conn, cache, bundles, offline) -> None:
    rep = R.run(conn, cache, R.select(conn), dry_run=True)
    assert rep.changed == 2 and rep.rows > 0 and rep.advanced == 2
    assert _facts(conn) == []
    assert {r[0] for r in conn.execute("SELECT status FROM companies")} == {"captured"}


def test_not_extracted_company_is_left_alone(conn, cache, bundles, offline) -> None:
    with conn:
        conn.execute("UPDATE artifacts SET extractor_version = 'ex-0.9' "
                     "WHERE company_id = ? AND kind = 'pricing'", (BID,))
    rep = R.run(conn, cache, [BID])
    assert rep.not_ready and "pricing not extracted" in rep.not_ready[0]
    assert _facts(conn, BID) == []
    assert conn.execute("SELECT status FROM companies WHERE company_id = ?",
                        (BID,)).fetchone()[0] == "captured"


def test_hand_company_past_captured_keeps_its_status(conn, cache, bundles, offline) -> None:
    with conn:
        conn.execute("UPDATE companies SET status = 'sent', hand = 1, pilot = 0 "
                     "WHERE company_id = ?", (BID,))
    assert BID in R.select(conn, pilot=True)
    rep = R.run(conn, cache, [BID])
    assert rep.advanced == 0 and _facts(conn, BID)
    assert conn.execute("SELECT status FROM companies WHERE company_id = ?",
                        (BID,)).fetchone()[0] == "sent"


def test_select(conn, cache, bundles) -> None:
    add_company(conn, "early-example", name="Early", status="qualified")
    assert R.select(conn) == [BID, QID]
    assert R.select(conn, limit=1) == [BID]
    with pytest.raises(R.ExtractFactsError):
        R.select(conn, company_id="early-example")
    with pytest.raises(R.ExtractFactsError):
        R.select(conn, company_id="nobody-example")


def test_facts_table_checks_found_and_quote_source(conn) -> None:
    add_company(conn)
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute("INSERT INTO facts (company_id, fact, value, artifact_kind, quote, "
                     "char_start, extractor_version, quote_source) VALUES "
                     "('zentrico-io', 'f', 'v', 'pricing', 'q', 0, 'fx', 'model')")
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute("INSERT INTO facts (company_id, fact, value, artifact_kind, quote, "
                     "char_start, extractor_version, found) VALUES "
                     "('zentrico-io', 'f', 'v', 'pricing', 'q', 0, 'fx', 2)")


def test_cli_extract_facts_runs(conn, db_file, cache, bundles, offline) -> None:
    args = ["extract", "facts", "--db", str(db_file), "--cache-dir", str(cache.root)]
    r = CliRunner().invoke(main, args + ["--dry-run"])
    assert r.exit_code == 0, r.output
    assert "would rewrite 2" in r.output and "model calls this stage: 0" in r.output
    r = CliRunner().invoke(main, args + ["--pilot"])
    assert r.exit_code == 0, r.output
    assert "rewrote 2" in r.output and "moved captured -> extracted 2" in r.output
    assert "skipped, artifact unseen" in r.output
    r = CliRunner().invoke(main, args + ["--company", QID])
    assert r.exit_code == 0, r.output
    assert "rewrote 0" in r.output and "unchanged 1" in r.output
    r = CliRunner().invoke(main, args + ["--company", "nobody-example"])
    assert r.exit_code == 1

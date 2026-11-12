"""Step 28: segment tagging and the location rule (engine/resolve/segment.py).

Companies and sites are invented (*.example). Requests go to an
httpx.MockTransport inside Guard; nothing here reaches the internet, reads
data/ or calls a model.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from engine import db
from engine.cli import main
from engine.guard import http as guard_http
from engine.guard.cache import Cache
from engine.guard.http import Guard
from engine.guard.ratelimit import RateLimiter
from engine.models import Signal
from engine.resolve import segment
from tests.conftest import add_company

TODAY = date(2026, 11, 12)


@pytest.fixture(autouse=True)
def _no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # No socket patch here: asyncio's event loop on Windows opens a local socket
    # pair. Every request goes to the MockTransport in make_guard.
    def no_model(*a: object, **k: object) -> None:
        raise AssertionError("segment tagging must not call a model")

    from engine.llm import gateway

    monkeypatch.setattr(gateway, "call", no_model)


def page(body: str, title: str = "Home") -> bytes:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>".encode()


class Sites:
    """Mock transport: exact URL -> (status, body). robots.txt allows all unless set."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.robots: dict[str, str] = {}
        self.requests: list[str] = []

    def add(self, url: str, body: bytes | str, status: int = 200) -> None:
        self.routes[url] = (status, body.encode() if isinstance(body, str) else body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if request.url.path == "/robots.txt":
            text = self.robots.get(request.url.host, "User-agent: *\nAllow: /\n")
            return httpx.Response(200, text=text)
        status, body = self.routes.get(url, (404, b"not found"))
        return httpx.Response(status, content=body, headers={"content-type": "text/html"})

    def pages(self) -> list[str]:
        return [u for u in self.requests if not u.endswith("/robots.txt")]


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "cache")


@pytest.fixture
def sites() -> Sites:
    return Sites()


def make_guard(cache: Cache, sites: Sites) -> Guard:
    return Guard(cache=cache, limiter=RateLimiter(limits={}, default_rps=50),
                 transport=httpx.MockTransport(sites.handler))


def resolved(conn: sqlite3.Connection, cid: str, name: str, domain: str) -> str:
    return add_company(conn, cid, name=name, domain=domain, status="resolved")


_n = 0


def role(conn: sqlite3.Connection, company_id: str, location: str, *, india: bool | None,
         fresh: bool | None = True, family: str = "sdr") -> str:
    global _n
    _n += 1
    sid = f"greenhouse:{company_id}:{_n}"
    with conn:
        db.insert(conn, Signal(
            signal_id=sid, source="greenhouse", board_slug=company_id,
            company_name_raw=company_id, title="SDR", role_family=family, location=location,
            first_seen=TODAY, company_id=company_id, fresh=fresh, india_located=india))
    return sid


def run(conn: sqlite3.Connection, cache: Cache, sites: Sites,
        seed_sites: dict[str, str] | None = None, **kw: object) -> segment.Report:
    async def go() -> segment.Report:
        targets = segment.select(conn, seed_sites, **kw)  # type: ignore[arg-type]
        async with make_guard(cache, sites) as g:
            return await segment.run(conn, targets, g)

    return asyncio.run(go())


def company(conn: sqlite3.Connection, cid: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM companies WHERE company_id = ?", (cid,)).fetchone()


def evidence(conn: sqlite3.Connection, cid: str) -> list[dict]:
    return json.loads(company(conn, cid)["hq_evidence"])


FOOTER_LEGAL = ('<footer><a href="/contact">Contact us</a> <a href="/legal/terms">Terms</a> '
                '<a href="https://twitter.com/x">Twitter</a></footer>')


# -- markers -------------------------------------------------------------------------


ANCHORS = segment.name_anchors("Zentrico", "zentrico.example")
URL = "https://zentrico.example/"


def markers(text: str) -> list[tuple[str, str]]:
    return [(m.marker, m.side) for m in segment.find_markers(text, URL, ANCHORS)]


def test_cin_is_detected_as_india_incorporation() -> None:
    ms = segment.find_markers("Corporate Identity Number: U72900KA2015PTC123456.", URL, ANCHORS)
    assert [(m.marker, m.side, m.kind) for m in ms] == [("cin", "india", "incorporation")]
    assert "U72900KA2015PTC123456" in ms[0].quote
    # Wrong shape (bad company class, too short, embedded in a longer token) is not a CIN.
    assert markers("U72900KA2015XYZ123456") == []
    assert markers("U72900KA2015PTC12345") == []
    assert markers("XU72900KA2015PTC1234567") == []
    # A listed company's CIN starts with L.
    assert markers("CIN L72200MH2000PLC123456") == [("cin", "india")]


def test_india_phone_versus_us_phone() -> None:
    assert markers("Call us on +91 80 4000 1234") == [("phone:+91", "india")]
    assert markers("Call us on +91-9876543210") == [("phone:+91", "india")]
    assert markers("Call us on +1 (415) 555-0100") == [("phone:+1", "abroad")]
    assert markers("Call us on +1-415-555-0100") == [("phone:+1", "abroad")]
    assert markers("Call us on +44 20 7946 0000") == [("phone:+44", "abroad")]
    # +1 is not read out of +91, nor +91 out of +191.
    assert ("phone:+1", "abroad") not in markers("+91 80 4000 1234")
    assert markers("ref +1915550100") == []


def test_phones_alone_never_decide_except_the_seed_default() -> None:
    ind = segment.find_markers("Call +91 80 4000 1234", URL, ANCHORS)
    us = segment.find_markers("Call +1 (415) 555-0100", URL, ANCHORS)
    assert segment.decide(ind).code == "phone_only"
    assert segment.decide(us).code == "phone_only"
    assert segment.decide(ind, seed=True) == segment.Decision("india", "seed_default")
    assert segment.decide(us, seed=True).code == "phone_only"


def test_entity_must_carry_the_company_name_or_follow_a_copyright() -> None:
    assert markers("We integrate with Salesforce, Inc. and Northwind LLC.") == []
    assert markers("Zentrico Software Pvt. Ltd.") == [("entity:pvt_ltd", "india")]
    assert markers("© 2026 Brightfold Labs Private Limited") == [("entity:pvt_ltd", "india")]
    assert markers("Copyright 2024-2026 Zentrico, Inc. All rights reserved") == [
        ("entity:inc", "abroad")]
    ms = segment.find_markers("© 2026 Zentrico Technologies Pvt. Ltd.", URL, ANCHORS)
    assert ms[0].entity == "Zentrico Technologies Pvt. Ltd."


def test_plain_ltd_counts_only_with_uk_markers() -> None:
    assert markers("Zentrico Ltd") == []
    assert markers("Zentrico Ltd, registered in England and Wales") == [
        ("entity:ltd_uk", "abroad")]


def test_addresses() -> None:
    assert markers("Koramangala, Bengaluru, Karnataka 560034") == [("address:in", "india")]
    assert markers("Baner, Pune - 411 045, India") == [("address:in", "india")]
    assert markers("548 Market St, San Francisco, CA 94104") == [("address:us", "abroad")]
    assert markers("1 Main St, Austin, Texas 78701") == [("address:us", "abroad")]
    assert markers("10 Anson Road, Singapore 079903") == [("address:sg", "abroad")]
    assert markers("1 Old St, London EC1V 9HL") == [("address:uk", "abroad")]
    # A six-digit number with no Indian place beside it is not a PIN.
    assert markers("Trusted by 250000 teams") == []


def test_subsidiary_words_name_the_parent() -> None:
    text = ("Zentrico India Private Limited is a wholly owned subsidiary of Zentrico, Inc., "
            "a Delaware corporation.")
    ms = segment.find_markers(text, URL, ANCHORS)
    roles = {(m.marker, m.role) for m in ms if m.kind == "entity"}
    assert roles == {("entity:pvt_ltd", "subsidiary"), ("entity:inc", "parent")}
    assert segment.decide(ms) == segment.Decision("abroad", "abroad_parent_india_subsidiary")
    # And the other way round: an Indian parent with a US subsidiary.
    ms = segment.find_markers("Zentrico USA Inc. and Zentrico Software Pvt Ltd", URL, ANCHORS)
    assert segment.decide(ms) == segment.Decision("india", "india_parent_abroad_subsidiary")


def test_both_entities_without_words_saying_which_is_parent_is_unclear() -> None:
    ms = segment.find_markers("Zentrico Inc. | Zentrico Software Pvt Ltd", URL, ANCHORS)
    d = segment.decide(ms)
    assert d.hq is None and d.code == "conflicting_entities"
    assert d.review_reason.startswith("segment_unclear: conflicting_entities: ")


def test_entity_against_only_opposite_addresses_is_unclear() -> None:
    ms = segment.find_markers("© 2026 Zentrico Inc. Office: Indiranagar, Bengaluru 560038",
                              URL, ANCHORS)
    assert segment.decide(ms).code == "entity_address_conflict"


def test_discovery_reads_links_and_never_guesses() -> None:
    markup = ('<nav><a href="/pricing">Pricing</a><a href="/about-us">About us</a></nav>'
              '<footer><a href="/privacy-policy">Privacy</a><a href="/s/tos">Terms of Service</a>'
              '<a href="https://zentrico.example/company/legal">Legal</a>'
              '<a href="mailto:hi@zentrico.example">Contact us</a>'
              '<a href="https://other.example/contact">Contact</a>'
              '<a href="https://www.zentrico.example/get-in-touch">Get in touch</a></footer>')
    got = segment.discover_pages(markup, "https://zentrico.example/", "zentrico.example")
    assert got == [
        ("contact", "https://www.zentrico.example/get-in-touch"),
        ("legal", "https://zentrico.example/company/legal"),
        ("terms", "https://zentrico.example/s/tos"),
    ]
    assert segment.discover_pages("<p>no links</p>", URL, "zentrico.example") == []


# -- the run -------------------------------------------------------------------------


def test_pvt_ltd_with_bengaluru_pin_is_in_saas(
    conn: sqlite3.Connection, cache: Cache, sites: Sites, sender: str
) -> None:
    resolved(conn, "kelvori-example", "Kelvori", "kelvori.example")
    role(conn, "kelvori-example", "San Francisco, CA", india=False)  # an India HQ hires anywhere
    sites.add("https://kelvori.example/", page(
        "<p>Kelvori</p><footer>© 2026 Kelvori Technologies Pvt. Ltd. 12 MG Road, Bengaluru, "
        "Karnataka 560001</footer>"))
    rep = run(conn, cache, sites)
    row = company(conn, "kelvori-example")
    assert (row["segment"], row["hq"], row["hq_basis"], row["status"]) == (
        "in_saas", "india", "entity", "resolved")
    assert row["review_reason"] is None
    ev = evidence(conn, "kelvori-example")
    assert {e["marker"] for e in ev} == {"entity:pvt_ltd", "address:in"}
    assert all(e["quote"] and e["url"] == "https://kelvori.example/" for e in ev)
    assert rep.segments == {"in_saas": 1} and rep.changed == 1


def test_delaware_inc_with_india_role_is_us_india_team(
    conn: sqlite3.Connection, cache: Cache, sites: Sites, sender: str
) -> None:
    resolved(conn, "norvale-example", "Norvale", "norvale.example")
    role(conn, "norvale-example", "Bengaluru, India", india=True)
    sites.add("https://norvale.example/", page("<p>Norvale</p>" + FOOTER_LEGAL))
    sites.add("https://norvale.example/legal/terms", page(
        "These terms are between you and Norvale, Inc., a Delaware corporation, "
        "100 Pine St, San Francisco, CA 94111."))
    rep = run(conn, cache, sites)
    row = company(conn, "norvale-example")
    assert (row["segment"], row["hq"], row["hq_basis"]) == ("us_india_team", "abroad", "entity")
    assert rep.segments == {"us_india_team": 1}
    terms = [e for e in evidence(conn, "norvale-example") if e["marker"] == "delaware"]
    assert terms and terms[0]["url"] == "https://norvale.example/legal/terms"
    assert "Delaware corporation" in terms[0]["quote"]


def test_inc_with_no_india_role_drops_out_of_scope(
    conn: sqlite3.Connection, cache: Cache, sites: Sites, sender: str
) -> None:
    resolved(conn, "brontide-example", "Brontide", "brontide.example")
    role(conn, "brontide-example", "London, UK", india=False)
    role(conn, "brontide-example", "Pune, India", india=True, fresh=False)  # stale: no count
    role(conn, "brontide-example", "Pune, India", india=True, family="not_gtm")  # not GTM
    sites.add("https://brontide.example/", page(
        "<footer>© 2026 Brontide, Inc. 9 Elm St, Austin, TX 78701</footer>"))
    rep = run(conn, cache, sites)
    row = company(conn, "brontide-example")
    assert (row["status"], row["drop_reason"], row["drop_stage"]) == (
        "dropped", "out_of_scope", "resolve")
    assert row["segment"] is None and row["hq"] == "abroad"
    assert rep.dropped == 1 and not rep.segments


def test_abroad_with_unfiltered_signals_goes_to_review_not_drop(
    conn: sqlite3.Connection, cache: Cache, sites: Sites, sender: str
) -> None:
    resolved(conn, "brontide-example", "Brontide", "brontide.example")
    role(conn, "brontide-example", "Pune", india=None, fresh=None)
    sites.add("https://brontide.example/", page("<footer>© 2026 Brontide, Inc.</footer>"))
    rep = run(conn, cache, sites)
    row = company(conn, "brontide-example")
    assert row["status"] == "resolved" and row["hq"] == "abroad" and row["segment"] is None
    assert row["review_reason"].startswith("segment_unclear: location_unknown: ")
    assert rep.needs_review == {"location_unknown": 1}


def test_inc_parent_with_pvt_ltd_subsidiary_is_abroad(
    conn: sqlite3.Connection, cache: Cache, sites: Sites, sender: str
) -> None:
    resolved(conn, "orbanta-example", "Orbanta", "orbanta.example")
    role(conn, "orbanta-example", "Hyderabad", india=True)
    sites.add("https://orbanta.example/", page(
        "<p>Orbanta</p><footer><a href='/contact'>Contact</a> © 2026 Orbanta Inc.</footer>"))
    sites.add("https://orbanta.example/contact", page(
        "Orbanta Inc., 1 Market St, San Francisco, CA 94105. India office: Orbanta India "
        "Private Limited, HITEC City, Hyderabad, Telangana 500081. CIN "
        "U72200TG2019PTC654321. +91 40 4000 1234"))
    run(conn, cache, sites)
    row = company(conn, "orbanta-example")
    assert (row["segment"], row["hq"], row["hq_basis"]) == (
        "us_india_team", "abroad", "abroad_parent_india_subsidiary")
    sub = [e for e in evidence(conn, "orbanta-example") if e["marker"] == "entity:pvt_ltd"]
    assert sub[0]["role"] == "subsidiary" and "Orbanta India Private Limited" in sub[0]["entity"]


def test_nothing_found_needs_review(
    conn: sqlite3.Connection, cache: Cache, sites: Sites, sender: str
) -> None:
    resolved(conn, "quillary-example", "Quillary", "quillary.example")
    role(conn, "quillary-example", "Bengaluru", india=True)
    sites.add("https://quillary.example/", page("<p>Quillary helps teams ship.</p>"))
    rep = run(conn, cache, sites)
    row = company(conn, "quillary-example")
    assert row["segment"] is None and row["hq"] is None and row["status"] == "resolved"
    assert row["review_reason"].startswith("segment_unclear: no_evidence: ")
    assert rep.needs_review == {"no_evidence": 1}


def test_seed_defaults_to_india_unless_evidence_says_abroad(
    conn: sqlite3.Connection, cache: Cache, sites: Sites, sender: str
) -> None:
    resolved(conn, "quillary-example", "Quillary", "quillary.example")
    resolved(conn, "parvanta-example", "Parvanta", "parvanta.example")
    role(conn, "parvanta-example", "Remote, US", india=False)
    sites.add("https://www.quillary.example/", page("<p>Quillary helps teams ship.</p>"))
    sites.add("https://www.parvanta.example/", page(
        "<footer>© 2026 Parvanta, Inc. 5 Oak Ave, Palo Alto, CA 94301</footer>"))
    seeds = {"quillary.example": "https://www.quillary.example/",
             "parvanta.example": "https://www.parvanta.example/"}
    run(conn, cache, sites, seeds)
    q, p = company(conn, "quillary-example"), company(conn, "parvanta-example")
    assert (q["segment"], q["hq"], q["hq_basis"]) == ("in_saas", "india", "seed_default")
    assert q["hq_evidence"] == "[]"
    assert (p["status"], p["drop_reason"], p["hq"]) == ("dropped", "out_of_scope", "abroad")
    assert "https://www.quillary.example/" in sites.pages()  # the seed's own site


def test_unreadable_homepage_is_review_even_for_a_seed(
    conn: sqlite3.Connection, cache: Cache, sites: Sites, sender: str
) -> None:
    resolved(conn, "quillary-example", "Quillary", "quillary.example")
    sites.add("https://quillary.example/", "Forbidden", status=403)
    rep = run(conn, cache, sites, {"quillary.example": "https://quillary.example/"})
    row = company(conn, "quillary-example")
    assert row["segment"] is None
    assert row["review_reason"].startswith("segment_unclear: homepage_blocked: ")
    assert rep.needs_review == {"homepage_blocked": 1}


def test_legal_page_is_found_from_the_footer_not_guessed(
    conn: sqlite3.Connection, cache: Cache, sites: Sites, sender: str
) -> None:
    resolved(conn, "norvale-example", "Norvale", "norvale.example")
    role(conn, "norvale-example", "Gurugram", india=True)
    sites.add("https://norvale.example/", page(
        "<p>Norvale</p><footer><a href='/company/legal-notice'>Legal</a></footer>"))
    sites.add("https://norvale.example/company/legal-notice", page(
        "Operated by Norvale LLC, 77 Lake St, Chicago, IL 60601"))
    run(conn, cache, sites)
    assert sites.pages() == ["https://norvale.example/",
                             "https://norvale.example/company/legal-notice"]
    row = company(conn, "norvale-example")
    assert row["segment"] == "us_india_team" and row["hq_basis"] == "entity"


def test_robots_denied_legal_page_is_skipped_and_recorded(
    conn: sqlite3.Connection, cache: Cache, sites: Sites, sender: str
) -> None:
    resolved(conn, "kelvori-example", "Kelvori", "kelvori.example")
    sites.robots["kelvori.example"] = "User-agent: *\nDisallow: /legal\n"
    sites.add("https://kelvori.example/", page("<p>Kelvori</p>" + FOOTER_LEGAL))
    sites.add("https://kelvori.example/legal/terms", page("Kelvori Inc."))  # never read
    sites.add("https://kelvori.example/contact", page(
        "Kelvori Software Private Limited, Baner, Pune 411045"))
    rep = run(conn, cache, sites)
    assert "https://kelvori.example/legal/terms" not in sites.pages()
    out = rep.outcomes[0]
    assert ("terms", "https://kelvori.example/legal/terms", "robots_denied") in out.pages
    assert rep.pages["robots_denied"] == 1
    row = company(conn, "kelvori-example")
    assert (row["segment"], row["hq"]) == ("in_saas", "india")


def test_rerun_changes_nothing_and_skips_reviewed(
    conn: sqlite3.Connection, cache: Cache, sites: Sites, sender: str
) -> None:
    resolved(conn, "kelvori-example", "Kelvori", "kelvori.example")
    resolved(conn, "quillary-example", "Quillary", "quillary.example")
    resolved(conn, "brontide-example", "Brontide", "brontide.example")
    role(conn, "brontide-example", "Berlin", india=False)
    sites.add("https://kelvori.example/", page("© 2026 Kelvori Pvt Ltd"))
    sites.add("https://quillary.example/", page("<p>hello</p>"))
    sites.add("https://brontide.example/", page("© 2026 Brontide GmbH"))
    first = run(conn, cache, sites)
    assert first.changed == 3
    before = [tuple(r) for r in conn.execute("SELECT * FROM companies ORDER BY company_id")]
    n_requests = len(sites.requests)

    second = run(conn, cache, sites)
    assert second.companies == 0 and second.changed == 0
    assert len(sites.requests) == n_requests
    # Naming a reviewed company retries it; same pages (today's cache), same verdict.
    third = run(conn, cache, sites, company_id="quillary-example")
    assert third.companies == 1 and third.changed == 0
    assert len(sites.requests) == n_requests
    after = [tuple(r) for r in conn.execute("SELECT * FROM companies ORDER BY company_id")]
    assert after == before


def test_other_review_reasons_are_left_alone(
    conn: sqlite3.Connection, cache: Cache, sites: Sites, sender: str
) -> None:
    add_company(conn, "kelvori-example", name="Kelvori", domain="kelvori.example",
                status="resolved", review_reason="manual: check headcount")
    sites.add("https://kelvori.example/", page("© 2026 Kelvori Pvt Ltd"))
    run(conn, cache, sites, company_id="kelvori-example")
    row = company(conn, "kelvori-example")
    assert row["segment"] == "in_saas" and row["review_reason"] == "manual: check headcount"


def test_only_resolved_companies_with_a_domain_are_selected(conn: sqlite3.Connection) -> None:
    add_company(conn, "a-example", name="A", domain="a.example")  # sourced
    add_company(conn, "unresolved-x", name="X", domain=None)
    resolved(conn, "b-example", "B", "b.example")
    resolved(conn, "c-example", "C", "c.example")
    with conn:
        conn.execute("UPDATE companies SET segment = 'in_saas' WHERE company_id = 'c-example'")
    assert [t.company_id for t in segment.select(conn)] == ["b-example"]


def test_database_rejects_evidence_without_a_quote(conn: sqlite3.Connection) -> None:
    resolved(conn, "b-example", "B", "b.example")
    bad = json.dumps([{"marker": "cin", "side": "india", "url": "https://b.example/",
                       "quote": " "}])
    with pytest.raises(sqlite3.IntegrityError, match="quote and a url"):
        with conn:
            conn.execute("UPDATE companies SET hq_evidence = ? WHERE company_id = 'b-example'",
                         (bad,))


# -- CLI -----------------------------------------------------------------------------


def test_cli_dry_run_projects_and_writes_nothing(
    db_file: Path, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    resolved(conn, "kelvori-example", "Kelvori", "kelvori.example")
    res = CliRunner().invoke(main, [
        "resolve", "segment", "--dry-run", "--db", str(db_file),
        "--seeds", str(tmp_path / "none.csv"), "--cache-dir", str(tmp_path / "cache")])
    assert res.exit_code == 0, res.output
    assert "companies to tag 1 (seed 0)" in res.output
    assert "dry run: 1 urls" in res.output
    assert company(conn, "kelvori-example")["segment"] is None


def test_cli_run_prints_summary(
    db_file: Path, tmp_path: Path, conn: sqlite3.Connection, sites: Sites, sender: str,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    class MockGuard(Guard):
        def __init__(self, **kw: object) -> None:
            super().__init__(limiter=RateLimiter(limits={}, default_rps=50),
                             transport=httpx.MockTransport(sites.handler), **kw)

    monkeypatch.setattr(guard_http, "Guard", MockGuard)
    resolved(conn, "kelvori-example", "Kelvori", "kelvori.example")
    resolved(conn, "brontide-example", "Brontide", "brontide.example")
    resolved(conn, "quillary-example", "Quillary", "quillary.example")
    role(conn, "brontide-example", "Berlin", india=False)
    sites.add("https://kelvori.example/", page("© 2026 Kelvori Pvt Ltd"))
    sites.add("https://brontide.example/", page("© 2026 Brontide GmbH"))
    sites.add("https://quillary.example/", page("<p>hello</p>"))
    seeds = tmp_path / "seeds.csv"
    seeds.write_text("company,site,careers_url,source\n", encoding="utf-8")
    res = CliRunner().invoke(main, [
        "resolve", "segment", "--db", str(db_file), "--seeds", str(seeds),
        "--cache-dir", str(tmp_path / "cache")])
    assert res.exit_code == 0, res.output
    assert ("companies 3: in_saas 1, us_india_team 0, dropped out_of_scope 1, "
            "needs_review 1, errors 0; changed 3") in res.output
    assert "needs_review no_evidence" in res.output

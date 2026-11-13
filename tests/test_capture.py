"""E3 capture (Steps 32-34) against invented sites on a mock transport. No internet."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from capture_site import (
    FILLER,
    FakeRenderer,
    FakeResolver,
    FakeSites,
    page,
    runtime,
)
from conftest import add_company
from engine import db
from engine.capture import bundle as B
from engine.capture import capture as cap
from engine.capture.discover import classify, discover, primary_cta, sitemap_urls
from engine.cli import main
from engine.guard.browser import fetch_rendered
from engine.guard.budget import Budget, BudgetExceeded
from engine.guard.cache import Cache
from engine.guard.dns import DnsGuard
from engine.guard.ratelimit import RateLimiter
from engine.models import Claim, Diagnosis, Signal

DOMAIN = "zentrico.example"
HOST = DOMAIN
CID = "zentrico-example"
JOB_HOST = "job-boards.greenhouse.io"
JOB_PATH = "/zentrico/jobs/101"
BOARD_HOST = "boards-api.greenhouse.io"
BOARD_PATH = "/v1/boards/zentrico/jobs?content=true"

HOME = page(f"""
<header><nav>
  <a href="/">Zentrico</a>
  <a href="/pricing">Pricing</a>
  <a href="/platform">Platform</a>
  <a href="https://twitter.com/zentrico">Twitter</a>
</nav></header>
<section class="hero"><h1>Close the books in a day</h1>
  <a class="btn btn-primary" href="/demo">Book a demo</a></section>
<p>{FILLER}</p>
<footer>
  <a href="/about">About us</a> <a href="/careers">Careers</a> <a href="/contact">Contact</a>
</footer>
""")
SITEMAP = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://{HOST}/</loc></url>
  <url><loc>https://{HOST}/blog/pricing-tips</loc></url>
  <url><loc>https://{HOST}/customers</loc></url>
  <url><loc>https://{HOST}/vs/ledgerly</loc></url>
</urlset>"""
SITE_PAGES = ("/pricing", "/platform", "/about", "/careers", "/contact", "/demo", "/customers",
              "/vs/ledgerly")


def make_site(*, home: str = HOME, robots: str = "User-agent: *\nDisallow: /private\n",
              skip: tuple[str, ...] = ()) -> FakeSites:
    s = FakeSites()
    s.route(HOST, "/robots.txt", robots, headers={"content-type": "text/plain"})
    s.route(HOST, "/", home)
    s.route(HOST, "/sitemap.xml", SITEMAP, headers={"content-type": "application/xml"})
    for p in SITE_PAGES:
        if p not in skip:
            s.route(HOST, p, page(f"<h1>{p}</h1><p>{FILLER}</p>"))
    s.route(JOB_HOST, "/robots.txt", "", status=404)
    s.route(JOB_HOST, JOB_PATH, page(f"<h1>Founding Account Executive</h1><p>{FILLER}</p>"))
    s.route(BOARD_HOST, "/robots.txt", "", status=404)
    s.route(BOARD_HOST, BOARD_PATH, json.dumps({"jobs": [{"id": 101, "title": "AE"}]}),
            headers={"content-type": "application/json"})
    return s


def make_resolver(*, subdomains: bool = False) -> FakeResolver:
    r = FakeResolver(records={
        (DOMAIN, "TXT"): ["v=spf1 include:_spf.google.com ~all", "google-site-verification=zx1"],
        (f"_dmarc.{DOMAIN}", "TXT"): ["v=DMARC1; p=none"],
        (DOMAIN, "MX"): ["1 aspmx.l.google.com"],
        (f"google._domainkey.{DOMAIN}", "TXT"): ["v=DKIM1; k=rsa; p=MIIB"],
    })
    if subdomains:
        r.records[(f"em.{DOMAIN}", "CNAME")] = ["u1.wl.sendgrid.net"]
    return r


def seed_company(conn: sqlite3.Connection, cid: str = CID, domain: str = DOMAIN, *,
                 status: str = "qualified", signal: bool = True, pilot: bool = True,
                 hand: bool = False) -> str:
    add_company(conn, cid, name="Zentrico", domain=domain, status=status, pilot=pilot,
                hand=hand)
    if signal:
        sid = f"gh-zentrico-101-{cid}"
        with conn:
            db.insert(conn, Signal(signal_id=sid, source="greenhouse", board_slug="zentrico",
                                   company_name_raw="Zentrico", title="Founding Account Executive",
                                   url=f"https://{JOB_HOST}{JOB_PATH}", first_seen=date(2026, 11, 2),
                                   company_id=cid))
            conn.execute("UPDATE companies SET best_signal_id = ? WHERE company_id = ?", (sid, cid))
    return cid


def capture_one(conn: sqlite3.Connection, rt: cap.Runtime, cid: str = CID) -> cap.Report:
    async def go() -> cap.Report:
        try:
            return await cap.run(conn, [cap.load_target(conn, cid)], rt)
        finally:
            await rt.aclose()

    return asyncio.run(go())


def rows(conn: sqlite3.Connection, cid: str = CID) -> dict[str, sqlite3.Row]:
    return {r["kind"]: r for r in conn.execute(
        "SELECT * FROM artifacts WHERE company_id = ?", (cid,))}


def status(conn: sqlite3.Connection, cid: str = CID) -> sqlite3.Row:
    return conn.execute("SELECT status, drop_reason, drop_stage FROM companies "
                        "WHERE company_id = ?", (cid,)).fetchone()


# -- the bundle ---------------------------------------------------------------------------


def test_bundle_has_the_twelve_kinds_and_a_version() -> None:
    assert B.KINDS == ("homepage", "pricing", "product", "customers", "about", "careers",
                       "contact", "demo_form", "compare", "job_post", "ats_board", "dns")
    assert B.BUNDLE_VERSION.startswith("bundle-")
    names = [n for n, _, _ in B.dns_lookups(DOMAIN)]
    assert {DOMAIN, f"_dmarc.{DOMAIN}"} <= set(names)
    assert {f"{s}._domainkey.{DOMAIN}" for s in ("google", "selector1", "selector2", "k1",
                                                 "default", "s1", "s2", "mail")} <= set(names)
    assert {f"{s}.{DOMAIN}" for s in ("em", "mail", "sg", "email", "send", "mg", "news",
                                      "mailer", "outbound")} <= set(names)


# -- discovery (pure) ---------------------------------------------------------------------


def test_discovery_reads_nav_footer_sitemap_and_the_hero_cta() -> None:
    found = discover(HOME, f"https://{HOST}/", DOMAIN, SITEMAP.encode())
    assert {k: (v.url.removeprefix(f"https://{HOST}"), v.source) for k, v in found.items()} == {
        "pricing": ("/pricing", "nav"),
        "product": ("/platform", "nav"),
        "about": ("/about", "footer"),
        "careers": ("/careers", "footer"),
        "contact": ("/contact", "footer"),
        "customers": ("/customers", "sitemap"),
        "compare": ("/vs/ledgerly", "sitemap"),
        "demo_form": ("/demo", "cta"),
    }


def test_discovery_never_guesses_and_stays_on_the_domain() -> None:
    home = page("""<nav><a href="https://ledgerly.example/pricing">Pricing</a></nav>
                   <p>Nothing else here.</p>""")
    assert discover(home, f"https://{HOST}/", DOMAIN, None) == {}
    assert sitemap_urls(b"<sitemapindex><sitemap><loc>https://x.example/a.xml</loc>"
                        b"</sitemap></sitemapindex>") == []
    assert classify(f"https://{HOST}/blog/pricing-tips", "") is None
    assert classify(f"https://{HOST}/zentrico-vs-ledgerly", "") == "compare"
    assert classify(f"https://{HOST}/x", "Plans") == "pricing"
    assert classify(f"https://{HOST}/x", "Case studies") == "customers"
    assert classify(f"https://{HOST}/x", "Alternatives") == "compare"


def test_primary_cta_may_point_to_a_booking_host_but_nowhere_else_off_site() -> None:
    hero = page("""<nav><a href="/signup">Sign up</a></nav>
                   <section><a class="button" href="https://calendly.com/zentrico/demo">
                   Book a demo</a></section>""")
    cta = primary_cta(hero, f"https://{HOST}/", DOMAIN)
    assert cta is not None and cta.url == "https://calendly.com/zentrico/demo"
    elsewhere = page("""<section><a class="btn" href="https://ledgerly.example/demo">Get a demo
                        </a></section><nav><a href="/signup">Sign up</a></nav>""")
    cta = primary_cta(elsewhere, f"https://{HOST}/", DOMAIN)
    assert cta is not None and cta.url == f"https://{HOST}/signup" and cta.source == "cta"


# -- a whole bundle -----------------------------------------------------------------------


def test_normal_site_gets_one_row_per_kind_and_moves_to_captured(
        conn: sqlite3.Connection, sender: str, tmp_path: Path) -> None:
    seed_company(conn)
    sites, resolver, renderer = make_site(), make_resolver(), FakeRenderer()
    rep = capture_one(conn, runtime(tmp_path, sites, resolver, renderer))

    got = rows(conn)
    assert set(got) == set(B.KINDS)
    assert all(r["bundle_version"] == B.BUNDLE_VERSION for r in got.values())
    assert {k: r["outcome"] for k, r in got.items()} == {k: "ok" for k in B.KINDS}
    assert got["pricing"]["url"] == f"https://{HOST}/pricing"
    assert "nav link 'Pricing'" in got["pricing"]["detail"]
    assert "sitemap" in got["customers"]["detail"]
    assert got["demo_form"]["url"] == f"https://{HOST}/demo"
    assert got["job_post"]["url"] == f"https://{JOB_HOST}{JOB_PATH}"
    assert got["ats_board"]["url"] == f"https://{BOARD_HOST}{BOARD_PATH}"
    assert {r["fetcher"] for k, r in got.items() if k != "dns"} == {"http"}
    assert got["dns"]["fetcher"] == "dns" and got["dns"]["url"] == f"dns:{DOMAIN}"
    # raw bytes in the cache, named by their hash
    cache = Cache(tmp_path / "cache")
    assert cache.get_body(got["homepage"]["body_sha256"]) == HOME.encode()
    assert Path(got["homepage"]["cache_path"]).read_bytes() == HOME.encode()
    # no guessed path was requested: only robots, the homepage, sitemap and linked pages
    assert set(sites.paths(HOST)) == {"/robots.txt", "/", "/sitemap.xml", *SITE_PAGES}
    assert "/blog/pricing-tips" not in sites.paths()
    assert renderer.calls == []
    assert status(conn)["status"] == "captured"
    assert rep.states["captured"] == 1 and rep.outcomes["ok"] == 12 and rep.blocked_rate == 0
    assert {h.user_agent for h in sites.hits} == {
        f"teardown-engine/0.1 (+https://github.com/whysaurabhsharma/teardown-engine; "
        f"research; contact {sender})"}


def test_missing_pricing_is_a_not_found_row(conn, sender, tmp_path) -> None:
    seed_company(conn)
    home = HOME.replace('<a href="/pricing">Pricing</a>', "")
    sites = make_site(home=home, skip=("/pricing",))
    capture_one(conn, runtime(tmp_path, sites, make_resolver()))
    r = rows(conn)["pricing"]
    assert r["outcome"] == "not_found" and r["fetcher"] == "none" and r["status"] is None
    assert r["url"] == f"https://{HOST}/"  # where it was looked for
    assert "no matching link" in r["detail"]
    assert "/pricing" not in sites.paths()  # never guessed


def test_job_post_on_linkedin_is_never_fetched(conn, sender, tmp_path) -> None:
    seed_company(conn)
    with conn:
        conn.execute("UPDATE signals SET url = 'https://www.linkedin.com/jobs/view/1' "
                     "WHERE company_id = ?", (CID,))
    sites = make_site()
    capture_one(conn, runtime(tmp_path, sites, make_resolver()))
    r = rows(conn)["job_post"]
    assert r["outcome"] == "robots_denied" and r["fetcher"] == "none"
    assert all("linkedin" not in h.host for h in sites.hits)


def test_403_page_is_blocked_not_an_error(conn, sender, tmp_path) -> None:
    seed_company(conn)
    sites = make_site()
    sites.route(HOST, "/pricing", "Forbidden", status=403)
    rep = capture_one(conn, runtime(tmp_path, sites, make_resolver()))
    r = rows(conn)["pricing"]
    assert r["outcome"] == "blocked" and r["status"] == 403 and r["fetcher"] == "http"
    assert sites.paths(HOST).count("/pricing") == 1  # recorded, not retried
    assert rep.outcomes["blocked"] == 1 and rep.blocked_rate == pytest.approx(1 / 12)
    assert not rep.errors


def test_robots_disallowed_page_is_robots_denied_and_never_requested(conn, sender,
                                                                     tmp_path) -> None:
    seed_company(conn)
    sites = make_site(robots="User-agent: *\nDisallow: /pricing\n")
    capture_one(conn, runtime(tmp_path, sites, make_resolver()))
    r = rows(conn)["pricing"]
    assert r["outcome"] == "robots_denied" and r["status"] is None
    assert "/pricing" not in sites.paths()


def test_blocked_homepage_leaves_undiscovered_kinds_unseen_and_drops(conn, sender,
                                                                    tmp_path) -> None:
    """blocked means unseen: no not_found may rest on a homepage nobody read."""
    seed_company(conn, signal=False)
    sites = make_site()
    sites.route(HOST, "/", "Forbidden", status=403)
    sites.route(HOST, "/sitemap.xml", "Forbidden", status=403)
    rep = capture_one(conn, runtime(tmp_path, sites, FakeResolver(dead=True)))
    got = rows(conn)
    assert set(got) == set(B.KINDS)
    for kind in B.DISCOVERED_KINDS:
        assert got[kind]["outcome"] == "blocked", kind
        assert "not discovered" in got[kind]["detail"]
    assert got["homepage"]["outcome"] == "blocked"
    assert got["job_post"]["outcome"] == "dead"  # no signal URL: unseen, not absent
    assert got["ats_board"]["outcome"] == "blocked"  # careers unseen, so is the board
    assert got["dns"]["outcome"] == "dead"
    assert status(conn)["status"] == "dropped"
    assert tuple(status(conn))[1:] == ("no_site_evidence", "capture")
    assert rep.states["dropped"] == 1


def test_few_usable_artifacts_drop_and_three_capture(conn, sender, tmp_path) -> None:
    # the homepage is readable and links nothing; dns ok; job_post ok => 3 ok -> captured
    seed_company(conn)
    seed_company(conn, "quillmesh-example", "quillmesh.example", signal=False)
    sites = make_site(home=page(f"<p>{FILLER}</p>"))
    sites.route(HOST, "/sitemap.xml", "", status=404)
    sites.route("quillmesh.example", "/robots.txt", "", status=404)
    sites.route("quillmesh.example", "/", page(f"<p>{FILLER}</p>"))
    rt = runtime(tmp_path, sites, make_resolver())

    async def go():
        try:
            return await cap.run(conn, [cap.load_target(conn, CID),
                                        cap.load_target(conn, "quillmesh-example")], rt)
        finally:
            await rt.aclose()

    asyncio.run(go())
    assert sum(r["outcome"] == "ok" for r in rows(conn).values()) == 4  # home, job, board, dns
    assert status(conn)["status"] == "captured"
    q = rows(conn, "quillmesh-example")
    assert sum(r["outcome"] == "ok" for r in q.values()) == 1  # only the homepage
    assert q["pricing"]["outcome"] == "not_found"  # the homepage was read: a real absence
    assert status(conn, "quillmesh-example")["drop_reason"] == "no_site_evidence"


def test_hand_company_past_captured_gets_rows_but_keeps_its_status(conn, sender,
                                                                   tmp_path) -> None:
    seed_company(conn, status="sent", hand=True, pilot=False)
    sites = make_site()
    sites.route(HOST, "/", "Forbidden", status=403)
    rep = capture_one(conn, runtime(tmp_path, sites, FakeResolver(dead=True)))
    assert len(rows(conn)) == 12
    assert status(conn)["status"] == "sent" and rep.states["kept"] == 1


# -- playwright fallback ------------------------------------------------------------------


def test_js_only_homepage_is_rendered_and_its_links_discovered(conn, sender,
                                                               tmp_path) -> None:
    seed_company(conn)
    shell = page('<div id="root"></div><script src="/app.js"></script>')
    sites = make_site(home=shell)
    renderer = FakeRenderer(pages={f"https://{HOST}/": HOME})
    rt = runtime(tmp_path, sites, make_resolver(), renderer)
    capture_one(conn, rt)
    got = rows(conn)
    assert renderer.calls == [f"https://{HOST}/"] and renderer.allowed == [True]
    assert got["homepage"]["fetcher"] == "playwright" and got["homepage"]["outcome"] == "ok"
    assert "playwright retry" in got["homepage"]["detail"]
    assert Cache(tmp_path / "cache").get_body(got["homepage"]["body_sha256"]) == HOME.encode()
    assert got["pricing"]["outcome"] == "ok"  # found in the rendered nav
    # the rendered document request was paced and charged like any other
    assert rt.guard.budget.fetches == rt.guard.requests_made
    assert renderer.closed


def test_playwright_is_capped_at_two_pages_per_company(conn, sender, tmp_path) -> None:
    seed_company(conn)
    sites = make_site()
    for p in SITE_PAGES:
        sites.route(HOST, p, page("<div id='app'></div>"))  # every page is a JS shell
    renderer = FakeRenderer()
    rep = capture_one(conn, runtime(tmp_path, sites, make_resolver(), renderer))
    got = rows(conn)
    assert len(renderer.calls) == 2 == rep.playwright_pages
    assert sum(r["fetcher"] == "playwright" for r in got.values()) == 2
    capped = [k for k, r in got.items() if "playwright cap of 2 reached" in (r["detail"] or "")]
    assert len(capped) == len(SITE_PAGES) - 2
    assert all(got[k]["outcome"] == "ok" and got[k]["fetcher"] == "http" for k in capped)


def test_failed_playwright_retry_keeps_the_plain_fetch(conn, sender, tmp_path) -> None:
    seed_company(conn)
    sites = make_site()
    sites.route(HOST, "/pricing", page("<div id='app'></div>"))
    renderer = FakeRenderer()

    async def boom(url, **kw):
        from engine.guard.browser import BrowserError
        raise BrowserError("cannot start chromium: missing")

    renderer.render = boom  # type: ignore[method-assign]
    capture_one(conn, runtime(tmp_path, sites, make_resolver(), renderer))
    r = rows(conn)["pricing"]
    assert r["outcome"] == "ok" and r["fetcher"] == "http"
    assert "playwright retry dead" in r["detail"]


def test_browser_obeys_robots_before_starting(sender, tmp_path) -> None:
    sites = make_site(robots="User-agent: *\nDisallow: /pricing\n")
    renderer = FakeRenderer()
    rt = runtime(tmp_path, sites, renderer=renderer)

    async def go():
        try:
            first = await fetch_rendered(rt.guard, f"https://{HOST}/pricing", "pricing",
                                         renderer=renderer)
            ok1 = await fetch_rendered(rt.guard, f"https://{HOST}/about", "about",
                                       renderer=renderer)
            ok2 = await fetch_rendered(rt.guard, f"https://{HOST}/about", "about",
                                       renderer=renderer)
            return first, ok1, ok2
        finally:
            await rt.aclose()

    denied, ok1, ok2 = asyncio.run(go())
    assert denied.outcome == "robots_denied"
    assert renderer.calls == [f"https://{HOST}/about"]  # once: the repeat is a cache hit
    assert ok1.outcome == "ok" and ok2.from_cache and ok2.requests == 0


# -- DNS ----------------------------------------------------------------------------------


def _dns(tmp_path: Path, resolver: FakeResolver, **kw):
    d = DnsGuard(cache=Cache(tmp_path / "cache"), resolver=resolver,
                 limiter=RateLimiter(limits={}, default_rps=1000), **kw)
    t = cap.Target(CID, DOMAIN, "qualified")
    return d, asyncio.run(cap.capture_dns(t, d))


def test_dns_inventory_records_present_names_and_per_name_not_found(tmp_path) -> None:
    resolver = make_resolver()
    d, (row, body) = _dns(tmp_path, resolver)
    doc = json.loads(body)
    assert row.outcome == "ok" and row.fetcher == "dns" and doc["domain"] == DOMAIN
    by = {(e["name"], e["type"]): e for e in doc["lookups"]}
    assert by[(DOMAIN, "TXT")]["records"][1].startswith("v=spf1")
    assert by[(f"_dmarc.{DOMAIN}", "TXT")]["records"] == ["v=DMARC1; p=none"]
    assert by[(DOMAIN, "MX")]["records"] == ["1 aspmx.l.google.com"]
    assert by[(f"google._domainkey.{DOMAIN}", "TXT")]["outcome"] == "ok"
    assert by[(f"selector1._domainkey.{DOMAIN}", "TXT")]["outcome"] == "not_found"
    for sub in B.SENDING_SUBDOMAINS:
        for ty in B.SUBDOMAIN_TYPES:
            e = by[(f"{sub}.{DOMAIN}", ty)]
            assert e["outcome"] == "not_found" and e["purpose"] == "sending_subdomain"
    # an NXDOMAIN subdomain costs one query, not four
    assert len(resolver.queries) == 3 + len(B.DKIM_SELECTORS) + len(B.SENDING_SUBDOMAINS)
    assert d.budget.dns_queries == len(resolver.queries)
    assert Cache(tmp_path / "cache").get_body(row.body_sha256) == body


def test_dns_never_enumerates(tmp_path) -> None:
    resolver = make_resolver(subdomains=True)
    _dns(tmp_path, resolver)
    allowed = {(n, t) for n, t, _ in B.dns_lookups(DOMAIN)}
    assert set(resolver.queries) <= allowed
    assert len(resolver.queries) == len(set(resolver.queries))  # nothing twice
    # the one live subdomain got all four types
    assert [t for n, t in resolver.queries if n == f"em.{DOMAIN}"] == list(B.SUBDOMAIN_TYPES)


def test_dns_dead_when_nothing_resolves_and_cached_same_day(tmp_path) -> None:
    resolver = FakeResolver(dead=True)
    d, (row, _) = _dns(tmp_path, resolver)
    assert row.outcome == "dead"
    n = len(resolver.queries)
    again = asyncio.run(cap.capture_dns(cap.Target(CID, DOMAIN, "qualified"), d))
    assert len(resolver.queries) == n and again[0] == row  # same-day cache, no query


def test_dns_budget_ceiling_stops_the_run(tmp_path) -> None:
    with pytest.raises(BudgetExceeded, match="DNS"):
        _dns(tmp_path, make_resolver(), budget=Budget(max_dns_queries=5))


# -- concurrency and pacing ---------------------------------------------------------------


def _pacing_run(conn: sqlite3.Connection, tmp_path: Path, n: int, hosts: list[str]) -> FakeSites:
    sites = FakeSites(delay=0.2)
    for h in hosts:
        sites.route(h, "/", page(f"<p>{FILLER}</p>"))  # robots.txt and sitemap 404
    rt = runtime(tmp_path / f"run{n}", sites, FakeResolver(dead=True), max_in_flight=n,
                 limiter=RateLimiter(limits={}))  # the real 1 rps per host

    async def go():
        try:
            targets = [cap.load_target(conn, h.replace(".", "-")) for h in hosts]
            return await cap.run(conn, targets, rt, max_in_flight=n)
        finally:
            await rt.aclose()

    asyncio.run(go())
    return sites


def test_max_in_flight_raises_cross_host_concurrency_never_per_host_rate(conn, sender,
                                                                         tmp_path) -> None:
    hosts = ["zentrico.example", "quillmesh.example", "brightloom.example"]
    for h in hosts:
        add_company(conn, h.replace(".", "-"), domain=h, status="sent", hand=True)
    serial = _pacing_run(conn, tmp_path, 1, hosts)
    wide = _pacing_run(conn, tmp_path, 4, hosts)
    assert serial.max_in_flight == 1
    assert wide.max_in_flight >= 2
    for sites in (serial, wide):
        for h in hosts:
            starts = [x.start for x in sites.hits if x.host == h]
            assert len(starts) == 3  # robots.txt, homepage, sitemap.xml
            gaps = [b - a for a, b in zip(starts, starts[1:])]
            assert min(gaps) >= 0.95, (h, gaps)  # 1 rps per host; 0.05 s timer slack


# -- idempotence, selection, CLI ----------------------------------------------------------


def test_same_day_rerun_sends_nothing_and_changes_nothing(conn, sender, tmp_path) -> None:
    seed_company(conn)
    sites, resolver = make_site(), make_resolver()
    shell = page("<div id='root'></div>")
    sites.route(HOST, "/about", shell)
    renderer = FakeRenderer()
    cache = Cache(tmp_path / "cache")
    capture_one(conn, runtime(tmp_path, sites, resolver, renderer, cache=cache))
    before = [tuple(r) for r in conn.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
    hits, queries, renders = len(sites.hits), len(resolver.queries), len(renderer.calls)

    rt = runtime(tmp_path, sites, resolver, renderer, cache=cache)

    async def again():
        try:
            return await cap.capture_company(cap.load_target(conn, CID), rt)
        finally:
            await rt.aclose()

    res = asyncio.run(again())
    assert cap.write_rows(conn, res.rows.values()) == 0
    assert (len(sites.hits), len(resolver.queries), len(renderer.calls)) == (hits, queries,
                                                                             renders)
    assert [tuple(r) for r in conn.execute("SELECT * FROM artifacts ORDER BY artifact_id")] \
        == before
    assert cap.select(conn, pilot=True).complete == [CID]  # and the CLI would skip it


def test_zero_model_calls(conn, sender, tmp_path, monkeypatch) -> None:
    from engine.llm import gateway

    def no(*a, **kw):
        raise AssertionError("capture made a model call")

    monkeypatch.setattr(gateway, "call", no)
    monkeypatch.setattr(Budget, "charge_model_call", no)
    seed_company(conn)
    capture_one(conn, runtime(tmp_path, make_site(), make_resolver()))
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    """Route the CLI's runtime to the fake sites; returns the holder of what it built."""
    built: dict[str, object] = {"sites": make_site(), "resolver": make_resolver(),
                                "renderer": FakeRenderer(), "calls": 0}

    def fake(cache, budget, max_in_flight=4):
        built["calls"] = int(built["calls"]) + 1  # type: ignore[arg-type]
        return runtime(tmp_path, built["sites"], built["resolver"], built["renderer"],
                       max_in_flight=max_in_flight, budget=budget, cache=cache)

    monkeypatch.setattr(cap, "make_runtime", fake)
    return built


def test_cli_dry_run_projects_and_sends_nothing(db_file, sender, cli_env, tmp_path) -> None:
    c = db.connect(db_file)
    seed_company(c)
    c.close()
    res = CliRunner().invoke(main, ["capture", "--pilot", "--dry-run", "--db", str(db_file),
                                    "--cache-dir", str(tmp_path / "cache")])
    assert res.exit_code == 0, res.output
    assert "dry run: 1 companies" in res.output and "DNS queries" in res.output
    assert cli_env["calls"] == 0 and cli_env["sites"].hits == []  # type: ignore[union-attr]
    assert db.connect(db_file).execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_cli_pilot_run_prints_the_summary_then_skips(db_file, sender, cli_env,
                                                     tmp_path) -> None:
    c = db.connect(db_file)
    seed_company(c)
    seed_company(c, "brightloom-example", "brightloom.example", signal=False, pilot=False)
    c.close()
    args = ["capture", "--pilot", "--db", str(db_file), "--cache-dir", str(tmp_path / "cache")]
    res = CliRunner().invoke(main, args)
    assert res.exit_code == 0, res.output
    assert "artifacts by outcome: ok 12" in res.output
    assert "blocked rate 0.0% (0 of 12 artifacts)" in res.output
    assert "captured 1" in res.output
    c = db.connect(db_file)
    assert c.execute("SELECT COUNT(*) FROM artifacts WHERE company_id = 'brightloom-example'"
                     ).fetchone()[0] == 0  # not pilot, not hand
    c.close()
    res = CliRunner().invoke(main, args)
    assert "already captured at" in res.output and "companies to capture 0" in res.output


def test_cli_refuses_a_company_before_qualified(db_file, sender, cli_env, tmp_path) -> None:
    c = db.connect(db_file)
    seed_company(c, status="resolved")
    c.close()
    res = CliRunner().invoke(main, ["capture", "--company", CID, "--db", str(db_file)])
    assert res.exit_code == 1 and "capture runs on qualified" in res.output


def test_recheck_without_a_diagnosis_does_nothing(db_file, sender, cli_env) -> None:
    c = db.connect(db_file)
    seed_company(c)
    c.close()
    res = CliRunner().invoke(main, ["capture", "--recheck", CID, "--db", str(db_file)])
    assert res.exit_code == 1 and "no diagnosis" in res.output
    assert cli_env["calls"] == 0


def test_recheck_refetches_only_cited_artifacts_fresh(db_file, sender, cli_env,
                                                      tmp_path) -> None:
    c = db.connect(db_file)
    seed_company(c)
    c.close()
    args = ["--db", str(db_file), "--cache-dir", str(tmp_path / "cache")]
    assert CliRunner().invoke(main, ["capture", "--company", CID, *args]).exit_code == 0
    c = db.connect(db_file)
    with c:
        db.insert(c, Diagnosis(
            company_id=CID, prompt_version="judge-test", attempted=True, gap_type="G2",
            need="one path", cannot_see="internal", claims=[
                Claim(text="Pricing shows prices", artifact_kind="pricing", found=True,
                      url=f"https://{HOST}/pricing", quote="/pricing"),
                Claim(text="The CTA is a demo form", artifact_kind="demo_form", found=True,
                      url=f"https://{HOST}/demo", quote="/demo"),
            ]))
    c.close()
    sites = cli_env["sites"]
    sites.hits.clear()  # type: ignore[union-attr]
    queries = len(cli_env["resolver"].queries)  # type: ignore[union-attr]
    res = CliRunner().invoke(main, ["capture", "--recheck", CID, *args])
    assert res.exit_code == 0, res.output
    # same day, yet fetched again: fresh, and only the two cited pages
    assert sorted(sites.paths()) == ["/demo", "/pricing"]  # type: ignore[union-attr]
    assert len(cli_env["resolver"].queries) == queries  # type: ignore[union-attr]
    assert "rechecked 2 artifacts fresh" in res.output
    c = db.connect(db_file)
    assert status(c)["status"] == "captured"
    c.close()


def test_recheck_of_a_not_found_kind_rereads_the_homepage(conn, sender, tmp_path) -> None:
    seed_company(conn)
    home = HOME.replace('<a href="/pricing">Pricing</a>', "")
    sites = make_site(home=home, skip=("/pricing",))
    cache = Cache(tmp_path / "cache")
    capture_one(conn, runtime(tmp_path, sites, make_resolver(), cache=cache))
    assert rows(conn)["pricing"]["outcome"] == "not_found"
    sites.route(HOST, "/", HOME)  # pricing appears in the nav
    sites.route(HOST, "/pricing", page(f"<p>{FILLER}</p>"))
    sites.hits.clear()
    rt = runtime(tmp_path, sites, make_resolver(), cache=cache)

    async def go():
        try:
            return await cap.recheck(conn, CID, ["pricing"], rt)
        finally:
            await rt.aclose()

    rc = asyncio.run(go())
    assert sorted(sites.paths()) == ["/", "/pricing", "/sitemap.xml"]
    assert rows(conn)["pricing"]["outcome"] == "ok" and rc.changed == 1
    # the homepage was re-read, but its row was not cited, so it is not rewritten
    assert rows(conn)["homepage"]["body_sha256"] == hashlib.sha256(home.encode()).hexdigest()


# -- the optional real browser ------------------------------------------------------------


@pytest.mark.browser
def test_real_chromium_renders_a_js_only_page(server, sender, tmp_path) -> None:
    """Runs only with `pytest -m browser` and chromium installed; local server only."""
    from engine.guard.browser import PlaywrightRenderer
    from engine.guard.http import Guard

    text = "Rendered by script. " * 40
    server.route("/app", body=page(f"<div id='root'></div><script>document.getElementById("
                                   f"'root').textContent = {json.dumps(text)};</script>"),
                 headers={"Content-Type": "text/html"})
    g = Guard(cache=Cache(tmp_path / "cache"), limiter=RateLimiter(limits={}, default_rps=50))
    renderer = PlaywrightRenderer()

    async def go():
        try:
            return await fetch_rendered(g, server.url("/app"), "homepage", renderer=renderer)
        finally:
            await renderer.aclose()
            await g.aclose()

    r = asyncio.run(go())
    assert r.outcome == "ok" and "Rendered by script." in r.text
    assert all(h.user_agent and "teardown-engine/0.1" in h.user_agent
               for h in server.hits if h.path in ("/app", "/robots.txt"))

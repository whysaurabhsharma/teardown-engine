"""Step 26: resolve each company's domain (engine/resolve/domain.py).

Companies and sites are invented (*.example). Requests go to an
httpx.MockTransport inside Guard (and the search hook); nothing here reaches
the internet or reads data/.
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
from engine.guard import search_api
from engine.guard.cache import Cache
from engine.guard.http import Guard
from engine.guard.ratelimit import RateLimiter
from engine.models import Signal
from engine.resolve import domain
from engine.signal.seeds import Seed

TODAY = date(2026, 11, 12)

ZENTRICO = Seed("Zentrico", "https://zentrico.example", "https://zentrico.example/careers", "test")
KELVORI = Seed("Kelvori Pvt Ltd", "https://www.kelvori.example",
               "https://www.kelvori.example/jobs", "test")


def html(title: str, body: str = "", site_name: str | None = None) -> bytes:
    og = f'<meta property="og:site_name" content="{site_name}">' if site_name else ""
    return f"<html><head><title>{title}</title>{og}</head><body>{body}</body></html>".encode()


class Sites:
    """Mock transport: exact URL -> (status, body, content type); robots.txt allows all."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes, str]] = {}
        self.requests: list[str] = []

    def add(self, url: str, body: bytes | str | dict, status: int = 200,
            ctype: str = "text/html") -> None:
        if isinstance(body, dict):
            body, ctype = json.dumps(body).encode(), "application/json"
        elif isinstance(body, str):
            body = body.encode()
        self.routes[url] = (status, body, ctype)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        status, body, ctype = self.routes.get(url, (404, b"not found", "text/plain"))
        return httpx.Response(status, content=body, headers={"content-type": ctype})

    def pages(self) -> list[str]:
        return [u for u in self.requests if not u.endswith("/robots.txt")]


@pytest.fixture(autouse=True)
def _no_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(search_api.EXA_ENV, raising=False)
    monkeypatch.delenv(search_api.TAVILY_ENV, raising=False)
    monkeypatch.setattr(search_api, "transport", None)


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "cache")


def make_guard(cache: Cache, sites: Sites) -> Guard:
    return Guard(cache=cache, limiter=RateLimiter(limits={}, default_rps=50),
                 transport=httpx.MockTransport(sites.handler))


def add_signal(conn: sqlite3.Connection, signal_id: str, source: str, board_slug: str | None,
               name: str, url: str, *, description: str | None = None,
               role_family: str = "sdr", fresh: bool = True) -> None:
    with conn:
        db.insert(conn, Signal(
            signal_id=signal_id, source=source, board_slug=board_slug, company_name_raw=name,
            title="Founding SDR", role_family=role_family, url=url, first_seen=TODAY,
            description_text=description, fresh=fresh,
        ))


def run(conn: sqlite3.Connection, cache: Cache, sites: Sites, seeds: list[Seed] = (),
        **kw) -> domain.Report:
    async def go() -> domain.Report:
        groups = domain.select_groups(conn, list(seeds), cache, **kw)
        async with make_guard(cache, sites) as g:
            return await domain.run(conn, groups, g, cache=cache)

    return asyncio.run(go())


def company(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM companies WHERE company_id = ?", (company_id,)).fetchone()


def owner(conn: sqlite3.Connection, signal_id: str) -> str | None:
    return conn.execute("SELECT company_id FROM signals WHERE signal_id = ?",
                        (signal_id,)).fetchone()[0]


# -- normalising ---------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [
    ("https://zentrico.example", "zentrico.example"),
    ("https://www.zentrico.example/about?x=1", "zentrico.example"),
    ("https://careers.eu.zentrico.example/jobs", "zentrico.example"),
    ("WWW.Zentrico.Example.", "zentrico.example"),
    ("https://jobs.kelvori.co.in/open", "kelvori.co.in"),
    ("www.kelvori.co.in", "kelvori.co.in"),
    ("https://shop.parvanta.co.uk:8443/", "parvanta.co.uk"),
    ("http://127.0.0.1:8000/", None),
    ("localhost", None),
    ("", None),
])
def test_registrable_domain(value: str, expected: str | None) -> None:
    assert domain.registrable_domain(value) == expected


def test_tldextract_never_touches_the_network() -> None:
    assert tuple(domain._EXTRACT.suffix_list_urls) == ()


def test_company_id_follows_the_plan_convention() -> None:
    assert domain.company_id_for("acme.com") == "acme-com"
    assert domain.company_id_for("kelvori.co.in") == "kelvori-co-in"


def test_text_domains_reads_urls_and_emails_not_bare_words() -> None:
    text = ("Learn more at https://www.norvale.example/about. Write to jobs@norvale.example; "
            "we use ASP.NET and Node.js.")
    assert domain.text_domains(text) == ["norvale.example", "norvale.example"]


def test_ats_and_social_domains_are_never_candidates() -> None:
    links = ["greenhouse.io", "linkedin.com", "norvale.example", "norvale.example", "bit.ly"]
    assert domain.rank_candidates("Norvale", [], links) == ["norvale.example"]


# -- fuzzy threshold -----------------------------------------------------------------


@pytest.mark.parametrize("name, text, ok", [
    ("Zentrico", "Zentrico | Sales software", True),  # subset of the title
    ("Zentrico Pvt Ltd", "Zentrico", True),  # legal words dropped
    ("parvanta-labs", "Parvanta Labs | Home", True),  # a board slug as name
    ("Zentrico", "Zentrica", True),  # 87.5
    ("Quillbyte", "Quilbite", False),  # 82.4
    ("Zentrico", "Zentrxca", False),  # 75
    ("Tessaro", "Totally Different Corp", False),
    ("Zentrico", "", False),
])
def test_name_match(name: str, text: str, ok: bool) -> None:
    assert domain.name_matches(name, text) is ok


def test_threshold_is_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(domain, "name_score", lambda a, b: 85.0)
    assert domain.name_matches("a", "b")
    monkeypatch.setattr(domain, "name_score", lambda a, b: 84.99)
    assert not domain.name_matches("a", "b")


def test_page_names_reads_title_and_og_site_name() -> None:
    page = html("Welcome  to\n the future", site_name="Zentrico").decode()
    assert domain.page_names(page) == ["Welcome to the future", "Zentrico"]


# -- the seed path -------------------------------------------------------------------


@pytest.fixture
def seed_sites() -> Sites:
    s = Sites()
    s.add("https://zentrico.example", html("Zentrico | Outbound for Indian SMBs"))
    s.add(ZENTRICO.careers_url,
          '<a href="https://job-boards.greenhouse.io/zentrico">Open roles</a>')
    s.add("https://www.kelvori.example", html("Home", site_name="Ombrelle Analytics"))
    return s


def test_seed_careers_and_handoff_signals_resolve_to_the_seed_site(
    conn: sqlite3.Connection, cache: Cache, seed_sites: Sites, sender: str
) -> None:
    add_signal(conn, "cr-zentrico.example-1", "careers", "zentrico.example", "Zentrico",
               "https://zentrico.example/careers/sdr")
    add_signal(conn, "gh-zentrico-7", "greenhouse", "zentrico", "Zentrico",
               "https://job-boards.greenhouse.io/zentrico/jobs/7")
    # The hand-off is re-derived from the careers page `te signal careers` cached.
    asyncio.run(_fetch_once(cache, seed_sites, ZENTRICO.careers_url))

    rep = run(conn, cache, seed_sites, [ZENTRICO])

    assert (rep.verified, sum(rep.needs_review.values()), rep.errors) == (1, 0, [])
    row = company(conn, "zentrico-example")
    assert (row["domain"], row["name"], row["status"], row["review_reason"]) == (
        "zentrico.example", "Zentrico", "resolved", None)
    assert owner(conn, "cr-zentrico.example-1") == owner(conn, "gh-zentrico-7") == "zentrico-example"
    # No board metadata is fetched for a seed: its site is already known.
    assert not any("boards-api" in u for u in seed_sites.pages())


async def _fetch_once(cache: Cache, sites: Sites, url: str) -> None:
    async with make_guard(cache, sites) as g:
        await g.fetch(url, "careers_page")


def test_seed_with_a_name_mismatch_needs_review(
    conn: sqlite3.Connection, cache: Cache, seed_sites: Sites, sender: str
) -> None:
    add_signal(conn, "cr-kelvori.example-1", "careers", "kelvori.example", "Kelvori Pvt Ltd",
               "https://www.kelvori.example/jobs/ae")
    rep = run(conn, cache, seed_sites, [KELVORI])
    assert rep.needs_review == {"name_mismatch": 1}
    row = company(conn, "kelvori-example")
    assert row["status"] == "sourced"
    assert row["review_reason"].startswith("name_mismatch: 'Ombrelle Analytics' scored")


# -- the ATS path --------------------------------------------------------------------


GH_META = "https://boards-api.greenhouse.io/v1/boards/norvale"
LV_META = "https://jobs.lever.co/quillbyte"
AB_META = "https://jobs.ashbyhq.com/tessaro"
WK_META = "https://apply.workable.com/api/v1/widget/accounts/brontide?details=true"


@pytest.fixture
def ats_sites() -> Sites:
    s = Sites()
    # Greenhouse: the board's content links the site; the site's careers page links back.
    s.add(GH_META, {"name": "Norvale", "content":
                    "&lt;p&gt;Read about us at &lt;a href=&quot;https://www.norvale.example/"
                    "&quot;&gt;our site&lt;/a&gt;&lt;/p&gt;"})
    s.add("https://norvale.example/", html(
        "Norvale - Revenue intelligence", '<nav><a href="/company/careers">Careers</a></nav>'))
    s.add("https://norvale.example/company/careers",
          '<iframe src="https://job-boards.greenhouse.io/embed/job_board?for=norvale"></iframe>')
    # Lever: the board page's logo links the site; the name matches, no linkback.
    s.add(LV_META, '<div class="main-header"><a class="main-header-logo" '
                   'href="https://quillbyte.example"><img alt="Quillbyte"></a></div>')
    s.add("https://quillbyte.example/", html("Quillbyte", '<a href="/careers">Join us</a>'))
    s.add("https://quillbyte.example/careers", "<h1>No open roles</h1>")
    # Ashby: no board page; the job post names the site; the homepage links the board
    # but carries another name.
    s.add("https://tessaro.example/", html(
        "Totally Different Corp", '<a href="https://jobs.ashbyhq.com/tessaro">Jobs</a>'))
    # Workable: the account JSON names the company but no site.
    s.add(WK_META, {"name": "Brontide", "jobs": []})
    return s


def test_ats_board_with_name_match_and_careers_linkback_is_verified(
    conn: sqlite3.Connection, cache: Cache, ats_sites: Sites, sender: str
) -> None:
    add_signal(conn, "gh-norvale-1", "greenhouse", "norvale", "Norvale",
               "https://job-boards.greenhouse.io/norvale/jobs/1")
    rep = run(conn, cache, ats_sites)
    assert (rep.verified, rep.errors) == (1, [])
    row = company(conn, "norvale-example")
    assert (row["domain"], row["name"], row["status"]) == ("norvale.example", "Norvale", "resolved")
    assert owner(conn, "gh-norvale-1") == "norvale-example"
    assert "https://norvale.example/company/careers" in ats_sites.pages()


def test_name_match_without_linkback_needs_review(
    conn: sqlite3.Connection, cache: Cache, ats_sites: Sites, sender: str
) -> None:
    add_signal(conn, "lv-quillbyte-1", "lever", "quillbyte", "quillbyte",
               "https://jobs.lever.co/quillbyte/1")
    rep = run(conn, cache, ats_sites)
    assert rep.needs_review == {"no_linkback": 1}
    row = company(conn, "quillbyte-example")
    assert row["status"] == "sourced"
    assert row["review_reason"] == "no_linkback: careers page does not link to lever:quillbyte"
    assert owner(conn, "lv-quillbyte-1") == "quillbyte-example"


def test_linkback_without_name_match_needs_review(
    conn: sqlite3.Connection, cache: Cache, ats_sites: Sites, sender: str
) -> None:
    add_signal(conn, "ab-tessaro-1", "ashby", "tessaro", "tessaro",
               "https://jobs.ashbyhq.com/tessaro/1",
               description="Tessaro builds billing tools. https://tessaro.example/about")
    rep = run(conn, cache, ats_sites)
    assert rep.needs_review == {"name_mismatch": 1}
    assert company(conn, "tessaro-example")["status"] == "sourced"


def test_no_candidate_and_no_search_provider_needs_review_without_guessing(
    conn: sqlite3.Connection, cache: Cache, ats_sites: Sites, sender: str
) -> None:
    add_signal(conn, "wk-brontide-1", "workable", "brontide", "Brontide",
               "https://apply.workable.com/brontide/j/1")
    rep = run(conn, cache, ats_sites)
    assert rep.needs_review == {"search_not_configured": 1}
    pid = "unresolved-workable-brontide"
    row = company(conn, pid)
    assert (row["domain"], row["name"], row["status"]) == (None, "Brontide", "sourced")
    assert row["review_reason"].startswith("search_not_configured:")
    assert owner(conn, "wk-brontide-1") == pid
    assert ats_sites.pages() == [WK_META]  # no homepage was guessed at


def test_search_not_configured_raises() -> None:
    with pytest.raises(search_api.NotConfigured):
        asyncio.run(search_api.search_site("Brontide"))


def test_placeholder_is_retried_once_search_is_configured(
    conn: sqlite3.Connection, cache: Cache, ats_sites: Sites, sender: str,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    add_signal(conn, "wk-brontide-1", "workable", "brontide", "Brontide",
               "https://apply.workable.com/brontide/j/1")
    run(conn, cache, ats_sites)
    assert owner(conn, "wk-brontide-1") == "unresolved-workable-brontide"

    queries: list[dict] = []

    def exa(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-key"
        queries.append(json.loads(request.content))
        return httpx.Response(200, json={"results": [
            {"url": "https://www.linkedin.com/company/brontide"},
            {"url": "https://www.brontide.example/"}]})

    monkeypatch.setenv(search_api.EXA_ENV, "test-key")
    monkeypatch.setattr(search_api, "transport", httpx.MockTransport(exa))
    ats_sites.add("https://brontide.example/", html(
        "Brontide", '<a href="https://apply.workable.com/brontide/">Careers</a>'))
    rep = run(conn, cache, ats_sites)

    assert rep.verified == 1
    assert [q["query"] for q in queries] == ["Brontide official website"]
    assert owner(conn, "wk-brontide-1") == "brontide-example"
    assert company(conn, "brontide-example")["status"] == "resolved"
    assert company(conn, "unresolved-workable-brontide") is None  # empty placeholder removed


# -- rows, idempotence, models -------------------------------------------------------


def test_two_boards_on_one_domain_make_one_company(
    conn: sqlite3.Connection, cache: Cache, ats_sites: Sites, sender: str
) -> None:
    add_signal(conn, "gh-norvale-1", "greenhouse", "norvale", "Norvale",
               "https://job-boards.greenhouse.io/norvale/jobs/1")
    add_signal(conn, "mn-norvale-x", "manual", None, "Norvale",
               "https://norvale.example/company/careers#sdr")
    rep = run(conn, cache, ats_sites)
    assert rep.groups == 2 and not rep.errors
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
    assert owner(conn, "mn-norvale-x") == owner(conn, "gh-norvale-1") == "norvale-example"


def test_only_fresh_gtm_unattached_signals_are_read(
    conn: sqlite3.Connection, cache: Cache, ats_sites: Sites, sender: str
) -> None:
    add_signal(conn, "gh-norvale-1", "greenhouse", "norvale", "Norvale", "u", fresh=False)
    add_signal(conn, "gh-norvale-2", "greenhouse", "norvale", "Norvale", "u",
               role_family="not_gtm")
    assert domain.select_groups(conn, [], cache) == []


def test_rerun_changes_nothing_and_fetches_nothing(
    conn: sqlite3.Connection, cache: Cache, ats_sites: Sites, sender: str
) -> None:
    add_signal(conn, "gh-norvale-1", "greenhouse", "norvale", "Norvale",
               "https://job-boards.greenhouse.io/norvale/jobs/1")
    add_signal(conn, "lv-quillbyte-1", "lever", "quillbyte", "quillbyte",
               "https://jobs.lever.co/quillbyte/1")
    add_signal(conn, "wk-brontide-1", "workable", "brontide", "Brontide",
               "https://apply.workable.com/brontide/j/1")
    run(conn, cache, ats_sites)
    before = [tuple(r) for r in conn.execute("SELECT * FROM companies ORDER BY company_id")]
    sigs = [tuple(r) for r in conn.execute("SELECT * FROM signals ORDER BY signal_id")]
    n = len(ats_sites.requests)

    rep = run(conn, cache, ats_sites)

    assert rep.groups == 0
    assert len(ats_sites.requests) == n
    assert [tuple(r) for r in conn.execute("SELECT * FROM companies ORDER BY company_id")] == before
    assert [tuple(r) for r in conn.execute("SELECT * FROM signals ORDER BY signal_id")] == sigs


def test_resolve_makes_zero_model_calls(
    conn: sqlite3.Connection, cache: Cache, ats_sites: Sites, seed_sites: Sites, sender: str,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from engine.llm import gateway

    def no_model(*a: object, **k: object) -> None:
        raise AssertionError("resolve called a model")

    monkeypatch.setattr(gateway, "call", no_model)
    ats_sites.routes.update(seed_sites.routes)
    add_signal(conn, "gh-norvale-1", "greenhouse", "norvale", "Norvale",
               "https://job-boards.greenhouse.io/norvale/jobs/1")
    add_signal(conn, "cr-zentrico.example-1", "careers", "zentrico.example", "Zentrico",
               "https://zentrico.example/careers/sdr")
    add_signal(conn, "wk-brontide-1", "workable", "brontide", "Brontide",
               "https://apply.workable.com/brontide/j/1")
    rep = run(conn, cache, ats_sites, [ZENTRICO])
    assert rep.groups == 3 and not rep.errors
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0


def test_company_and_limit_select_groups(conn: sqlite3.Connection, cache: Cache) -> None:
    add_signal(conn, "gh-norvale-1", "greenhouse", "norvale", "Norvale", "u")
    add_signal(conn, "cr-zentrico.example-1", "careers", "zentrico.example", "Zentrico", "u")
    keys = [g.key for g in domain.select_groups(conn, [ZENTRICO], cache)]
    assert keys == ["greenhouse:norvale", "seed:zentrico.example"]
    assert [g.key for g in domain.select_groups(conn, [ZENTRICO], cache, limit=1)] == keys[:1]
    one = domain.select_groups(conn, [ZENTRICO], cache, company_id="zentrico-example")
    assert [g.key for g in one] == ["seed:zentrico.example"]
    one = domain.select_groups(conn, [ZENTRICO], cache, company_id="greenhouse:norvale")
    assert [g.key for g in one] == ["greenhouse:norvale"]


# -- CLI -----------------------------------------------------------------------------


def test_cli_dry_run_projects_and_sends_nothing(
    db_file: Path, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    add_signal(conn, "gh-norvale-1", "greenhouse", "norvale", "Norvale", "u")
    res = CliRunner().invoke(main, [
        "resolve", "--dry-run", "--db", str(db_file), "--seeds", str(tmp_path / "none.csv"),
        "--cache-dir", str(tmp_path / "cache")])
    assert res.exit_code == 0, res.output
    assert "companies to resolve 1: ats 1" in res.output
    assert "dry run: 1 urls" in res.output
    assert "search provider not configured" in res.output
    assert owner(conn, "gh-norvale-1") is None


def test_cli_run_prints_summary(
    db_file: Path, tmp_path: Path, conn: sqlite3.Connection, ats_sites: Sites, sender: str,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    class MockGuard(Guard):
        def __init__(self, **kw: object) -> None:
            super().__init__(limiter=RateLimiter(limits={}, default_rps=50),
                             transport=httpx.MockTransport(ats_sites.handler), **kw)

    monkeypatch.setattr(guard_http, "Guard", MockGuard)
    add_signal(conn, "gh-norvale-1", "greenhouse", "norvale", "Norvale",
               "https://job-boards.greenhouse.io/norvale/jobs/1")
    add_signal(conn, "wk-brontide-1", "workable", "brontide", "Brontide",
               "https://apply.workable.com/brontide/j/1")
    res = CliRunner().invoke(main, [
        "resolve", "--db", str(db_file), "--seeds", str(tmp_path / "none.csv"),
        "--cache-dir", str(tmp_path / "cache")])
    assert res.exit_code == 0, res.output
    assert "companies 2: verified 1, needs_review 1, errors 0; signals attached 2" in res.output
    assert "needs_review search_not_configured" in res.output

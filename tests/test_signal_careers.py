"""The Indian SaaS path (docs/PLAN.md Step 23): seed list loader, careers-page
collector with ATS hand-off, and manual entry. Companies and domains are
invented (*.example). Requests go to an httpx.MockTransport inside Guard;
nothing here reaches the internet or reads data/.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from engine.cli import main
from engine.guard import http as guard_http
from engine.guard.cache import Cache
from engine.guard.http import Guard
from engine.guard.ratelimit import RateLimiter
from engine.signal import careers, greenhouse, manual
from engine.signal.boards import store
from engine.signal.seeds import Seed, SeedsError, load_seeds, parse_seeds

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 12)
GH_BOARD = greenhouse.board_url("zentrico")

ZENTRICO = Seed("Zentrico", "https://zentrico.example", "https://zentrico.example/careers", "test")
PARVANTA = Seed("Parvanta Labs", "https://www.parvanta.example",
                "https://www.parvanta.example/jobs", "test")
KELVORI = Seed("Kelvori", "https://kelvori.example",
               "https://kelvori.example/company/careers", "test")
BRONTIDE = Seed("Brontide", "https://brontide.example", "https://brontide.example/careers", "test")
SUMBARA = Seed("Sumbara", "https://sumbara.example", "https://sumbara.example/careers", "test")
SEEDS = [ZENTRICO, PARVANTA, KELVORI, BRONTIDE, SUMBARA]


def page(name: str) -> bytes:
    return (FIXTURES / "careers" / name).read_bytes()


class Sites:
    """Mock transport: exact URL -> (status, body, content type). robots.txt
    allows everything except /careers on sumbara.example."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes, str]] = {}
        self.requests: list[str] = []

    def add(self, url: str, body: bytes, status: int = 200, ctype: str = "text/html") -> None:
        self.routes[url] = (status, body, ctype)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if request.url.path == "/robots.txt":
            rules = "Disallow: /careers\n" if request.url.host == "sumbara.example" else "Allow: /\n"
            return httpx.Response(200, text="User-agent: *\n" + rules)
        status, body, ctype = self.routes.get(url, (404, b"not found", "text/plain"))
        return httpx.Response(status, content=body, headers={"content-type": ctype})

    def pages(self) -> list[str]:
        return [u for u in self.requests if not u.endswith("/robots.txt")]


@pytest.fixture
def sites() -> Sites:
    s = Sites()
    s.add(ZENTRICO.careers_url, page("zentrico_handoff.html"))
    s.add(GH_BOARD, (FIXTURES / "ats" / "greenhouse_zentrico.json").read_bytes(),
          ctype="application/json")
    s.add(PARVANTA.careers_url, page("parvanta_jsonld.html"))
    s.add(KELVORI.careers_url, page("kelvori_plain.html"))
    s.add(BRONTIDE.careers_url, b"<h1>Forbidden</h1>", status=403)
    s.add(SUMBARA.careers_url, b"<h1>Account Executive</h1>")
    return s


def make_guard(tmp_path: Path, sites: Sites) -> Guard:
    return Guard(
        cache=Cache(tmp_path / "cache"),
        limiter=RateLimiter(limits={}, default_rps=50),
        transport=httpx.MockTransport(sites.handler),
    )


def collect(tmp_path: Path, sites: Sites, seeds: list[Seed]) -> list[careers.SeedResult]:
    async def go() -> list[careers.SeedResult]:
        async with make_guard(tmp_path, sites) as g:
            return [await careers.collect_seed(s, g, today=TODAY) for s in seeds]

    return asyncio.run(go())


def one(tmp_path: Path, sites: Sites, seed: Seed) -> careers.SeedResult:
    return collect(tmp_path, sites, [seed])[0]


# -- seeds loader -------------------------------------------------------------------


def test_example_seed_file_loads() -> None:
    seeds = load_seeds(Path(__file__).parent.parent / "docs" / "seeds.example.csv")
    assert [s.company for s in seeds] == ["Zentrico", "Parvanta Labs", "Kelvori"]
    assert all(s.domain.endswith(".example") for s in seeds)
    assert seeds[1].domain == "parvanta.example"  # www. dropped


def test_seed_file_bom_extra_columns_and_blank_rows(tmp_path: Path) -> None:
    f = tmp_path / "seeds.csv"
    f.write_text("﻿Company,careers_url,site,source,notes\n"
                 "Zentrico,https://zentrico.example/careers,https://zentrico.example,,x\n"
                 ",,,,\n", encoding="utf-8")
    [s] = load_seeds(f)
    assert s == Seed("Zentrico", "https://zentrico.example",
                     "https://zentrico.example/careers", "", line=2)


def test_seed_file_missing_columns() -> None:
    with pytest.raises(SeedsError, match="missing column.*careers_url, source"):
        parse_seeds(["company,site\n", "Zentrico,https://zentrico.example\n"])


def test_seed_file_reports_every_bad_row() -> None:
    rows = [
        "company,site,careers_url,source\n",
        "Zentrico,https://zentrico.example,https://zentrico.example/careers,list\n",
        ",https://kelvori.example,https://kelvori.example/careers,list\n",
        "Parvanta,parvanta.example,https://parvanta.example/jobs,list\n",
        "Brontide,https://brontide.example,ftp://brontide.example/careers,list\n",
        "Zentrico again,https://WWW.Zentrico.example/,https://zentrico.example/jobs,list\n",
        "Sumbara,https://sumbara.example,https://www.linkedin.com/company/sumbara/jobs,list\n",
    ]
    with pytest.raises(SeedsError) as e:
        parse_seeds(rows)
    assert e.value.problems == [
        "line 3: company is blank",
        "line 4: site is not an http(s) URL: 'parvanta.example'",
        "line 5: careers_url is not an http(s) URL: 'ftp://brontide.example/careers'",
        "line 6: duplicate site zentrico.example (first on line 2)",
        "line 7: careers_url is on LinkedIn or Naukri, which code never reads; "
        "add those roles by hand with `te signal add`",
    ]


# -- ATS hand-off -------------------------------------------------------------------


def test_page_linking_to_greenhouse_hands_off(sender, tmp_path: Path, sites: Sites) -> None:
    r = one(tmp_path, sites, ZENTRICO)
    assert r.outcome == "ats_handoff" and r.found == []
    assert [(b.source, b.board_slug, b.state) for b in r.boards] == [
        ("greenhouse", "zentrico", "jobs")]
    # the greenhouse collector's own signals, unchanged; the page's h2 is not read
    assert [s.signal_id for s in r.signals] == ["gh-zentrico-4411001", "gh-zentrico-4411027"]
    assert {s.source for s in r.signals} == {"greenhouse"}
    assert sites.pages() == [ZENTRICO.careers_url, GH_BOARD]


def test_careers_url_on_an_ats_host_is_read_directly(sender, tmp_path: Path, sites: Sites) -> None:
    seed = Seed("Zentrico", "https://zentrico.example",
                "https://boards.greenhouse.io/zentrico", "test")
    r = one(tmp_path, sites, seed)
    assert r.outcome == "ats_handoff" and len(r.signals) == 2
    assert sites.pages() == [GH_BOARD]  # the board page itself is never fetched


@pytest.mark.parametrize("markup, expected", [
    ('<a href="https://job-boards.greenhouse.io/zentrico/jobs/4411001">x</a>',
     [("greenhouse", "zentrico")]),
    ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=zentrico"></script>',
     [("greenhouse", "zentrico")]),
    ('<iframe src="//boards.greenhouse.io/embed/job_board?for=zentrico&amp;b=x"></iframe>',
     [("greenhouse", "zentrico")]),
    ('<a href="https://jobs.lever.co/quillbyte/1f2e">x</a>'
     '<a href="https://jobs.lever.co/quillbyte">y</a>', [("lever", "quillbyte")]),
    ('fetch("https://api.ashbyhq.com/posting-api/job-board/norvale")', [("ashby", "norvale")]),
    ('<a href="https://jobs.ashbyhq.com/norvale/abc">x</a>', [("ashby", "norvale")]),
    ('<a href="https://apply.workable.com/tessaro/j/9A8B7C/">x</a>', [("workable", "tessaro")]),
    ('<a href="https://apply.workable.com/j/9A8B7C">x</a>', []),
    ('<a href="https://kelvori.example/jobs">x</a>', []),
])
def test_find_boards(markup: str, expected: list[tuple[str, str]]) -> None:
    assert careers.find_boards(markup) == expected


# -- reading the page itself --------------------------------------------------------


def test_jsonld_jobposting(sender, tmp_path: Path, sites: Sites) -> None:
    r = one(tmp_path, sites, PARVANTA)
    assert r.outcome == "ok"
    rows = sorted((s.model_dump(mode="json") for s in r.signals), key=lambda d: d["title"])
    ae, revops = rows
    assert ae == {
        "signal_id": careers.page_signal_id(
            "parvanta.example", "Founding Account Executive",
            "https://www.parvanta.example/jobs/founding-ae"),
        "source": "careers",
        "board_slug": "parvanta.example",
        "company_name_raw": "Parvanta Labs",
        "title": "Founding Account Executive",
        "role_family": None,
        "offer_type": None,
        "location": "Pune, MH, IN",
        "posted_at": None,  # datePosted is not used; first_seen stands in
        "url": "https://www.parvanta.example/jobs/founding-ae",
        "first_seen": "2026-11-12",
        "company_id": None,
        "description_text": "Close our first 20 mid-market customers.",
    }
    assert ae["signal_id"].startswith("cr-parvanta.example-") and len(ae["signal_id"]) == 32
    assert revops["title"] == "Revenue Operations Lead"
    assert revops["url"] == PARVANTA.careers_url  # no link of its own
    assert revops["location"] == "India; Remote"


def test_plain_html_list_of_roles(sender, tmp_path: Path, sites: Sites) -> None:
    r = one(tmp_path, sites, KELVORI)
    assert r.outcome == "ok"
    got = sorted((s.title, s.url) for s in r.signals)
    assert got == [
        ("Growth Marketing", "https://kelvori.example/jobs/482"),  # by its job URL
        ("Sales Development Representative", KELVORI.careers_url),  # heading, bare "Apply" link
        ("Senior Backend Engineer", "https://kelvori.example/company/careers/backend"),
    ]
    # header, footer, social, mailto, "Apply", "Our culture", "Sales", "Open positions"
    # and "Life at Kelvori" are not roles
    assert all(s.source == "careers" and s.posted_at is None for s in r.signals)


def test_embedded_json_jobs() -> None:
    markup = """<html><body>
    <script id="__NEXT_DATA__" type="application/json">
    {"props": {"pageProps": {
      "jobs": [
        {"jobId": 7, "title": "Inside Sales Associate", "applyUrl": "/apply/7",
         "location": {"name": "Noida"}},
        {"title": "Customer Success", "department": "CS", "location": "Remote"}
      ],
      "nav": [{"title": "Pricing", "href": "/pricing"}],
      "post": {"title": "Our Series A", "author": "Team"}
    }}}
    </script></body></html>"""
    found = careers.read_page(markup, "https://zentrico.example/careers")
    assert [(f.title, f.url, f.location, f.how) for f in found] == [
        ("Inside Sales Associate", "https://zentrico.example/apply/7", "Noida", "json"),
        ("Customer Success", None, "Remote", "json"),
    ]


# -- outcomes -----------------------------------------------------------------------


def test_blocked_page_is_an_outcome(sender, tmp_path: Path, sites: Sites) -> None:
    r = one(tmp_path, sites, BRONTIDE)
    assert (r.outcome, r.status, r.signals) == ("blocked", 403, [])


def test_robots_denied_page_is_not_fetched(sender, tmp_path: Path, sites: Sites) -> None:
    r = one(tmp_path, sites, SUMBARA)
    assert (r.outcome, r.signals) == ("robots_denied", [])
    assert SUMBARA.careers_url not in sites.requests


def test_missing_page_is_not_found(sender, tmp_path: Path, sites: Sites) -> None:
    seed = Seed("Tavrin", "https://tavrin.example", "https://tavrin.example/careers", "test")
    assert one(tmp_path, sites, seed).outcome == "not_found"


# -- storing and re-runs ------------------------------------------------------------


def test_second_run_inserts_zero_rows(
    sender, tmp_path: Path, sites: Sites, conn: sqlite3.Connection
) -> None:
    first = collect(tmp_path, sites, SEEDS)
    assert [r.outcome for r in first] == [
        "ats_handoff", "ok", "ok", "blocked", "robots_denied"]
    n = store(conn, [s for r in first for s in r.signals])
    assert n == 2 + 2 + 3
    # a fresh cache, so the second run re-reads every page over the network
    again = collect(tmp_path / "second", sites, SEEDS)
    assert store(conn, [s for r in again for s in r.signals]) == 0
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 7
    assert conn.execute(
        "SELECT count(*) FROM signals WHERE source = 'careers' AND posted_at IS NULL"
    ).fetchone()[0] == 5


# -- manual entry -------------------------------------------------------------------


def test_manual_signal_id_is_deterministic() -> None:
    a = manual.make_signal(company="Kelvori ", title="Founding  AE",
                           url="https://kelvori.example/jobs/9", today=TODAY)
    b = manual.make_signal(company="kelvori", title="founding AE",
                           url="https://kelvori.example/jobs/9", today=date(2026, 12, 1))
    assert a.signal_id == b.signal_id and a.signal_id.startswith("mn-kelvori-")
    assert a.source == "manual" and a.title == "Founding AE"
    with pytest.raises(ValueError):
        manual.make_signal(company="Kelvori", title="AE", url="kelvori.example", today=TODAY)


def test_cli_add_is_idempotent(db_file: Path) -> None:
    args = ["signal", "add", "--company", "Kelvori", "--title", "Founding Account Executive",
            "--url", "https://kelvori.example/jobs/9", "--location", "Gurugram",
            "--posted-at", "2026-11-01", "--db", str(db_file)]
    first = CliRunner().invoke(main, args)
    assert first.exit_code == 0, first.output
    assert first.output.startswith("added mn-kelvori-")
    second = CliRunner().invoke(main, args)
    assert second.exit_code == 0 and second.output.startswith("already recorded mn-kelvori-")
    c = sqlite3.connect(db_file)
    rows = c.execute("SELECT source, title, location, posted_at FROM signals").fetchall()
    c.close()
    assert rows == [("manual", "Founding Account Executive", "Gurugram", "2026-11-01")]


def test_cli_add_rejects_bad_input(db_file: Path) -> None:
    res = CliRunner().invoke(main, ["signal", "add", "--company", "Kelvori", "--title", "AE",
                                    "--url", "not a url", "--db", str(db_file)])
    assert res.exit_code == 1 and "http(s) URL" in res.output
    res = CliRunner().invoke(main, ["signal", "add", "--company", "Kelvori", "--title", "AE",
                                    "--url", "https://kelvori.example", "--posted-at", "1/11/26",
                                    "--db", str(db_file)])
    assert res.exit_code == 2


# -- CLI: te signal careers ---------------------------------------------------------


def write_seeds(tmp_path: Path, seeds: list[Seed]) -> Path:
    f = tmp_path / "seeds.csv"
    f.write_text("company,site,careers_url,source\n" + "".join(
        f"{s.company},{s.site},{s.careers_url},{s.source}\n" for s in seeds), encoding="utf-8")
    return f


def test_cli_careers_dry_run_sends_nothing(tmp_path: Path) -> None:
    seeds_file = write_seeds(tmp_path, SEEDS)
    res = CliRunner().invoke(main, ["signal", "careers", "--seeds", str(seeds_file),
                                    "--dry-run", "--cache-dir", str(tmp_path / "cache")])
    assert res.exit_code == 0, res.output
    assert "5 urls" in res.output and "10 requests projected" in res.output
    limited = CliRunner().invoke(main, ["signal", "careers", "--seeds", str(seeds_file),
                                        "--dry-run", "--limit", "2",
                                        "--cache-dir", str(tmp_path / "cache")])
    assert "2 urls" in limited.output


def test_cli_careers_missing_or_bad_seed_file(tmp_path: Path) -> None:
    res = CliRunner().invoke(main, ["signal", "careers", "--seeds", str(tmp_path / "none.csv"),
                                    "--dry-run"])
    assert res.exit_code == 1 and "docs/seeds.example.csv" in res.output
    bad = tmp_path / "bad.csv"
    bad.write_text("company,site\nZentrico,https://zentrico.example\n", encoding="utf-8")
    res = CliRunner().invoke(main, ["signal", "careers", "--seeds", str(bad), "--dry-run"])
    assert res.exit_code == 1 and "missing column" in res.output


def test_cli_careers_twice(
    sender, tmp_path: Path, sites: Sites, db_file: Path, monkeypatch
) -> None:
    real_guard = guard_http.Guard

    def mocked(**kw):
        return real_guard(limiter=RateLimiter(limits={}, default_rps=50),
                          transport=httpx.MockTransport(sites.handler), **kw)

    monkeypatch.setattr(guard_http, "Guard", mocked)
    args = ["signal", "careers", "--seeds", str(write_seeds(tmp_path, SEEDS)),
            "--db", str(db_file), "--cache-dir", str(tmp_path / "cache")]
    first = CliRunner().invoke(main, args)
    assert first.exit_code == 0, first.output
    assert "seeds 5, signals 7, new 7, errors 0" in first.output
    assert "greenhouse:zentrico jobs (2)" in first.output
    assert "robots_denied" in first.output and "blocked" in first.output
    second = CliRunner().invoke(main, args)
    assert second.exit_code == 0, second.output
    assert "seeds 5, signals 7, new 0, errors 0, requests sent 0" in second.output

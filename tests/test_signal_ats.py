"""The four ATS collectors (docs/PLAN.md Step 21), against hand-written fixtures
shaped like each board API's public JSON. Companies are invented. Requests go
to an httpx.MockTransport inside Guard; nothing here reaches the internet.
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

from engine.cli import main
from engine.guard import http as guard_http
from engine.guard.cache import Cache
from engine.guard.http import Guard
from engine.guard.ratelimit import RateLimiter
from engine.signal import ashby, ats, boards, greenhouse, lever, workable

FIXTURES = Path(__file__).parent / "fixtures" / "ats"
TODAY = date(2026, 11, 12)

FIXTURE_FILES = {
    "greenhouse": ("zentrico", "greenhouse_zentrico.json"),
    "lever": ("quillbyte", "lever_quillbyte.json"),
    "ashby": ("norvale", "ashby_norvale.json"),
    "workable": ("tessaro", "workable_tessaro.json"),
}


class Boards:
    """A mock transport: exact URL -> (status, body); robots.txt allows all."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.requests: list[str] = []

    def add(self, url: str, status: int = 200, body: bytes | str = b"") -> None:
        self.routes[url] = (status, body.encode() if isinstance(body, str) else body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        status, body = self.routes.get(url, (404, b'{"error":"not found"}'))
        return httpx.Response(status, content=body,
                              headers={"content-type": "application/json"})

    def page_requests(self) -> list[str]:
        return [u for u in self.requests if not u.endswith("/robots.txt")]


def fixture_bytes(source: str) -> bytes:
    return (FIXTURES / FIXTURE_FILES[source][1]).read_bytes()


@pytest.fixture
def mock_boards() -> Boards:
    b = Boards()
    for source, (slug, _) in FIXTURE_FILES.items():
        b.add(boards.COLLECTORS[source].board_url(slug), body=fixture_bytes(source))
    return b


def make_guard(tmp_path: Path, mb: Boards) -> Guard:
    return Guard(
        cache=Cache(tmp_path / "cache"),
        limiter=RateLimiter(limits={}, default_rps=50),
        transport=httpx.MockTransport(mb.handler),
    )


def fetch(tmp_path: Path, mb: Boards, source: str, slug: str) -> ats.BoardResult:
    async def go() -> ats.BoardResult:
        async with make_guard(tmp_path, mb) as g:
            return await boards.collect_board(source, slug, g, today=TODAY)

    return asyncio.run(go())


def row(s) -> dict:
    return s.model_dump(mode="json")


# -- field mapping, one fixture per ATS ------------------------------------------


def test_greenhouse_mapping(sender, tmp_path: Path, mock_boards: Boards) -> None:
    r = fetch(tmp_path, mock_boards, "greenhouse", "zentrico")
    assert r.state == "jobs" and r.status == 200 and r.skipped == 0
    first, second = (row(s) for s in r.signals)
    assert first == {
        "signal_id": "gh-zentrico-4411001",
        "source": "greenhouse",
        "board_slug": "zentrico",
        "company_name_raw": "Zentrico",
        "title": "Founding Sales Development Representative",
        "role_family": None,
        "offer_type": None,
        "location": "Bengaluru, Karnataka, India",
        "posted_at": "2026-11-04",  # updated_at 22:15 at -04:00 is the 4th in UTC
        "url": "https://job-boards.greenhouse.io/zentrico/jobs/4411001",
        "first_seen": "2026-11-12",
        "company_id": None,
        "description_text": (
            "Zentrico builds invoice reconciliation software for mid-market finance teams.\n"
            "What you will do\n"
            "Build and own target account lists from scratch\n"
            "Run outbound sequences to finance leaders in North America\n"
            "You will be our first SDR in India & report to the Head of Sales."
        ),
        "fresh": None,  # set later by `te signal filter`
        "india_located": None,
    }
    assert second["posted_at"] == "2026-10-21" and second["location"] == "New York, NY"
    # descriptions come only with ?content=true
    assert mock_boards.page_requests() == [
        "https://boards-api.greenhouse.io/v1/boards/zentrico/jobs?content=true"
    ]


def test_lever_mapping(sender, tmp_path: Path, mock_boards: Boards) -> None:
    r = fetch(tmp_path, mock_boards, "lever", "quillbyte")
    assert r.state == "jobs"
    first, second = (row(s) for s in r.signals)
    assert first["signal_id"] == "lv-quillbyte-5c1e7a2b-90d4-4f6e-8a1b-3f2d6c7e9a10"
    assert first["source"] == "lever" and first["board_slug"] == "quillbyte"
    assert first["company_name_raw"] == "quillbyte"  # Lever's JSON names no company
    assert first["title"] == "Founding Account Executive"
    assert first["location"] == "Pune, India; Remote - India"
    assert first["posted_at"] == "2026-11-03"  # createdAt 1793700000000 ms
    assert first["url"] == "https://jobs.lever.co/quillbyte/5c1e7a2b-90d4-4f6e-8a1b-3f2d6c7e9a10"
    assert first["description_text"] == (
        "Quillbyte helps procurement teams compare supplier quotes.\n"
        "We are hiring our first account executive for India.\n\n"
        "What you will do\n\n"
        "Run full-cycle deals with mid-market buyers\n"
        "Build the first sales playbook with the founders\n\n"
        "What you bring\n\n"
        "Three years closing B2B software deals\n\n"
        "Quillbyte is an equal opportunity employer."
    )
    assert second["posted_at"] == "2026-10-14" and second["location"] == "Austin, TX"
    assert mock_boards.page_requests() == ["https://api.lever.co/v0/postings/quillbyte?mode=json"]


def test_ashby_mapping(sender, tmp_path: Path, mock_boards: Boards) -> None:
    r = fetch(tmp_path, mock_boards, "ashby", "norvale")
    first, second = (row(s) for s in r.signals)
    assert first["signal_id"] == "ab-norvale-0b6d2f84-3c1a-4e9b-9f27-5a8e1d4c3b62"
    assert first["company_name_raw"] == "norvale"  # Ashby's JSON names no company
    assert first["title"] == "GTM Engineer"
    assert first["location"] == "Hyderabad; Bengaluru"
    assert first["posted_at"] == "2026-10-28"
    assert first["url"] == "https://jobs.ashbyhq.com/norvale/0b6d2f84-3c1a-4e9b-9f27-5a8e1d4c3b62"
    assert first["description_text"] == (
        "Norvale makes fleet maintenance software.\n\nWire our enrichment and routing into the CRM."
    )
    assert second["posted_at"] == "2026-11-10"  # 23:30 at -08:00 is the 10th in UTC
    assert mock_boards.page_requests() == [
        "https://api.ashbyhq.com/posting-api/job-board/norvale"
    ]


def test_workable_mapping(sender, tmp_path: Path, mock_boards: Boards) -> None:
    r = fetch(tmp_path, mock_boards, "workable", "tessaro")
    first, second = (row(s) for s in r.signals)
    assert first["signal_id"] == "wk-tessaro-7F3A21C9B0"
    assert first["company_name_raw"] == "Tessaro Labs"
    assert first["title"] == "Business Development Representative"
    assert first["location"] == "Gurugram, Haryana, India"
    assert first["posted_at"] == "2026-10-30"
    assert first["url"] == "https://apply.workable.com/j/7F3A21C9B0"
    assert first["description_text"] == (
        "Book first meetings with clinic owners across North India.\n"
        "Prospect from our clinic database"
    )
    # no published_on: created_at stands in; no locations list: the job's own fields
    assert second["posted_at"] == "2026-11-02"
    assert second["location"] == "Mumbai, Maharashtra, India"
    assert mock_boards.page_requests() == [
        "https://apply.workable.com/api/v1/widget/accounts/tessaro?details=true"
    ]


@pytest.mark.parametrize("module", [greenhouse, lever, ashby, workable])
def test_collect_returns_signals(module, sender, tmp_path: Path, mock_boards: Boards) -> None:
    slug = FIXTURE_FILES[module.SOURCE][0]

    async def go():
        async with make_guard(tmp_path, mock_boards) as g:
            return await module.collect(slug, g, today=TODAY)

    signals = asyncio.run(go())
    assert len(signals) == 2
    assert all(s.source == module.SOURCE and s.posted_at is not None for s in signals)
    assert all(s.role_family is None and s.offer_type is None for s in signals)


# -- outcomes -----------------------------------------------------------------


@pytest.mark.parametrize("source", list(FIXTURE_FILES))
def test_404_is_a_wrong_or_private_board(source, sender, tmp_path: Path) -> None:
    mb = Boards()  # every board URL 404s
    r = fetch(tmp_path, mb, source, "nosuchboard")
    assert r.outcome == "not_found" and r.state == "not_found" and r.status == 404
    assert r.signals == [] and r.note == "wrong or private board"


EMPTY = {
    "greenhouse": {"jobs": [], "meta": {"total": 0}},
    "lever": [],
    "ashby": {"apiVersion": "1", "jobs": []},
    "workable": {"name": "Brindle Works", "description": "", "jobs": []},
}


@pytest.mark.parametrize("source", list(FIXTURE_FILES))
def test_empty_board_is_real_with_nothing_open(source, sender, tmp_path: Path) -> None:
    mb = Boards()
    mb.add(boards.COLLECTORS[source].board_url("brindle"), body=json.dumps(EMPTY[source]))
    r = fetch(tmp_path, mb, source, "brindle")
    assert r.outcome == "ok" and r.state == "empty" and r.signals == []
    assert r.note == "real board, nothing open"


def test_blocked_board_is_an_outcome_not_an_error(sender, tmp_path: Path) -> None:
    mb = Boards()
    mb.add(greenhouse.board_url("zentrico"), status=403, body="forbidden")
    r = fetch(tmp_path, mb, "greenhouse", "zentrico")
    assert r.state == "blocked" and r.signals == []


def test_ok_response_that_is_not_board_json_raises(sender, tmp_path: Path) -> None:
    mb = Boards()
    mb.add(lever.board_url("zentrico"), body="<html>maintenance</html>")
    with pytest.raises(ats.BoardFormatError):
        fetch(tmp_path, mb, "lever", "zentrico")
    mb.add(ashby.board_url("zentrico"), body='{"unexpected": true}')
    with pytest.raises(ats.BoardFormatError):
        fetch(tmp_path, mb, "ashby", "zentrico")


def test_job_without_title_or_id_is_skipped(sender, tmp_path: Path) -> None:
    payload = json.loads(fixture_bytes("greenhouse"))
    payload["jobs"].append({"id": 4411099, "title": "  "})
    payload["jobs"].append({"title": "No id here"})
    mb = Boards()
    mb.add(greenhouse.board_url("zentrico"), body=json.dumps(payload))
    r = fetch(tmp_path, mb, "greenhouse", "zentrico")
    assert len(r.signals) == 2 and r.skipped == 2


# -- raw JSON in the cache, idempotent storage ----------------------------------


@pytest.mark.parametrize("source", list(FIXTURE_FILES))
def test_raw_json_is_stored_by_the_cache(source, sender, tmp_path: Path, mock_boards) -> None:
    slug = FIXTURE_FILES[source][0]
    r = fetch(tmp_path, mock_boards, source, slug)
    assert (tmp_path / "cache" / r.sha256).read_bytes() == fixture_bytes(source)


def test_second_run_inserts_zero_new_rows(
    sender, tmp_path: Path, mock_boards: Boards, conn: sqlite3.Connection
) -> None:
    def run(day: date, cache_dir: Path) -> tuple[int, int]:
        async def go():
            g = Guard(cache=Cache(cache_dir), limiter=RateLimiter(limits={}, default_rps=50),
                      transport=httpx.MockTransport(mock_boards.handler))
            async with g:
                results = [await boards.collect_board(s, slug, g, today=day)
                           for s, (slug, _) in FIXTURE_FILES.items()]
                return results, g.requests_made

        results, made = asyncio.run(go())
        return boards.store(conn, [s for r in results for s in r.signals]), made

    new, made = run(TODAY, tmp_path / "c1")
    assert new == 8 and made == 8  # four boards, four robots.txt
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 8

    new, made = run(TODAY, tmp_path / "c1")  # same day: from the cache
    assert new == 0 and made == 0

    new, made = run(date(2026, 11, 13), tmp_path / "c2")  # refetched on a later day
    assert new == 0 and made == 8
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 8
    first_seen = {r[0] for r in conn.execute("SELECT first_seen FROM signals")}
    assert first_seen == {"2026-11-12"}  # a re-run never rewrites first_seen

    stored = conn.execute(
        "SELECT posted_at, description_text FROM signals WHERE signal_id = 'gh-zentrico-4411001'"
    ).fetchone()
    assert stored[0] == "2026-11-04" and stored[1].startswith("Zentrico builds")


# -- helpers --------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (1793700000000, date(2026, 11, 3)),
        ("2026-11-03T22:15:07-04:00", date(2026, 11, 4)),
        ("2026-10-28T09:41:12.345+00:00", date(2026, 10, 28)),
        ("2026-10-21T14:02:55Z", date(2026, 10, 21)),
        ("2026-10-21T14:02:55", date(2026, 10, 21)),
        ("2026-10-30", date(2026, 10, 30)),
        (None, None),
        ("", None),
        ("soon", None),
        (True, None),
    ],
)
def test_iso_date(value, expected) -> None:
    assert ats.iso_date(value) == expected


def test_board_specs() -> None:
    assert boards.parse_spec("Greenhouse:zentrico") == ("greenhouse", "zentrico")
    for bad in ("zentrico", "smartrecruiters:zentrico", "lever:", "lever:../x", "ashby:a/b"):
        with pytest.raises(ValueError):
            boards.parse_spec(bad)
    assert boards.unique_boards(["lever:Quillbyte", "lever:quillbyte", "ashby:quillbyte"]) == [
        ("lever", "Quillbyte"), ("ashby", "quillbyte"),
    ]


# -- CLI ------------------------------------------------------------------------


def test_cli_dry_run_projects_and_sends_nothing(tmp_path: Path) -> None:
    res = CliRunner().invoke(main, [
        "signal", "collect", "--board", "greenhouse:zentrico", "--board", "lever:quillbyte",
        "--dry-run", "--cache-dir", str(tmp_path / "cache"),
    ])
    assert res.exit_code == 0, res.output
    assert "2 urls" in res.output and "4 requests projected" in res.output


def test_cli_collect_twice(
    sender, tmp_path: Path, mock_boards: Boards, db_file: Path, monkeypatch
) -> None:
    real_guard = guard_http.Guard

    def mocked(**kw):
        return real_guard(limiter=RateLimiter(limits={}, default_rps=50),
                          transport=httpx.MockTransport(mock_boards.handler), **kw)

    monkeypatch.setattr(guard_http, "Guard", mocked)
    boards_file = tmp_path / "boards.txt"
    boards_file.write_text(
        "# invented boards\n" + "\n".join(f"{s}:{slug}" for s, (slug, _) in FIXTURE_FILES.items())
        + "\nashby:nosuchboard\n",
        encoding="utf-8",
    )
    args = ["signal", "collect", "--boards-file", str(boards_file), "--db", str(db_file),
            "--cache-dir", str(tmp_path / "cache")]
    first = CliRunner().invoke(main, args)
    assert first.exit_code == 0, first.output
    assert "boards 5, signals 8, new 8, errors 0" in first.output
    assert "wrong or private board" in first.output
    second = CliRunner().invoke(main, args)
    assert second.exit_code == 0, second.output
    assert "boards 5, signals 8, new 0, errors 0, requests sent 0" in second.output

    limited = CliRunner().invoke(main, args + ["--limit", "1"])
    assert "boards 1," in limited.output

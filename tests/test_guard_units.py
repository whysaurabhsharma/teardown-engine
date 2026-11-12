"""Guard pieces without a network: ratelimit, cache, budget, robots, and `te fetch`."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine.cli import main
from engine.guard import robots
from engine.guard.budget import Budget, BudgetExceeded, Projection, format_projection, project
from engine.guard.cache import Cache, CacheEntry, cache_dir
from engine.guard.ratelimit import (
    DEFAULT_RPS,
    RateLimiter,
    TokenBucket,
    load_limits,
    provider_for,
)

# -- ratelimit -------------------------------------------------------------------


def test_limits_load_from_budget_md() -> None:
    limits = load_limits()
    assert limits["greenhouse"] == 1.0
    assert limits["lever"] == 1.0
    assert limits["ashby"] == 1.0
    assert "claude-cli" not in limits and "sqlite" not in limits


def test_load_limits_parses_a_table(tmp_path: Path) -> None:
    p = tmp_path / "BUDGET.md"
    p.write_text(
        "| Provider | Reported limit | Default applied |\n| --- | --- | --- |\n"
        "| lever | 10/s | 0.5 rps |\n| sqlite | n/a | local |\n",
        encoding="utf-8",
    )
    assert load_limits(p) == {"lever": 0.5}
    assert load_limits(tmp_path / "missing.md") == {}


def test_providers_share_a_bucket_and_never_exceed_the_default() -> None:
    rl = RateLimiter(limits={"greenhouse": 5.0, "lever": 0.5})
    assert provider_for("https://boards-api.greenhouse.io/v1/boards/x/jobs") == "greenhouse"
    assert rl.key_for("https://boards-api.greenhouse.io/v1/x") == ("greenhouse", DEFAULT_RPS)
    assert rl.key_for("https://api.lever.co/v0/postings/x") == ("lever", 0.5)
    assert rl.key_for("https://zentrico.example/careers") == ("zentrico.example", DEFAULT_RPS)
    assert rl.key_for("http://127.0.0.1:8080/x") == ("127.0.0.1:8080", DEFAULT_RPS)
    assert rl.bucket("https://boards.greenhouse.io/a") is rl.bucket("https://boards-api.greenhouse.io/b")


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s


def test_token_bucket_spaces_acquisitions() -> None:
    clock = FakeClock()
    bucket = TokenBucket(1.0, clock=clock, sleep=clock.sleep)
    stamps: list[float] = []

    async def go():
        for _ in range(4):
            await bucket.acquire()
            stamps.append(clock())

    asyncio.run(go())
    assert stamps == [100.0, 101.0, 102.0, 103.0]


def test_crawl_delay_only_slows_a_host() -> None:
    rl = RateLimiter(limits={})
    rl.apply_crawl_delay("https://zentrico.example/", 5.0)
    assert rl.bucket("https://zentrico.example/x").rate == pytest.approx(0.2)
    rl.apply_crawl_delay("https://zentrico.example/", 0.1)  # faster than default: ignored
    assert rl.bucket("https://zentrico.example/x").rate == pytest.approx(0.2)


# -- robots ----------------------------------------------------------------------


def test_robots_policy_from_response() -> None:
    rp = robots.policy_from_response(200, b"User-agent: *\nDisallow: /private\nCrawl-delay: 3\n")
    assert robots.allowed(rp, "https://zentrico.example/careers")
    assert not robots.allowed(rp, "https://zentrico.example/private/x")
    assert robots.crawl_delay(rp) == 3.0
    assert not robots.allowed(robots.policy_from_response(401, None), "https://zentrico.example/")
    assert robots.allowed(robots.policy_from_response(404, None), "https://zentrico.example/")
    assert not robots.allowed(robots.policy_from_response(503, None), "https://zentrico.example/")
    assert not robots.allowed(robots.policy_from_response(429, None), "https://zentrico.example/")


# -- cache -----------------------------------------------------------------------


def test_cache_dir_resolves_like_the_db(tmp_path: Path, monkeypatch) -> None:
    assert cache_dir(tmp_path / "x") == tmp_path / "x"
    monkeypatch.setenv("TE_CACHE_DIR", str(tmp_path / "env"))
    assert cache_dir() == tmp_path / "env"
    monkeypatch.delenv("TE_CACHE_DIR")
    assert cache_dir() == Path("data") / "cache"


def _entry(url: str, sha: str | None, when: datetime) -> CacheEntry:
    return CacheEntry(url=url, final_url=url, kind="page", status=200, outcome="ok",
                      sha256=sha, fetched_at=when)


def test_cache_same_day_lookup(tmp_path: Path) -> None:
    c = Cache(tmp_path)
    sha = c.put_body(b"raw bytes")
    assert c.get_body(sha) == b"raw bytes" and (tmp_path / sha).exists()
    url = "https://zentrico.example/careers"
    c.record(_entry(url, sha, datetime(2026, 11, 12, 23, 59, tzinfo=UTC)))
    assert c.lookup(url, date(2026, 11, 12)) is not None
    assert c.lookup(url, date(2026, 11, 13)) is None
    (tmp_path / sha).unlink()  # body gone: not a usable hit
    assert c.lookup(url, date(2026, 11, 12)) is None


# -- budget ----------------------------------------------------------------------


def test_budget_ceilings() -> None:
    b = Budget(max_fetches=2, max_model_calls=1)
    b.charge_fetch()
    b.charge_fetch()
    with pytest.raises(BudgetExceeded):
        b.charge_fetch()
    b.charge_model_call()
    with pytest.raises(BudgetExceeded):
        b.charge_model_call()
    assert b.remaining_fetches == 0 and b.fetches == 2


def test_projection_counts_only_uncached_requests(tmp_path: Path) -> None:
    c = Cache(tmp_path)
    today = date(2026, 11, 12)
    now = datetime(2026, 11, 12, 9, tzinfo=UTC)
    c.record(_entry("https://zentrico.example/robots.txt", c.put_body(b""), now))
    c.record(_entry("https://zentrico.example/", c.put_body(b"home"), now))
    urls = ["https://zentrico.example/", "https://zentrico.example/careers",
            "https://halvora.example/", "https://zentrico.example/careers"]
    p = project(urls, c, day=today)
    assert p == Projection(urls=3, cached=1, page_fetches=2, robots_fetches=1)
    assert p.fetches == 3
    assert project(urls, c, day=today, fresh=True).page_fetches == 3
    assert "OVER BUDGET" in format_projection(p, Budget(max_fetches=2))
    assert "3 requests projected" in format_projection(p)


# -- te fetch ----------------------------------------------------------------------


def test_cli_dry_run_projects_and_sends_nothing(server, tmp_path: Path) -> None:
    server.route("/page", body="hello")
    result = CliRunner().invoke(
        main, ["fetch", "--dry-run", "--cache-dir", str(tmp_path / "c"), server.url("/page")]
    )
    assert result.exit_code == 0, result.output
    assert "2 requests projected (1 pages, 1 robots.txt)" in result.output
    assert server.hits == []


def test_cli_second_run_sends_zero_requests(server, sender: str, tmp_path: Path) -> None:
    server.route("/page", body="hello")
    args = ["fetch", "--cache-dir", str(tmp_path / "c"), server.url("/page")]
    first = CliRunner().invoke(main, args)
    assert first.exit_code == 0, first.output
    assert "requests sent: 2" in first.output
    second = CliRunner().invoke(main, args)
    assert second.exit_code == 0, second.output
    assert "requests sent: 0" in second.output and "cache" in second.output
    assert len(server.hits) == 2


def test_cli_fetch_without_sender_email_fails_cleanly(server, tmp_path: Path) -> None:
    server.route("/page", body="hello")
    result = CliRunner().invoke(main, ["fetch", "--cache-dir", str(tmp_path / "c"), server.url("/page")])
    assert result.exit_code == 1 and "SENDER_EMAIL" in result.output
    assert server.hits == []

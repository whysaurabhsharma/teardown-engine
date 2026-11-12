"""Guard's HTTP rules, against a local server on 127.0.0.1 (or a mock transport)."""

from __future__ import annotations

import asyncio
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from engine.guard import http as guard_http
from engine.guard.budget import Budget, BudgetExceeded
from engine.guard.cache import Cache
from engine.guard.http import (
    MAX_BYTES,
    Guard,
    GuardConfigError,
    parse_retry_after,
    user_agent,
)
from engine.guard.ratelimit import RateLimiter

HTML = "<html><head><title>Zentrico careers</title></head><body>We are hiring.</body></html>"


def fast() -> RateLimiter:
    """A quicker limiter so tests that are not about pacing do not wait a second per hop."""
    return RateLimiter(limits={}, default_rps=50)


class SleepLog:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def guard(tmp_path: Path, **kw) -> Guard:
    kw.setdefault("limiter", fast())
    return Guard(cache=Cache(tmp_path / "cache"), **kw)


def run_fetch(g: Guard, url: str, kind: str = "page", **kw):
    async def go():
        async with g:
            return await g.fetch(url, kind, **kw)

    return asyncio.run(go())


# -- identity -----------------------------------------------------------------


def test_user_agent_is_one_honest_string(sender: str) -> None:
    assert user_agent() == (
        "teardown-engine/0.1 (+https://github.com/whysaurabhsharma/teardown-engine; "
        f"research; contact {sender})"
    )


def test_no_sender_email_refuses_to_touch_the_network(server, tmp_path: Path) -> None:
    server.route("/page", body=HTML)
    with pytest.raises(GuardConfigError, match="SENDER_EMAIL"):
        run_fetch(guard(tmp_path), server.url("/page"))
    assert server.hits == []


def test_ok_fetch_sends_ua_and_stores_raw_body(server, sender: str, tmp_path: Path) -> None:
    server.route("/page", body=HTML, headers={"Content-Type": "text/html"})
    r = run_fetch(guard(tmp_path), server.url("/page"), kind="careers")
    assert r.outcome == "ok" and r.status == 200 and r.kind == "careers"
    assert r.body == HTML.encode() and not r.from_cache
    assert r.requests == 2  # robots.txt, then the page
    assert Path(r.cache_path).read_bytes() == HTML.encode()
    assert Path(r.cache_path).name == r.sha256
    assert {h.user_agent for h in server.hits} == {user_agent()}


# -- cache --------------------------------------------------------------------


def test_second_fetch_same_day_is_cache_hit_with_zero_requests(server, sender: str, tmp_path: Path) -> None:
    server.route("/page", body=HTML)
    first = run_fetch(guard(tmp_path), server.url("/page"))
    hits_after_first = len(server.hits)

    g2 = guard(tmp_path)  # a new run: nothing in memory, only the cache on disk
    second = run_fetch(g2, server.url("/page"))
    assert second.from_cache and second.requests == 0 and g2.requests_made == 0
    assert len(server.hits) == hits_after_first  # robots.txt was not refetched either
    assert second.body == first.body and second.outcome == "ok"


def test_cache_hit_needs_no_sender_email(server, sender: str, tmp_path: Path, monkeypatch) -> None:
    server.route("/page", body=HTML)
    run_fetch(guard(tmp_path), server.url("/page"))
    monkeypatch.delenv("SENDER_EMAIL")
    assert run_fetch(guard(tmp_path), server.url("/page")).from_cache


def test_fresh_bypasses_the_cache(server, sender: str, tmp_path: Path) -> None:
    server.sequence("/page", [(200, {}, "v1"), (200, {}, "v2")])
    run_fetch(guard(tmp_path), server.url("/page"))
    r = run_fetch(guard(tmp_path), server.url("/page"), fresh=True)
    assert not r.from_cache and r.text == "v2"
    assert len(server.hits_for("/page")) == 2
    assert len(server.hits_for("/robots.txt")) == 1  # robots.txt still from cache


def test_next_day_refetches(server, sender: str, tmp_path: Path) -> None:
    server.route("/page", body=HTML)
    day1 = datetime(2026, 11, 12, 10, tzinfo=UTC)
    run_fetch(guard(tmp_path, now=lambda: day1), server.url("/page"))
    r = run_fetch(guard(tmp_path, now=lambda: day1 + timedelta(days=1)), server.url("/page"))
    assert not r.from_cache
    assert len(server.hits_for("/page")) == 2


def test_failures_are_cached_too(server, sender: str, tmp_path: Path) -> None:
    server.route("/gone", status=403, body="no")
    run_fetch(guard(tmp_path), server.url("/gone"))
    r = run_fetch(guard(tmp_path), server.url("/gone"))
    assert r.outcome == "blocked" and r.from_cache
    assert len(server.hits_for("/gone")) == 1


# -- robots.txt -----------------------------------------------------------------


def test_robots_disallow_is_respected_without_fetching(server, sender: str, tmp_path: Path) -> None:
    server.route("/private/team", body=HTML)
    r = run_fetch(guard(tmp_path), server.url("/private/team"))
    assert r.outcome == "robots_denied" and r.body is None and r.status is None
    assert server.hits_for("/private/team") == []
    assert len(server.hits_for("/robots.txt")) == 1


def test_redirect_into_disallowed_path_is_robots_denied(server, sender: str, tmp_path: Path) -> None:
    server.route("/jobs", status=302, headers={"Location": "/private/jobs"})
    server.route("/private/jobs", body=HTML)
    r = run_fetch(guard(tmp_path), server.url("/jobs"))
    assert r.outcome == "robots_denied"
    assert server.hits_for("/private/jobs") == []


def test_robots_403_denies_everything(server, sender: str, tmp_path: Path) -> None:
    server.route("/robots.txt", status=403, body="no")
    server.route("/page", body=HTML)
    assert run_fetch(guard(tmp_path), server.url("/page")).outcome == "robots_denied"
    assert server.hits_for("/page") == []


def test_robots_404_allows_everything(server, sender: str, tmp_path: Path) -> None:
    server.route("/robots.txt", status=404)
    server.route("/private/x", body=HTML)
    assert run_fetch(guard(tmp_path), server.url("/private/x")).outcome == "ok"


def test_robots_agent_specific_rule(server, sender: str, tmp_path: Path) -> None:
    server.route("/robots.txt", body="User-agent: teardown-engine\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    server.route("/page", body=HTML)
    assert run_fetch(guard(tmp_path), server.url("/page")).outcome == "robots_denied"


# -- outcomes -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(403, "blocked"), (401, "blocked"), (451, "blocked"), (404, "not_found"), (410, "not_found"),
     (500, "dead"), (400, "dead")],
)
def test_status_becomes_outcome(server, sender: str, tmp_path: Path, status: int, outcome: str) -> None:
    server.route("/x", status=status, body="body")
    r = run_fetch(guard(tmp_path), server.url("/x"))
    assert r.outcome == outcome and r.status == status
    assert len(server.hits_for("/x")) == 1  # never retried


def test_challenge_page_is_blocked(server, sender: str, tmp_path: Path) -> None:
    server.route("/page", body="<html><head><title>Just a moment...</title></head></html>")
    assert run_fetch(guard(tmp_path), server.url("/page")).outcome == "blocked"


def test_503_challenge_is_blocked_and_not_retried(server, sender: str, tmp_path: Path) -> None:
    server.route("/page", status=503, headers={"cf-mitigated": "challenge", "Retry-After": "0"})
    sleeps = SleepLog()
    r = run_fetch(guard(tmp_path, sleep=sleeps), server.url("/page"))
    assert r.outcome == "blocked" and sleeps.waits == []
    assert len(server.hits_for("/page")) == 1


def test_recaptcha_on_an_ordinary_page_is_not_a_challenge(server, sender: str, tmp_path: Path) -> None:
    server.route("/contact", body=HTML + '<div class="g-recaptcha"></div>')
    assert run_fetch(guard(tmp_path), server.url("/contact")).outcome == "ok"


def test_429_honours_retry_after_and_retries_once(server, sender: str, tmp_path: Path) -> None:
    server.sequence("/page", [(429, {"Retry-After": "3"}, ""), (200, {}, HTML)])
    sleeps = SleepLog()
    r = run_fetch(guard(tmp_path, sleep=sleeps), server.url("/page"))
    assert r.outcome == "ok" and r.requests == 3
    assert sleeps.waits == [3.0]


def test_429_twice_is_blocked_after_exactly_one_retry(server, sender: str, tmp_path: Path) -> None:
    server.route("/page", status=429, headers={"Retry-After": "1"})
    sleeps = SleepLog()
    r = run_fetch(guard(tmp_path, sleep=sleeps), server.url("/page"))
    assert r.outcome == "blocked" and r.status == 429
    assert len(server.hits_for("/page")) == 2 and sleeps.waits == [1.0]


def test_503_twice_is_dead(server, sender: str, tmp_path: Path) -> None:
    server.route("/page", status=503)  # no Retry-After: the default wait applies
    sleeps = SleepLog()
    r = run_fetch(guard(tmp_path, sleep=sleeps), server.url("/page"))
    assert r.outcome == "dead" and len(server.hits_for("/page")) == 2
    assert sleeps.waits == [guard_http.RETRY_WAIT_DEFAULT]


def test_retry_after_beyond_the_limit_is_not_waited_for(server, sender: str, tmp_path: Path) -> None:
    server.route("/page", status=429, headers={"Retry-After": "3600"})
    sleeps = SleepLog()
    r = run_fetch(guard(tmp_path, sleep=sleeps), server.url("/page"))
    assert r.outcome == "blocked" and sleeps.waits == []
    assert len(server.hits_for("/page")) == 1


def test_parse_retry_after() -> None:
    now = datetime(2026, 11, 12, 10, 0, 0, tzinfo=UTC)
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("Thu, 12 Nov 2026 10:00:30 GMT", now) == 30.0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


def test_body_is_capped_at_2_mb(server, sender: str, tmp_path: Path) -> None:
    server.route("/big", body=b"a" * (3 * 1024 * 1024))
    r = run_fetch(guard(tmp_path), server.url("/big"))
    assert r.outcome == "ok" and r.truncated
    assert len(r.body) == MAX_BYTES == 2 * 1024 * 1024
    assert "cut at" in r.detail


def test_redirects_are_followed(server, sender: str, tmp_path: Path) -> None:
    server.route("/old", status=301, headers={"Location": "/new"})
    server.route("/new", body=HTML)
    r = run_fetch(guard(tmp_path), server.url("/old"))
    assert r.outcome == "ok" and r.final_url == server.url("/new") and r.url == server.url("/old")


def test_more_than_five_redirects_is_dead(server, sender: str, tmp_path: Path) -> None:
    for i in range(7):
        server.route(f"/r{i}", status=302, headers={"Location": f"/r{i + 1}"})
    r = run_fetch(guard(tmp_path), server.url("/r0"))
    assert r.outcome == "dead" and "redirects" in r.detail
    assert len([h for h in server.hits if h.path != "/robots.txt"]) == 6  # original + 5 hops


def test_connection_refused_is_dead(sender: str, tmp_path: Path) -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = run_fetch(guard(tmp_path), f"http://127.0.0.1:{port}/page")
    assert r.outcome == "dead" and "robots.txt unreachable" in r.detail


def test_timeout_is_dead(server, sender: str, tmp_path: Path) -> None:
    def slow():
        time.sleep(1.5)
        return 200, {}, b"late"

    server.handler("/slow", slow)
    r = run_fetch(guard(tmp_path, timeout=0.3), server.url("/slow"))
    assert r.outcome == "dead" and "timeout" in r.detail


def test_non_http_url_is_rejected(sender: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_fetch(guard(tmp_path), "file:///etc/passwd")


# -- pacing and concurrency -------------------------------------------------------


def test_per_host_pacing_holds_at_one_request_per_second(server, sender: str, tmp_path: Path) -> None:
    server.route("/a", body=HTML)
    server.route("/b", body=HTML)
    g = Guard(cache=Cache(tmp_path / "cache"), limiter=RateLimiter(limits={}))  # the real default

    async def go():
        async with g:
            return await asyncio.gather(g.fetch(server.url("/a"), "page"), g.fetch(server.url("/b"), "page"))

    results = asyncio.run(go())
    assert [r.outcome for r in results] == ["ok", "ok"]
    times = sorted(h.at for h in server.hits)
    assert len(times) == 3  # robots.txt + 2 pages
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= 0.95, gaps


def test_at_most_four_in_flight_overall(sender: str, tmp_path: Path) -> None:
    state = {"now": 0, "max": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.1)
        state["now"] -= 1
        return httpx.Response(200, text="hello")

    g = guard(tmp_path, transport=httpx.MockTransport(handler))
    urls = [f"https://site{i}.example/page" for i in range(10)]  # ten different hosts

    async def go():
        async with g:
            return await asyncio.gather(*(g.fetch(u, "page") for u in urls))

    results = asyncio.run(go())
    assert all(r.outcome == "ok" for r in results)
    assert state["max"] == 4


# -- budget -------------------------------------------------------------------------


def test_budget_ceiling_stops_before_the_request(server, sender: str, tmp_path: Path) -> None:
    server.route("/a", body=HTML)
    server.route("/b", body=HTML)
    g = guard(tmp_path, budget=Budget(max_fetches=2))

    async def go():
        async with g:
            await g.fetch(server.url("/a"), "page")  # robots + /a = 2
            await g.fetch(server.url("/a"), "page")  # cache hit, free
            await g.fetch(server.url("/b"), "page")

    with pytest.raises(BudgetExceeded):
        asyncio.run(go())
    assert server.hits_for("/b") == []
    assert g.budget.fetches == 2


# -- module-level fetch ---------------------------------------------------------------


def test_module_level_fetch_uses_the_configured_cache(server, sender: str, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TE_CACHE_DIR", str(tmp_path / "envcache"))
    monkeypatch.setattr(guard_http, "_default", None)
    server.route("/page", body=HTML)

    async def go():
        return await guard_http.fetch(server.url("/page"), "page")

    r = asyncio.run(go())
    assert r.outcome == "ok"
    assert Path(r.cache_path).parent == tmp_path / "envcache"

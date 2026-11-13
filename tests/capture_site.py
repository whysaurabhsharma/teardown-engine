"""Invented websites, resolver and browser for capture tests. Nothing here reaches the internet.

FakeSites answers httpx requests through MockTransport from a table of
host -> path -> reply, records every hit with its start and end time, and
tracks how many requests are in flight at once.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from engine.capture.capture import Runtime
from engine.guard.browser import Allow, BrowserError, Rendered
from engine.guard.budget import Budget
from engine.guard.cache import Cache
from engine.guard.dns import DnsGuard, LookupFailed, NameNotFound
from engine.guard.http import Guard
from engine.guard.ratelimit import RateLimiter

FILLER = (
    "Zentrico helps finance teams close their books faster with reconciliations that run "
    "themselves, approvals that route to the right person and reports that are ready when the "
    "month ends. Teams in Pune, Austin and Leeds use it every day. "
) * 4  # well over 500 visible characters

Reply = tuple[int, dict[str, str], bytes]


def page(body: str, title: str = "Zentrico") -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


@dataclass
class Hit:
    host: str
    path: str
    user_agent: str | None
    start: float
    end: float = 0.0


@dataclass
class FakeSites:
    routes: dict[str, dict[str, Reply]] = field(default_factory=dict)
    hits: list[Hit] = field(default_factory=list)
    delay: float = 0.0
    in_flight: int = 0
    max_in_flight: int = 0

    def route(self, host: str, path: str, body: str | bytes = b"", status: int = 200,
              headers: dict[str, str] | None = None) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        hdrs = {"content-type": "text/html; charset=utf-8"} if headers is None else headers
        self.routes.setdefault(host, {})[path] = (status, hdrs, data)

    def paths(self, host: str | None = None) -> list[str]:
        return [h.path for h in self.hits if host is None or h.host == host]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        hit = Hit(request.url.host, request.url.raw_path.decode(),
                  request.headers.get("user-agent"), time.monotonic())
        self.hits.append(hit)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            status, headers, body = self.routes.get(hit.host, {}).get(
                hit.path, (404, {"content-type": "text/plain"}, b"no such page"))
            return httpx.Response(status, headers=headers, content=body)
        finally:
            self.in_flight -= 1
            hit.end = time.monotonic()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@dataclass
class FakeResolver:
    """(name, type) -> records; names in `missing` are NXDOMAIN; `dead` fails everything."""

    records: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    dead: bool = False
    queries: list[tuple[str, str]] = field(default_factory=list)

    async def resolve(self, name: str, rdtype: str, timeout: float) -> list[str]:
        self.queries.append((name, rdtype))
        if self.dead:
            raise LookupFailed("timeout after 5s")
        if (name, rdtype) in self.records:
            return self.records[(name, rdtype)]
        if any(n == name for n, _ in self.records):
            raise NameNotFound(f"no {rdtype} record")
        raise NameNotFound("NXDOMAIN")


@dataclass
class FakeRenderer:
    """Stands in for headless chromium: asks Guard's allow() for the document, as the
    real one does through page.route, then returns the html it was given."""

    pages: dict[str, str] = field(default_factory=dict)  # url -> rendered html
    calls: list[str] = field(default_factory=list)
    allowed: list[bool] = field(default_factory=list)
    closed: bool = False

    async def render(self, url: str, *, user_agent: str, allow: Allow,
                     timeout: float) -> Rendered:
        self.calls.append(url)
        ok = await allow(url, "document")
        self.allowed.append(ok)
        if not ok:
            raise BrowserError("document request refused")
        html = self.pages.get(url, page(FILLER))
        return Rendered(status=200, final_url=url, html=html,
                        headers={"content-type": "text/html"})

    async def aclose(self) -> None:
        self.closed = True


def fast_limiter() -> RateLimiter:
    return RateLimiter(limits={}, default_rps=1000)


def runtime(tmp_path: Path, sites: FakeSites, resolver: FakeResolver | None = None,
            renderer: FakeRenderer | None = None, *, max_in_flight: int = 4,
            limiter: RateLimiter | None = None, budget: Budget | None = None,
            cache: Cache | None = None) -> Runtime:
    cache = cache or Cache(tmp_path / "cache")
    budget = budget or Budget()
    return Runtime(
        guard=Guard(cache=cache, budget=budget, transport=sites.transport(),
                    limiter=limiter or fast_limiter(), max_in_flight=max_in_flight),
        dns=DnsGuard(cache=cache, budget=budget, limiter=fast_limiter(),
                     resolver=resolver or FakeResolver()),
        renderer=renderer or FakeRenderer(),
    )

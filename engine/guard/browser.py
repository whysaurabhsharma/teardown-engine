"""Guarded headless chromium: the playwright fallback (protocol/CONDUCT.md, "Playwright").

    res = await fetch_rendered(guard, url, kind, renderer=PlaywrightRenderer())

Used only when an `ok` page yields under 500 characters of visible text; the
caller (engine/capture/) enforces that and the cap of 2 pages per company. This
module enforces the conduct rules, the same as guard.http:

- one honest User-Agent (guard.http.user_agent()), set on the browser context.
  No stealth, no fingerprint changes, no header spoofing.
- robots.txt is checked through Guard before navigating; a disallowed URL is
  `robots_denied` and the browser is never started for it. Every request the
  page makes (scripts, XHR) is robots-checked, paced per host by Guard's rate
  limiter and charged to the run budget before it is let through. Images,
  media, fonts and stylesheets are never requested: text does not need them.
  Redirect hops are not routed by playwright, so the final URL is
  robots-checked after the load and the page discarded if it is disallowed.
- the outcome uses guard.http.classify: 403, 429 and challenge pages are
  `blocked`, an outcome to record, never something to work around.
- the rendered document (page.content(), UTF-8) is what the browser produced
  for the URL. It is stored byte for byte in the cache under its SHA-256, as
  guard.http stores a response body; it is never parsed before storing. Its
  cache index key is "browser:" + url, so it never answers a plain HTTP fetch.
  A same-day repeat is a cache hit and starts no browser.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from engine.guard.budget import BudgetExceeded
from engine.guard.cache import CacheEntry
from engine.guard.http import FetchResult, Guard, _normalise, classify, user_agent

CACHE_PREFIX = "browser:"
SKIPPED_RESOURCES = frozenset({"image", "media", "font", "stylesheet"})
SETTLE_SECONDS = 5.0  # extra wait for network idle after load; not an error if it times out

Allow = Callable[[str, str], Awaitable[bool]]  # (url, resource_type) -> may it be requested


class BrowserError(RuntimeError):
    """The browser could not load the page (no chromium, navigation error, timeout)."""


@dataclass
class Rendered:
    status: int | None  # HTTP status of the main document; None if no response
    final_url: str
    html: str
    headers: dict[str, str] = field(default_factory=dict)


class Renderer(Protocol):
    async def render(self, url: str, *, user_agent: str, allow: Allow,
                     timeout: float) -> Rendered: ...

    async def aclose(self) -> None: ...


class PlaywrightRenderer:
    """Headless chromium via playwright, started on first use, one context per page."""

    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None

    async def _ensure(self) -> Any:
        if self._browser is None:
            try:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(headless=True)
            except Exception as e:  # missing browser binaries, launch failure
                await self.aclose()
                raise BrowserError(f"cannot start chromium: {type(e).__name__}: {e}") from None
        return self._browser

    async def render(self, url: str, *, user_agent: str, allow: Allow,
                     timeout: float) -> Rendered:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        browser = await self._ensure()
        ctx = await browser.new_context(user_agent=user_agent, service_workers="block")
        try:
            page = await ctx.new_page()

            async def handle(route: Any, request: Any) -> None:
                if await allow(request.url, request.resource_type):
                    await route.continue_()
                else:
                    await route.abort()

            await page.route("**/*", handle)
            try:
                resp = await page.goto(url, wait_until="load", timeout=timeout * 1000)
            except PlaywrightError as e:
                raise BrowserError(f"navigation failed: {str(e).splitlines()[0]}") from None
            try:
                await page.wait_for_load_state("networkidle", timeout=SETTLE_SECONDS * 1000)
            except PlaywrightTimeout:
                pass
            html = await page.content()
            headers = await resp.all_headers() if resp is not None else {}
            return Rendered(status=resp.status if resp is not None else None,
                            final_url=page.url, html=html, headers=headers)
        finally:
            await ctx.close()

    async def aclose(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            finally:
                self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            finally:
                self._pw = None


def _result(guard: Guard, url: str, kind: str, entry: CacheEntry, body: bytes | None,
            *, from_cache: bool, requests: int) -> FetchResult:
    return FetchResult(
        url=url,
        kind=kind,
        outcome=entry.outcome,
        status=entry.status,
        final_url=entry.final_url,
        body=body,
        sha256=entry.sha256,
        cache_path=str(guard.cache.body_path(entry.sha256)) if entry.sha256 else None,
        content_type=entry.content_type,
        fetched_at=entry.fetched_at,
        from_cache=from_cache,
        truncated=entry.truncated,
        detail=entry.detail,
        requests=requests,
    )


async def fetch_rendered(guard: Guard, url: str, kind: str, *, renderer: Renderer,
                         fresh: bool = False) -> FetchResult:
    """Load url in headless chromium under Guard's rules. Always returns an outcome.

    Raises GuardConfigError without SENDER_EMAIL and BudgetExceeded when the run's
    fetch ceiling would be crossed, like Guard.fetch.
    """
    url = _normalise(url)
    key = CACHE_PREFIX + url
    if not fresh:
        entry = guard.cache.lookup(key, guard.now().date())
        if entry is not None:
            body = guard.cache.get_body(entry.sha256) if entry.sha256 else None
            return _result(guard, url, kind, entry, body, from_cache=True, requests=0)

    ua = user_agent()
    start = guard.requests_made
    over_budget: list[BudgetExceeded] = []

    def record(outcome: str, detail: str, *, status: int | None = None,
               final_url: str = url, body: bytes | None = None,
               content_type: str | None = None, truncated: bool = False) -> FetchResult:
        sha = guard.cache.put_body(body) if body is not None else None
        entry = CacheEntry(url=key, final_url=final_url, kind=kind, status=status,
                           outcome=outcome, sha256=sha, content_type=content_type,
                           truncated=truncated, detail=detail, fetched_at=guard.now())
        guard.cache.record(entry)
        return _result(guard, url, kind, entry, body, from_cache=False,
                       requests=guard.requests_made - start)

    verdict = await guard.robots_check(url)
    if verdict is not None:
        return record(verdict[0], verdict[1])

    async def allow(req_url: str, resource_type: str) -> bool:
        if resource_type in SKIPPED_RESOURCES:
            return False
        if not req_url.startswith(("http://", "https://")):
            return True  # data:, blob: and the like never reach the network
        if over_budget:
            return False
        try:
            if await guard.robots_check(req_url) is not None:
                return False
            guard.budget.charge_fetch()
        except BudgetExceeded as e:
            over_budget.append(e)
            return False
        except ValueError:
            return False
        await guard.limiter.acquire(req_url)
        guard.requests_made += 1
        return True

    try:
        r = await renderer.render(url, user_agent=ua, allow=allow, timeout=guard.timeout)
    except BrowserError as e:
        if over_budget:
            raise over_budget[0] from None
        return record("dead", f"playwright: {e}")
    if over_budget:
        raise over_budget[0]

    final_url = r.final_url or url
    if final_url != url and final_url.startswith(("http://", "https://")):
        verdict = await guard.robots_check(final_url)
        if verdict is not None:
            return record(verdict[0], f"redirected to {final_url}: {verdict[1]}",
                          final_url=final_url)

    body = r.html.encode("utf-8")
    truncated = len(body) > guard.max_bytes
    body = body[: guard.max_bytes]
    if r.status is None:
        return record("dead", "playwright: no response for the document", final_url=final_url)
    headers = httpx.Headers(r.headers)
    outcome, detail = classify(r.status, headers, body)
    detail = f"playwright: {detail}" if detail else "playwright"
    return record(outcome, detail, status=r.status, final_url=final_url, body=body,
                  content_type=headers.get("content-type"), truncated=truncated)

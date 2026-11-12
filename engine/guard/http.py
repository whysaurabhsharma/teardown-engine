"""Guard's HTTP client: the only code in the engine that touches the network.

    result = await fetch(url, kind)            # module-level, shared Guard
    async with Guard() as g:                   # or an explicit Guard per run
        result = await g.fetch(url, kind, fresh=False)

Rules (protocol/CONDUCT.md, .claude/skills/capture-conduct):
- one honest User-Agent naming the repo and $SENDER_EMAIL; no other identity.
- robots.txt checked before every request, redirect hops included; a disallowed
  URL is `robots_denied` and is not requested.
- 1 request per second per host (ratelimit.py), 4 in flight overall.
- 20 s timeout, 2 MB cap, up to 5 redirects.
- 429/503: honour Retry-After, retry once. Nothing else is retried.
- 403, 429 after its retry and challenge pages are `blocked`: an outcome, never
  something to work around.
- every response body is cached raw; a same-day repeat makes no request.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from engine.guard import robots
from engine.guard.budget import Budget, robots_url
from engine.guard.cache import Cache, CacheEntry
from engine.guard.ratelimit import MAX_IN_FLIGHT, RateLimiter
from engine.models import Outcome

REPO_URL = "https://github.com/whysaurabhsharma/teardown-engine"
SENDER_ENV_VAR = "SENDER_EMAIL"
TIMEOUT = 20.0  # seconds, whole request including body
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
RETRY_WAIT_DEFAULT = 5.0  # seconds, when a 429/503 carries no Retry-After
MAX_RETRY_WAIT = 60.0  # a longer Retry-After is not waited for; no retry
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
BLOCKED_STATUSES = frozenset({401, 403, 429, 451})
NOT_FOUND_STATUSES = frozenset({404, 410})

# Strong markers of an anti-bot interstitial. Kept narrow on purpose: scripts
# such as reCAPTCHA also appear on ordinary pages and must not flip them.
CHALLENGE_MARKERS = (
    b"<title>just a moment...</title>",
    b"attention required! | cloudflare",
    b"_cf_chl_opt",
    b"px-captcha",
)


class GuardConfigError(RuntimeError):
    """Guard cannot make an honest request (e.g. SENDER_EMAIL is not set)."""


def user_agent(sender_email: str | None = None) -> str:
    email = (sender_email if sender_email is not None else os.environ.get(SENDER_ENV_VAR, "")).strip()
    if not email or "@" not in email or any(c.isspace() for c in email):
        raise GuardConfigError(
            f"${SENDER_ENV_VAR} is not set to an email address; Guard will not make "
            "network requests without a contact in its User-Agent"
        )
    return f"teardown-engine/0.1 (+{REPO_URL}; research; contact {email})"


@dataclass(frozen=True)
class FetchResult:
    url: str  # as requested, fragment removed
    kind: str
    outcome: Outcome
    status: int | None  # final HTTP status; None if no response
    final_url: str
    body: bytes | None
    sha256: str | None  # of body, which is stored at cache_path
    cache_path: str | None
    content_type: str | None
    fetched_at: datetime
    from_cache: bool
    truncated: bool = False  # body cut at MAX_BYTES
    detail: str = ""
    requests: int = 0  # network requests this call sent (robots.txt included)

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    @property
    def text(self) -> str:
        return (self.body or b"").decode("utf-8", errors="replace")


@dataclass
class _Raw:
    status: int
    headers: httpx.Headers
    body: bytes
    truncated: bool


@dataclass
class _Done:
    outcome: Outcome
    final_url: str
    status: int | None = None
    body: bytes | None = None
    content_type: str | None = None
    truncated: bool = False
    detail: str = ""


class _Dead(Exception):
    pass


@dataclass
class _Counter:
    n: int = 0


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def is_challenge(status: int, headers: httpx.Headers, body: bytes) -> bool:
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    head = body[:65536].lower()
    return any(m in head for m in CHALLENGE_MARKERS)


def classify(status: int, headers: httpx.Headers, body: bytes) -> tuple[Outcome, str]:
    if is_challenge(status, headers, body):
        return "blocked", f"challenge page (HTTP {status})"
    if 200 <= status < 300:
        return "ok", ""
    if status in NOT_FOUND_STATUSES:
        return "not_found", f"HTTP {status}"
    if status in BLOCKED_STATUSES:
        return "blocked", f"HTTP {status}"
    return "dead", f"HTTP {status}"


def _normalise(url: str) -> str:
    url, _ = urldefrag(url.strip())
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"Guard fetches http(s) URLs only: {url!r}")
    return url


def _is_robots(url: str) -> bool:
    return urlsplit(url).path == "/robots.txt"


class Guard:
    """One run's worth of network state: pacing, robots, budget, cache, client."""

    def __init__(
        self,
        *,
        cache: Cache | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
        limiter: RateLimiter | None = None,
        budget: Budget | None = None,
        max_in_flight: int = MAX_IN_FLIGHT,
        timeout: float = TIMEOUT,
        max_bytes: int = MAX_BYTES,
        retry_wait_default: float = RETRY_WAIT_DEFAULT,
        max_retry_wait: float = MAX_RETRY_WAIT,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.cache = cache or Cache(cache_dir)
        self.limiter = limiter or RateLimiter()
        self.budget = budget or Budget()
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.retry_wait_default = retry_wait_default
        self.max_retry_wait = max_retry_wait
        self.requests_made = 0
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._in_flight = asyncio.Semaphore(max_in_flight)
        self._url_locks: dict[str, asyncio.Lock] = {}
        self._robots: dict[str, RobotFileParser | None] = {}  # None: robots host dead

    async def __aenter__(self) -> Guard:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=False,  # followed by hand so each hop is robots-checked and paced
                timeout=httpx.Timeout(self.timeout),
                transport=self._transport,
            )
        return self._client

    async def fetch(self, url: str, kind: str, *, fresh: bool = False) -> FetchResult:
        """Fetch url, from today's cache unless fresh=True. Always returns an outcome.

        Raises GuardConfigError if a request is needed and SENDER_EMAIL is unset,
        BudgetExceeded if the run's fetch ceiling would be crossed, and ValueError
        for a non-http(s) URL.
        """
        url = _normalise(url)
        ctr = _Counter()
        return await self._get(url, kind, fresh=fresh, check_robots=not _is_robots(url), ctr=ctr)

    async def _get(
        self, url: str, kind: str, *, fresh: bool, check_robots: bool, ctr: _Counter
    ) -> FetchResult:
        lock = self._url_locks.setdefault(url, asyncio.Lock())
        async with lock:
            if not fresh:
                entry = self.cache.lookup(url, self._now().date())
                if entry is not None:
                    return self._from_cache(entry, kind)
            start = ctr.n
            done = await self._network(url, check_robots, ctr)
            fetched_at = self._now()
            sha = self.cache.put_body(done.body) if done.body is not None else None
            entry = CacheEntry(
                url=url,
                final_url=done.final_url,
                kind=kind,
                status=done.status,
                outcome=done.outcome,
                sha256=sha,
                content_type=done.content_type,
                truncated=done.truncated,
                detail=done.detail,
                fetched_at=fetched_at,
            )
            self.cache.record(entry)
            return FetchResult(
                url=url,
                kind=kind,
                outcome=done.outcome,
                status=done.status,
                final_url=done.final_url,
                body=done.body,
                sha256=sha,
                cache_path=str(self.cache.body_path(sha)) if sha else None,
                content_type=done.content_type,
                fetched_at=fetched_at,
                from_cache=False,
                truncated=done.truncated,
                detail=done.detail,
                requests=ctr.n - start,
            )

    def _from_cache(self, entry: CacheEntry, kind: str) -> FetchResult:
        body = self.cache.get_body(entry.sha256) if entry.sha256 else None
        return FetchResult(
            url=entry.url,
            kind=kind,
            outcome=entry.outcome,
            status=entry.status,
            final_url=entry.final_url,
            body=body,
            sha256=entry.sha256,
            cache_path=str(self.cache.body_path(entry.sha256)) if entry.sha256 else None,
            content_type=entry.content_type,
            fetched_at=entry.fetched_at,
            from_cache=True,
            truncated=entry.truncated,
            detail=entry.detail,
            requests=0,
        )

    async def _network(self, url: str, check_robots: bool, ctr: _Counter) -> _Done:
        current = url
        hops = 0
        while True:
            if check_robots and not _is_robots(current):
                denied = await self._robots_verdict(current, ctr)
                if denied is not None:
                    return denied
            try:
                raw = await self._request(current, ctr)
            except _Dead as e:
                return _Done("dead", current, detail=str(e))
            location = raw.headers.get("location")
            if raw.status in REDIRECT_STATUSES and location:
                if hops >= MAX_REDIRECTS:
                    return _Done("dead", current, status=raw.status,
                                 detail=f"more than {MAX_REDIRECTS} redirects")
                hops += 1
                nxt = urljoin(current, location)
                try:
                    current = _normalise(nxt)
                except ValueError:
                    return _Done("dead", current, status=raw.status,
                                 detail=f"redirect to unsupported URL {nxt!r}")
                continue
            outcome, detail = classify(raw.status, raw.headers, raw.body)
            if raw.truncated:
                cut = f"body cut at {self.max_bytes} bytes"
                detail = f"{detail}; {cut}" if detail else cut
            return _Done(
                outcome,
                current,
                status=raw.status,
                body=raw.body,
                content_type=raw.headers.get("content-type"),
                truncated=raw.truncated,
                detail=detail,
            )

    async def _robots_verdict(self, url: str, ctr: _Counter) -> _Done | None:
        """None if robots.txt allows url; otherwise the outcome to return instead."""
        rurl = robots_url(url)
        if rurl not in self._robots:
            res = await self._get(rurl, "robots", fresh=False, check_robots=False, ctr=ctr)
            if res.status is None:
                self._robots[rurl] = None
            else:
                status = 403 if res.outcome == "blocked" else res.status
                rp = robots.policy_from_response(status, res.body)
                self._robots[rurl] = rp
                delay = robots.crawl_delay(rp)
                if delay:
                    self.limiter.apply_crawl_delay(url, delay)
        rp = self._robots[rurl]
        if rp is None:
            return _Done("dead", url, detail=f"robots.txt unreachable at {rurl}")
        if not robots.allowed(rp, url):
            return _Done("robots_denied", url, detail=f"disallowed by {rurl}")
        return None

    async def _request(self, url: str, ctr: _Counter) -> _Raw:
        raw = await self._send(url, ctr)
        if raw.status in (429, 503) and not is_challenge(raw.status, raw.headers, raw.body):
            wait = parse_retry_after(raw.headers.get("retry-after"), self._now())
            if wait is None:
                wait = self.retry_wait_default
            if wait <= self.max_retry_wait:
                await self._sleep(wait)
                raw = await self._send(url, ctr)  # the one retry; its answer stands
        return raw

    async def _send(self, url: str, ctr: _Counter) -> _Raw:
        headers = {"User-Agent": user_agent()}
        self.budget.charge_fetch()
        client = self._http()  # built before the token is spent, so the send follows it closely
        await self.limiter.acquire(url, then=self._in_flight.acquire)
        try:
            ctr.n += 1
            self.requests_made += 1
            async with asyncio.timeout(self.timeout):
                async with client.stream("GET", url, headers=headers) as resp:
                    body = bytearray()
                    truncated = False
                    async for chunk in resp.aiter_bytes():
                        room = self.max_bytes - len(body)
                        if len(chunk) > room:
                            body += chunk[:room]
                            truncated = True
                            break
                        body += chunk
                    return _Raw(resp.status_code, resp.headers, bytes(body), truncated)
        except TimeoutError:
            raise _Dead(f"timeout after {self.timeout:g}s") from None
        except httpx.HTTPError as e:
            raise _Dead(f"{type(e).__name__}: {e}".strip()) from None
        finally:
            self._in_flight.release()


_default: tuple[asyncio.AbstractEventLoop, Guard] | None = None


def default_guard() -> Guard:
    """The shared Guard for the running event loop (a new loop gets a new Guard)."""
    global _default
    loop = asyncio.get_running_loop()
    if _default is None or _default[0] is not loop:
        _default = (loop, Guard())
    return _default[1]


async def fetch(url: str, kind: str, *, fresh: bool = False) -> FetchResult:
    """Fetch through the shared Guard. See Guard.fetch."""
    return await default_guard().fetch(url, kind, fresh=fresh)

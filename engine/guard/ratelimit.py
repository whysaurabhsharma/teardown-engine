"""Pacing: a token bucket per provider or host, limits loaded from protocol/BUDGET.md.

No host ever sees more than DEFAULT_RPS requests per second, whatever BUDGET.md
says: a provider row can only slow a host down. Buckets hold one token, so there
are no bursts. MAX_IN_FLIGHT caps concurrent requests across all hosts; Guard
owns that semaphore and passes it in as `then` so a token is only spent at the
moment a request can actually go out.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_RPS = 1.0  # CLAUDE.md: 1 request per second per host
MAX_IN_FLIGHT = 4  # CONDUCT.md: at most 4 in flight overall
BUDGET_PATH = Path(__file__).resolve().parents[2] / "protocol" / "BUDGET.md"

# Which hosts belong to which BUDGET.md provider row (suffix match on hostname).
# Every host of one provider shares one bucket.
PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
    "ashby": ("ashbyhq.com",),
}

_RPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*rps\b", re.IGNORECASE)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


def load_limits(path: str | Path | None = None) -> dict[str, float]:
    """Provider -> requests per second, from the 'Default applied' column.

    Rows without an "N rps" figure (subprocess, local, n/a) are not HTTP limits
    and are skipped. A missing file yields no limits; the default then applies.
    """
    p = Path(path) if path is not None else BUDGET_PATH
    if not p.exists():
        return {}
    limits: dict[str, float] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not line.lstrip().startswith("|"):
            continue
        provider, applied = cells[0].lower(), cells[2]
        m = _RPS_RE.search(applied)
        if m and float(m.group(1)) > 0:
            limits[provider] = float(m.group(1))
    return limits


def host_key(url: str) -> str:
    """The per-host identity used for pacing: hostname plus port if given."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return f"{host}:{parts.port}" if parts.port else host


def provider_for(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    for provider, suffixes in PROVIDER_HOSTS.items():
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return provider
    return None


class TokenBucket:
    """Capacity-one token bucket: consecutive acquisitions are >= 1/rate apart."""

    def __init__(self, rate: float, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self._clock = clock
        self._sleep = sleep
        self._tokens = 1.0
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(1.0, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def slow_to(self, rate: float) -> None:
        """Lower the rate (e.g. for a robots.txt Crawl-delay). Never raises it."""
        if 0 < rate < self.rate:
            self._refill()
            self.rate = rate

    async def acquire(self, then: Callable[[], Awaitable[object]] | None = None) -> None:
        """Wait for a token; run `then` (still holding this bucket); spend the token."""
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= 1.0:
                    break
                await self._sleep((1.0 - self._tokens) / self.rate)
            if then is not None:
                await then()
                self._refill()
            self._tokens -= 1.0


class RateLimiter:
    """One TokenBucket per provider (from BUDGET.md) or, failing that, per host."""

    def __init__(
        self,
        limits: dict[str, float] | None = None,
        default_rps: float = DEFAULT_RPS,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ):
        self.limits = load_limits() if limits is None else dict(limits)
        self.default_rps = default_rps
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}

    def key_for(self, url: str) -> tuple[str, float]:
        provider = provider_for(url)
        if provider is not None and provider in self.limits:
            return provider, min(self.limits[provider], self.default_rps)
        return host_key(url), self.default_rps

    def bucket(self, url: str) -> TokenBucket:
        key, rate = self.key_for(url)
        b = self._buckets.get(key)
        if b is None:
            b = self._buckets[key] = TokenBucket(rate, self._clock, self._sleep)
        return b

    def apply_crawl_delay(self, url: str, delay_seconds: float) -> None:
        if delay_seconds > 0:
            self.bucket(url).slow_to(1.0 / delay_seconds)

    async def acquire(self, url: str, then: Callable[[], Awaitable[object]] | None = None) -> None:
        await self.bucket(url).acquire(then)

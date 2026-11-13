"""Per-run ceilings on network requests, DNS queries and model calls, and the --dry-run projection.

Guard charges one fetch for every HTTP request it actually sends (robots.txt,
redirect hops and the one Retry-After retry included); cache hits are free.
Going over a ceiling raises BudgetExceeded before the request is sent, which
stops the run rather than recording an outcome: it is our limit, not the site's.
guard.dns charges one DNS query for every lookup it sends, against its own
ceiling, so the fixed DNS list never eats the page-fetch ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from engine.guard.cache import Cache

DEFAULT_MAX_FETCHES = 500
DEFAULT_MAX_MODEL_CALLS = 200
DEFAULT_MAX_DNS_QUERIES = 1000


class BudgetExceeded(RuntimeError):
    """A per-run ceiling would be crossed."""


@dataclass
class Budget:
    max_fetches: int = DEFAULT_MAX_FETCHES
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS
    fetches: int = 0
    model_calls: int = 0
    max_dns_queries: int = DEFAULT_MAX_DNS_QUERIES
    dns_queries: int = 0

    def charge_fetch(self, n: int = 1) -> None:
        if self.fetches + n > self.max_fetches:
            raise BudgetExceeded(
                f"fetch ceiling reached: {self.fetches} of {self.max_fetches} used this run"
            )
        self.fetches += n

    def charge_dns(self, n: int = 1) -> None:
        if self.dns_queries + n > self.max_dns_queries:
            raise BudgetExceeded(
                f"DNS query ceiling reached: {self.dns_queries} of {self.max_dns_queries} used this run"
            )
        self.dns_queries += n

    def charge_model_call(self, n: int = 1) -> None:
        if self.model_calls + n > self.max_model_calls:
            raise BudgetExceeded(
                f"model call ceiling reached: {self.model_calls} of {self.max_model_calls} used this run"
            )
        self.model_calls += n

    @property
    def remaining_fetches(self) -> int:
        return self.max_fetches - self.fetches

    @property
    def remaining_model_calls(self) -> int:
        return self.max_model_calls - self.model_calls


@dataclass(frozen=True)
class Projection:
    urls: int
    cached: int  # answered from today's cache, no request
    page_fetches: int
    robots_fetches: int  # robots.txt files not yet cached today
    model_calls: int = 0

    @property
    def fetches(self) -> int:
        """Projected requests. A floor: redirects and Retry-After retries add to it."""
        return self.page_fetches + self.robots_fetches


def robots_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


def project(
    urls: list[str],
    cache: Cache | None = None,
    *,
    fresh: bool = False,
    model_calls: int = 0,
    day: date | None = None,
) -> Projection:
    """Count the requests a run over `urls` would send. Sends nothing."""
    cache = cache or Cache()
    day = day or datetime.now(UTC).date()
    unique = list(dict.fromkeys(urls))
    cached = 0 if fresh else sum(1 for u in unique if cache.lookup(u, day) is not None)
    origins = {robots_url(u) for u in unique if urlsplit(u).path != "/robots.txt"}
    robots = sum(1 for r in origins if cache.lookup(r, day) is None)
    return Projection(
        urls=len(unique),
        cached=cached,
        page_fetches=len(unique) - cached,
        robots_fetches=robots,
        model_calls=model_calls,
    )


def format_projection(p: Projection, budget: Budget | None = None) -> str:
    budget = budget or Budget()
    line = (
        f"dry run: {p.urls} urls, {p.cached} cached today, "
        f"{p.fetches} requests projected ({p.page_fetches} pages, {p.robots_fetches} robots.txt); "
        f"ceiling {budget.max_fetches} fetches"
    )
    if p.model_calls:
        line += f"; {p.model_calls} model calls projected, ceiling {budget.max_model_calls}"
    if p.fetches > budget.max_fetches or p.model_calls > budget.max_model_calls:
        line += "; OVER BUDGET"
    return line

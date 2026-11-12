"""Guard's web search hook: find a company's official site by name.

    urls = await search_site("Zentrico Labs")     # raises NotConfigured without a key

Search only finds a site (protocol/CONDUCT.md); the site itself, fetched
through guard.http, is what verifies it. Only engine/guard/ touches the
network, so engine/resolve/ calls this and never an API directly.

Provider, chosen from the environment at call time (never from .env here; the
CLI loads it): EXA_API_KEY first, then TAVILY_API_KEY. With neither set,
NotConfigured is raised and the caller records the company for review rather
than guessing.

Rules:
- one POST per query, 20 s timeout, no redirects, no retries.
- calls to one provider are at least 1/DEFAULT_RPS seconds apart.
- each call is charged to the run's Budget as one fetch when a Budget is given.
- the raw response is cached through guard.cache under a search:// key, so the
  same query on the same UTC day is answered without a call.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from engine.guard.budget import Budget
from engine.guard.cache import Cache, CacheEntry
from engine.guard.ratelimit import DEFAULT_RPS

EXA_ENV = "EXA_API_KEY"
TAVILY_ENV = "TAVILY_API_KEY"
EXA_URL = "https://api.exa.ai/search"
TAVILY_URL = "https://api.tavily.com/search"
TIMEOUT = 20.0
MAX_RESULTS = 5
FETCH_KIND = "search"

# Tests replace this with httpx.MockTransport; None means the real network.
transport: httpx.AsyncBaseTransport | None = None

_last_call: dict[str, float] = {}
_pace_lock = asyncio.Lock()


class NotConfigured(RuntimeError):
    """No search provider key is set in the environment."""


class SearchError(RuntimeError):
    """The search provider answered with an error or a body we cannot read."""


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    key: str


def provider() -> Provider:
    """The configured provider, or NotConfigured."""
    exa = os.environ.get(EXA_ENV, "").strip()
    if exa:
        return Provider("exa", EXA_URL, exa)
    tavily = os.environ.get(TAVILY_ENV, "").strip()
    if tavily:
        return Provider("tavily", TAVILY_URL, tavily)
    raise NotConfigured(f"no web search provider: set ${EXA_ENV} or ${TAVILY_ENV}")


def is_configured() -> bool:
    try:
        provider()
    except NotConfigured:
        return False
    return True


def site_query(company_name: str) -> str:
    return f"{' '.join(company_name.split())} official website"


def _request(p: Provider, query: str) -> tuple[dict[str, str], dict[str, object]]:
    if p.name == "exa":
        return ({"x-api-key": p.key},
                {"query": query, "numResults": MAX_RESULTS, "type": "keyword"})
    return ({"Authorization": f"Bearer {p.key}"},
            {"query": query, "max_results": MAX_RESULTS, "search_depth": "basic"})


def result_urls(payload: object) -> list[str]:
    """Result URLs from an Exa or Tavily response (both use results[].url)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise SearchError("search response has no results list")
    return [r["url"] for r in payload["results"]
            if isinstance(r, dict) and isinstance(r.get("url"), str) and r["url"].strip()]


async def _pace(name: str) -> None:
    gap = 1.0 / DEFAULT_RPS
    async with _pace_lock:
        last = _last_call.get(name)
        now = time.monotonic()
        if last is not None and now - last < gap:
            await asyncio.sleep(gap - (now - last))
        _last_call[name] = time.monotonic()


async def search_site(
    company_name: str,
    *,
    cache: Cache | None = None,
    budget: Budget | None = None,
) -> list[str]:
    """URLs a web search returns for the company's official site.

    Raises NotConfigured when no provider key is set, SearchError on a failed
    call, and BudgetExceeded when the run's fetch ceiling would be crossed.
    """
    p = provider()
    query = site_query(company_name)
    key = f"search://{p.name}?q={quote(query)}"
    cache = cache or Cache()
    today = datetime.now(UTC).date()
    hit = cache.lookup(key, today)
    if hit is not None and hit.sha256:
        body = cache.get_body(hit.sha256) or b""
        return result_urls(json.loads(body))

    if budget is not None:
        budget.charge_fetch()
    await _pace(p.name)
    headers, payload = _request(p, query)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT,
                                     follow_redirects=False) as client:
            resp = await client.post(p.url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise SearchError(f"{p.name}: request failed: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise SearchError(f"{p.name}: HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise SearchError(f"{p.name}: response is not JSON") from exc
    urls = result_urls(data)
    sha = cache.put_body(resp.content)
    cache.record(CacheEntry(
        url=key, final_url=key, kind=FETCH_KIND, status=resp.status_code, outcome="ok",
        sha256=sha, content_type=resp.headers.get("content-type"),
        fetched_at=datetime.now(UTC),
    ))
    return urls

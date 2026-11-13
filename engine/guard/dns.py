"""Guarded DNS lookups (protocol/CONDUCT.md, "DNS").

    async with DnsGuard(cache=cache, budget=budget) as d:
        answer = await d.lookup("_dmarc.zentrico.example", "TXT")

DNS lookups are capture like any page fetch, so they follow the same rules:

- only engine/guard/ sends them. The caller (engine/capture/) passes names from
  the fixed, versioned list in engine/capture/bundle.py; this module looks up
  exactly the (name, type) it is given and nothing else. No zone transfers, no
  wordlists, no certificate-log sweeps.
- each query is charged to the run budget (Budget.charge_dns) before it is
  sent, and paced: at most one query per second per registrable domain, the
  same 1 rps rule guard.http applies per host.
- a timeout per query (DNS_TIMEOUT); nothing is retried.
- NXDOMAIN or an empty answer is `not_found`, a fact. A timeout, SERVFAIL or
  no reachable nameserver is `dead`: the name could not be seen.
- every answer is cached for the UTC day: the records as JSON, stored raw under
  data/cache/<sha256> like a response body, indexed under "dns:<TYPE>:<name>".
  A same-day repeat is a cache hit and sends no query. fresh=True bypasses it.

The resolver is pluggable (Resolver protocol); the default uses dnspython's
async resolver with the system's configured nameservers. Tests pass a fake.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from engine.guard.budget import Budget
from engine.guard.cache import Cache, CacheEntry
from engine.guard.ratelimit import RateLimiter

RdType = Literal["A", "CNAME", "MX", "TXT"]
RDTYPES: frozenset[str] = frozenset({"A", "CNAME", "MX", "TXT"})
DnsOutcome = Literal["ok", "not_found", "dead"]
DNS_TIMEOUT = 5.0  # seconds per query, all nameservers included
CACHE_PREFIX = "dns:"
NXDOMAIN = "NXDOMAIN"  # the detail of a not_found answer for a name that does not exist


class NameNotFound(Exception):
    """The name does not exist (NXDOMAIN) or has no record of the asked type."""


class LookupFailed(Exception):
    """No definitive answer: timeout, SERVFAIL, no reachable nameserver."""


class Resolver(Protocol):
    async def resolve(self, name: str, rdtype: str, timeout: float) -> list[str]:
        """Records as text. Raises NameNotFound or LookupFailed."""
        ...


class DnspythonResolver:
    """dnspython's async stub resolver against the system's nameservers."""

    async def resolve(self, name: str, rdtype: str, timeout: float) -> list[str]:
        import dns.asyncresolver
        import dns.exception
        import dns.resolver

        try:
            answer = await dns.asyncresolver.resolve(name, rdtype, lifetime=timeout,
                                                     search=False)
        except dns.resolver.NXDOMAIN:
            raise NameNotFound(NXDOMAIN) from None
        except dns.resolver.NoAnswer:
            raise NameNotFound(f"no {rdtype} record") from None
        except dns.exception.Timeout:
            raise LookupFailed(f"timeout after {timeout:g}s") from None
        except dns.resolver.NoNameservers as e:
            raise LookupFailed(f"no nameserver answered ({type(e).__name__})") from None
        except dns.exception.DNSException as e:
            raise LookupFailed(f"{type(e).__name__}: {e}") from None
        return [_text(rdtype, r) for r in answer]


def _text(rdtype: str, rdata: object) -> str:
    if rdtype == "TXT":
        strings = getattr(rdata, "strings", ())
        return "".join(s.decode("utf-8", errors="replace") for s in strings)
    if rdtype == "MX":
        return f"{getattr(rdata, 'preference')} {str(getattr(rdata, 'exchange')).rstrip('.')}"
    if rdtype == "CNAME":
        return str(getattr(rdata, "target")).rstrip(".")
    return str(rdata)


@dataclass(frozen=True)
class DnsAnswer:
    name: str
    rdtype: str
    outcome: DnsOutcome
    records: tuple[str, ...]
    detail: str
    fetched_at: datetime
    from_cache: bool
    queries: int  # queries this call sent (0 for a cache hit)

    @property
    def nxdomain(self) -> bool:
        return self.outcome == "not_found" and self.detail == NXDOMAIN

    def as_json(self) -> dict[str, object]:
        return {"name": self.name, "type": self.rdtype, "outcome": self.outcome,
                "records": list(self.records), "detail": self.detail}


def _norm(name: str) -> str:
    name = name.strip().lower().rstrip(".")
    if not name or any(c.isspace() for c in name) or "/" in name or ".." in name:
        raise ValueError(f"not a DNS name: {name!r}")
    return name


def _zone(name: str) -> str:
    """The registrable domain a name sits under, the pacing key."""
    from engine.resolve.domain import registrable_domain

    return registrable_domain(name) or name


class DnsGuard:
    def __init__(
        self,
        *,
        cache: Cache | None = None,
        budget: Budget | None = None,
        limiter: RateLimiter | None = None,
        resolver: Resolver | None = None,
        timeout: float = DNS_TIMEOUT,
        now: Callable[[], datetime] | None = None,
    ):
        self.cache = cache or Cache()
        self.budget = budget or Budget()
        # Its own limiter: DNS buckets are keyed by zone, never shared with HTTP hosts.
        self.limiter = limiter or RateLimiter(limits={})
        self.resolver: Resolver = resolver or DnspythonResolver()
        self.timeout = timeout
        self.queries_made = 0
        self._now = now or (lambda: datetime.now(UTC))

    async def __aenter__(self) -> DnsGuard:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @staticmethod
    def cache_key(name: str, rdtype: str) -> str:
        return f"{CACHE_PREFIX}{rdtype}:{name}"

    def cached(self, name: str, rdtype: str) -> bool:
        """True if today's cache answers this lookup (no query needed)."""
        return self.cache.lookup(self.cache_key(_norm(name), rdtype), self._now().date()) is not None

    async def lookup(self, name: str, rdtype: str, *, fresh: bool = False) -> DnsAnswer:
        """One lookup, from today's cache unless fresh. Always returns an outcome.

        Raises BudgetExceeded when the run's DNS ceiling would be crossed and
        ValueError for an unknown record type or a malformed name.
        """
        if rdtype not in RDTYPES:
            raise ValueError(f"guard.dns looks up {sorted(RDTYPES)} only: {rdtype!r}")
        name = _norm(name)
        key = self.cache_key(name, rdtype)
        if not fresh:
            entry = self.cache.lookup(key, self._now().date())
            if entry is not None:
                body = self.cache.get_body(entry.sha256) if entry.sha256 else None
                records = tuple(json.loads(body)) if body else ()
                return DnsAnswer(name, rdtype, entry.outcome, records,  # type: ignore[arg-type]
                                 entry.detail, entry.fetched_at, True, 0)

        self.budget.charge_dns()
        await self.limiter.acquire(f"dns://{_zone(name)}/")
        self.queries_made += 1
        records: tuple[str, ...] = ()
        try:
            got = await self.resolver.resolve(name, rdtype, self.timeout)
            records = tuple(sorted({r.strip() for r in got if r and r.strip()}))
            outcome: DnsOutcome = "ok" if records else "not_found"
            detail = "" if records else f"no {rdtype} record"
        except NameNotFound as e:
            outcome, detail = "not_found", str(e) or NXDOMAIN
        except LookupFailed as e:
            outcome, detail = "dead", str(e) or "lookup failed"
        fetched_at = self._now()
        sha = self.cache.put_body(json.dumps(list(records)).encode("utf-8")) if records else None
        self.cache.record(CacheEntry(url=key, final_url=key, kind="dns", status=None,
                                     outcome=outcome, sha256=sha, detail=detail,
                                     fetched_at=fetched_at))
        return DnsAnswer(name, rdtype, outcome, records, detail, fetched_at, False, 1)

"""Guard's client for the address finders (docs/PLAN.md Step 42): Hunter, then Apollo.

    found = hunter_find("velmora.example", "Ilsa", "Marrow")    # FinderResult | None
    found = apollo_find("velmora.example", "Ilsa", "Marrow")
    check = hunter_verify("ilsa@velmora.example")               # VerifyResult

Only engine/guard/ touches the network, so the People step calls these and
never an API directly. Keys come from the environment at call time
(HUNTER_API_KEY, APOLLO_API_KEY); this module never reads .env. Without the
key, NotConfigured is raised before anything is sent.

Rules (protocol/CONDUCT.md Part 2, "People's data"):
- Hunter's finder first, Apollo second, then a pattern checked with Hunter's
  verifier. Mail servers are never contacted directly, by any route: no
  connection to a mail host, no recipient probing. These are HTTPS API calls
  to the two providers and nothing else.
- one request per call, TIMEOUT seconds, no redirects, no retries.
- calls to one provider are at least 1/DEFAULT_RPS seconds apart.
- each call is charged to the run's Budget as one fetch when a Budget is given.
- responses hold personal data, so nothing is cached and no error message
  carries a response body, a name or an address: only the provider and the
  HTTP status.
- the API key goes in a header, never in the URL.

Confidence (contacts.confidence, 0012):
  Hunter finder   score >= 90 high, >= 70 medium, else low; a result Hunter
                  itself marks invalid is no result.
  Apollo match    email_status "verified" medium, anything else low; a
                  locked placeholder address is no result.
  Hunter verifier status "valid" medium; "accept_all", "webmail", "unknown"
                  low; "invalid" or "disposable" rejects the address.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from engine.guard.budget import Budget
from engine.guard.ratelimit import DEFAULT_RPS

HUNTER_ENV = "HUNTER_API_KEY"
APOLLO_ENV = "APOLLO_API_KEY"
HUNTER_FINDER_URL = "https://api.hunter.io/v2/email-finder"
HUNTER_VERIFIER_URL = "https://api.hunter.io/v2/email-verifier"
APOLLO_MATCH_URL = "https://api.apollo.io/api/v1/people/match"
TIMEOUT = 20.0

Confidence = Literal["high", "medium", "low"]

# Tests replace this with httpx.MockTransport; None means the real network.
transport: httpx.BaseTransport | None = None

_last_call: dict[str, float] = {}
_pace_lock = threading.Lock()


class NotConfigured(RuntimeError):
    """The provider's API key is not set in the environment."""


class FinderError(RuntimeError):
    """The provider answered with an error or a body we cannot read."""


@dataclass(frozen=True)
class FinderResult:
    email: str
    confidence: Confidence
    source: Literal["hunter", "apollo"]


@dataclass(frozen=True)
class VerifyResult:
    status: str  # the provider's own word
    confidence: Confidence | None  # None: the address is rejected


def _key(env: str) -> str:
    key = os.environ.get(env, "").strip()
    if not key:
        raise NotConfigured(f"${env} is not set; the finder is not configured")
    return key


def hunter_configured() -> bool:
    return bool(os.environ.get(HUNTER_ENV, "").strip())


def apollo_configured() -> bool:
    return bool(os.environ.get(APOLLO_ENV, "").strip())


def _pace(provider: str, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
    gap = 1.0 / DEFAULT_RPS
    with _pace_lock:
        last = _last_call.get(provider)
        now = clock()
        if last is not None and now - last < gap:
            sleep(gap - (now - last))
            now = clock()
        _last_call[provider] = now


def _send(provider: str, method: str, url: str, *, headers: dict[str, str],
          params: dict[str, str] | None = None, payload: dict[str, Any] | None = None,
          budget: Budget | None = None, clock: Callable[[], float] = time.monotonic,
          sleep: Callable[[float], None] = time.sleep) -> Any:
    if budget is not None:
        budget.charge_fetch()
    _pace(provider, clock, sleep)
    try:
        with httpx.Client(transport=transport, timeout=TIMEOUT, follow_redirects=False) as client:
            resp = client.request(method, url, params=params, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise FinderError(f"{provider}: request failed: {type(exc).__name__}") from None
    if resp.status_code == 404:
        return None  # nothing found for that person
    if resp.status_code >= 400:
        raise FinderError(f"{provider}: HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise FinderError(f"{provider}: response is not JSON") from None


def _hunter_level(score: Any) -> Confidence:
    s = score if isinstance(score, (int, float)) and not isinstance(score, bool) else 0
    return "high" if s >= 90 else "medium" if s >= 70 else "low"


def hunter_find(domain: str, first: str, last: str, *, budget: Budget | None = None,
                **pace: Any) -> FinderResult | None:
    """Hunter's email finder for one person at a domain."""
    key = _key(HUNTER_ENV)
    body = _send("hunter", "GET", HUNTER_FINDER_URL, headers={"X-API-KEY": key},
                 params={"domain": domain, "first_name": first, "last_name": last},
                 budget=budget, **pace)
    data = (body or {}).get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("email"), str) or not data["email"]:
        return None
    verification = data.get("verification") if isinstance(data.get("verification"), dict) else {}
    if verification.get("status") == "invalid":
        return None
    return FinderResult(data["email"], _hunter_level(data.get("score")), "hunter")


def apollo_find(domain: str, first: str, last: str, *, budget: Budget | None = None,
                **pace: Any) -> FinderResult | None:
    """Apollo's people match for one person at a domain (no personal emails)."""
    key = _key(APOLLO_ENV)
    body = _send("apollo", "POST", APOLLO_MATCH_URL,
                 headers={"X-Api-Key": key, "Content-Type": "application/json"},
                 payload={"first_name": first, "last_name": last, "domain": domain,
                          "reveal_personal_emails": False},
                 budget=budget, **pace)
    person = (body or {}).get("person") if isinstance(body, dict) else None
    if not isinstance(person, dict):
        return None
    email = person.get("email")
    if not isinstance(email, str) or "@" not in email or email.startswith("email_not_unlocked"):
        return None
    level: Confidence = "medium" if person.get("email_status") == "verified" else "low"
    return FinderResult(email, level, "apollo")


_VERIFY_LEVEL: dict[str, Confidence | None] = {
    "valid": "medium",
    "accept_all": "low",
    "webmail": "low",
    "unknown": "low",
    "invalid": None,
    "disposable": None,
}


def hunter_verify(email: str, *, budget: Budget | None = None, **pace: Any) -> VerifyResult:
    """Hunter's verifier for one address (Hunter checks it; we never touch the mail host)."""
    key = _key(HUNTER_ENV)
    body = _send("hunter", "GET", HUNTER_VERIFIER_URL, headers={"X-API-KEY": key},
                 params={"email": email}, budget=budget, **pace)
    data = (body or {}).get("data") if isinstance(body, dict) else None
    status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(status, str):
        return VerifyResult("unknown", "low")
    return VerifyResult(status, _VERIFY_LEVEL.get(status, "low"))

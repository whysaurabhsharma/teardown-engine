"""Guard's client for model-provider HTTP APIs (Groq, Gemini, Cloudflare Workers AI).

Only engine/guard/ touches the network, so the HTTP-API model providers in
engine/llm/ build a request and hand it here. This module never reads .env:
callers pass the API key they read from the environment at run time.

    data = post_json("groq", url, payload, headers={"Authorization": ...})

Rules:
- one POST per call, TIMEOUT seconds, no redirects, no retries here; the
  gateway decides about retries and fallbacks.
- calls to one provider are at least 1/DEFAULT_RPS seconds apart.
- HTTP 429, or a 4xx whose body speaks of a quota or rate limit, raises
  ModelApiQuota so the gateway can fall back to the route's second provider.
- any other failure raises ModelApiError with the status and a short excerpt.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from engine.guard.ratelimit import DEFAULT_RPS

TIMEOUT = 120.0  # seconds; model calls are slow but must not hang a run
EXCERPT = 300  # characters of an error body kept in an exception message

_QUOTA_RE = re.compile(r"quota|rate.?limit|resource.?exhausted|too many requests", re.IGNORECASE)

# Tests replace this with httpx.MockTransport; None means the real network.
transport: httpx.BaseTransport | None = None

_last_call: dict[str, float] = {}
_pace_lock = threading.Lock()


class ModelApiError(RuntimeError):
    """A model API call failed for a reason other than quota."""


class ModelApiQuota(ModelApiError):
    """The provider refused for quota or rate limit (HTTP 429 or a quota body)."""


def _pace(provider: str, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
    gap = 1.0 / DEFAULT_RPS
    with _pace_lock:
        last = _last_call.get(provider)
        now = clock()
        if last is not None and now - last < gap:
            sleep(gap - (now - last))
            now = clock()
        _last_call[provider] = now


def post_json(
    provider: str,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    *,
    timeout: float = TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """POST `payload` as JSON to a model API and return the decoded JSON body."""
    _pace(provider, clock, sleep)
    try:
        with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False) as client:
            resp = client.post(url, json=payload, headers=headers or {})
    except httpx.HTTPError as exc:
        raise ModelApiError(f"{provider}: request failed: {type(exc).__name__}") from exc
    body = resp.text
    if resp.status_code == 429 or (400 <= resp.status_code < 500 and _QUOTA_RE.search(body)):
        raise ModelApiQuota(f"{provider}: HTTP {resp.status_code} quota or rate limit")
    if resp.status_code >= 400:
        raise ModelApiError(f"{provider}: HTTP {resp.status_code}: {body[:EXCERPT]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ModelApiError(f"{provider}: response is not JSON") from exc

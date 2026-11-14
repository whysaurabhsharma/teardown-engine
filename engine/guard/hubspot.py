"""Guard's HubSpot client: the CRM view of the campaign (docs/PLAN.md Steps 67, 76).

    from engine.guard import hubspot
    hs = hubspot.Client.from_env()          # NotConfigured without $HUBSPOT_TOKEN
    hs.search("companies", "domain", "acme.example")
    hs.create("deals", {...}); hs.update("deals", deal_id, {...})
    hs.associate("deals", deal_id, "companies", company_id)
    hs.deal_pipeline("Teardown outreach")   # label -> stage id

Only engine/guard/ touches the network, so `te sync` (engine/outreach/
hubspot_sync.py) calls this and never the HubSpot API itself. SQLite stays the
system of record; HubSpot is the view shown to people.

Rules:
- the token comes from $HUBSPOT_TOKEN at call time (the CLI loads .env; this
  module never reads it), sent only as an `Authorization: Bearer` header. No
  exception message, log line or repr carries it.
- one request per call, TIMEOUT seconds, no redirects, no retries: a failed
  run is simply re-run (te sync is idempotent).
- requests are at least 1/RPS seconds apart (HubSpot allows 100 per 10 s for
  a private app; this stays far under it).
- each request is charged to the run's Budget as one fetch.
- errors name the method, the path (HubSpot ids only) and the status, plus
  HubSpot's error `category` when it gives one. Never the response body or the
  request body: either may echo a contact's address.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from engine.guard.budget import Budget

TOKEN_ENV = "HUBSPOT_TOKEN"
BASE_URL = "https://api.hubapi.com"
TIMEOUT = 20.0
RPS = 5.0
OBJECTS = ("companies", "contacts", "deals")

# Tests replace this with httpx.MockTransport; None means the real network.
transport: httpx.BaseTransport | None = None

_last_call: list[float] = []
_pace_lock = threading.Lock()


class NotConfigured(RuntimeError):
    """$HUBSPOT_TOKEN is not set."""


class HubSpotError(RuntimeError):
    """A HubSpot request failed. The message never carries the token or a body."""


def token_from_env() -> str:
    tok = os.environ.get(TOKEN_ENV, "").strip()
    if not tok:
        raise NotConfigured(f"${TOKEN_ENV} is not set: create a HubSpot private app with read "
                            "and write on contacts, companies and deals, and put its token "
                            "in .env (docs/PLAN.md Step 67)")
    return tok


def is_configured() -> bool:
    return bool(os.environ.get(TOKEN_ENV, "").strip())


def _pace(clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
    gap = 1.0 / RPS
    with _pace_lock:
        now = clock()
        if _last_call and now - _last_call[0] < gap:
            sleep(gap - (now - _last_call[0]))
            now = clock()
        _last_call[:] = [now]


@dataclass
class Client:
    token: str = field(repr=False)
    budget: Budget = field(default_factory=lambda: Budget(max_fetches=1000))
    base_url: str = BASE_URL
    timeout: float = TIMEOUT
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    requests: int = 0

    @classmethod
    def from_env(cls, budget: Budget | None = None) -> Client:
        return cls(token_from_env(), budget or Budget(max_fetches=1000))

    def __repr__(self) -> str:  # never the token
        return f"hubspot.Client(base_url={self.base_url!r}, requests={self.requests})"

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """One request; the decoded JSON body ({} for an empty one)."""
        self.budget.charge_fetch()
        _pace(self.clock, self.sleep)
        self.requests += 1
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        try:
            with httpx.Client(transport=transport, timeout=self.timeout,
                              follow_redirects=False) as client:
                resp = client.request(method, self.base_url + path, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise HubSpotError(f"{method} {path}: request failed ({type(e).__name__})") from None
        if resp.status_code >= 400:
            category = ""
            try:
                data = resp.json()
                if isinstance(data, dict) and isinstance(data.get("category"), str):
                    category = f", {data['category'][:60]}"
            except ValueError:
                pass
            hint = " (check HUBSPOT_TOKEN and its scopes)" if resp.status_code in (401, 403) \
                else ""
            raise HubSpotError(f"{method} {path}: HTTP {resp.status_code}{category}{hint}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise HubSpotError(f"{method} {path}: response is not JSON") from None

    # -- CRM objects ------------------------------------------------------------------------

    def search(self, obj: str, prop: str, value: str) -> str | None:
        """The id of the first `obj` record whose `prop` equals `value`, or None."""
        _check(obj)
        data = self.request("POST", f"/crm/v3/objects/{obj}/search", {
            "filterGroups": [{"filters": [{"propertyName": prop, "operator": "EQ",
                                           "value": value}]}],
            "properties": [prop], "limit": 1})
        results = data.get("results") if isinstance(data, dict) else None
        if isinstance(results, list) and results and isinstance(results[0], dict):
            rid = results[0].get("id")
            return str(rid) if rid is not None else None
        return None

    def create(self, obj: str, properties: dict[str, str]) -> str:
        _check(obj)
        data = self.request("POST", f"/crm/v3/objects/{obj}", {"properties": properties})
        rid = data.get("id") if isinstance(data, dict) else None
        if rid is None:
            raise HubSpotError(f"POST /crm/v3/objects/{obj}: no id in the response")
        return str(rid)

    def update(self, obj: str, record_id: str, properties: dict[str, str]) -> None:
        _check(obj)
        self.request("PATCH", f"/crm/v3/objects/{obj}/{_id(record_id)}",
                     {"properties": properties})

    def associate(self, from_obj: str, from_id: str, to_obj: str, to_id: str) -> None:
        """The default (unlabelled) association between two records."""
        _check(from_obj)
        _check(to_obj)
        self.request("PUT", f"/crm/v4/objects/{from_obj}/{_id(from_id)}/associations/default/"
                            f"{to_obj}/{_id(to_id)}")

    def deal_pipeline(self, label: str) -> tuple[str, dict[str, str]]:
        """(pipeline id, {stage label: stage id}) of the deal pipeline named `label`."""
        data = self.request("GET", "/crm/v3/pipelines/deals")
        for p in (data.get("results") or []) if isinstance(data, dict) else []:
            if isinstance(p, dict) and p.get("label") == label:
                stages = {s["label"]: str(s["id"]) for s in p.get("stages") or []
                          if isinstance(s, dict) and "label" in s and "id" in s}
                return str(p["id"]), stages
        raise HubSpotError(f"no deal pipeline named {label!r}: create it in the HubSpot UI "
                           "(docs/PLAN.md Step 67)")


def _check(obj: str) -> None:
    if obj not in OBJECTS:
        raise ValueError(f"not a HubSpot object this engine writes: {obj!r}")


def _id(record_id: str) -> str:
    if not str(record_id).isdigit():
        raise HubSpotError("a HubSpot record id is digits only")
    return str(record_id)

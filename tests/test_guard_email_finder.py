"""engine/guard/email_finder.py: Hunter and Apollo through guard (Step 42).

Invented people and fake keys only; every request goes to an httpx.MockTransport.
"""

from __future__ import annotations

import json

import httpx
import pytest

from engine.guard import email_finder as F
from engine.guard.budget import Budget, BudgetExceeded

FAKE_KEY = "test-key-not-real"
NO_PACE = {"clock": lambda: 0.0, "sleep": lambda s: None}


class Recorder:
    def __init__(self, status: int = 200, body: object = None):
        self.status, self.body = status, body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(F.HUNTER_ENV, raising=False)
    monkeypatch.delenv(F.APOLLO_ENV, raising=False)
    monkeypatch.setattr(F, "_last_call", {})


def _install(monkeypatch: pytest.MonkeyPatch, rec: Recorder) -> None:
    monkeypatch.setattr(F, "transport", httpx.MockTransport(rec))


@pytest.mark.parametrize("call", [
    lambda: F.hunter_find("velmora.example", "Ilsa", "Marrow", **NO_PACE),
    lambda: F.apollo_find("velmora.example", "Ilsa", "Marrow", **NO_PACE),
    lambda: F.hunter_verify("ilsa@velmora.example", **NO_PACE),
])
def test_not_configured_sends_nothing(monkeypatch, call) -> None:
    rec = Recorder(body={})
    _install(monkeypatch, rec)
    budget_before = Budget()
    with pytest.raises(F.NotConfigured):
        call()
    assert rec.requests == [] and budget_before.fetches == 0
    assert not F.hunter_configured() and not F.apollo_configured()


@pytest.mark.parametrize("score,level", [(97, "high"), (90, "high"), (75, "medium"),
                                         (40, "low"), (None, "low")])
def test_hunter_find(monkeypatch, score, level) -> None:
    monkeypatch.setenv(F.HUNTER_ENV, FAKE_KEY)
    rec = Recorder(body={"data": {"email": "ilsa@velmora.example", "score": score,
                                  "verification": {"status": "valid"}}})
    _install(monkeypatch, rec)
    budget = Budget()
    got = F.hunter_find("velmora.example", "Ilsa", "Marrow", budget=budget, **NO_PACE)
    assert got == F.FinderResult("ilsa@velmora.example", level, "hunter")
    [req] = rec.requests
    assert req.method == "GET" and req.url.host == "api.hunter.io"
    assert req.url.path == "/v2/email-finder"
    assert req.headers["X-API-KEY"] == FAKE_KEY and FAKE_KEY not in str(req.url)
    assert req.url.params["domain"] == "velmora.example"
    assert budget.fetches == 1


def test_hunter_find_nothing(monkeypatch) -> None:
    monkeypatch.setenv(F.HUNTER_ENV, FAKE_KEY)
    for rec in (Recorder(body={"data": {"email": None}}), Recorder(status=404, body={}),
                Recorder(body={"data": {"email": "ilsa@velmora.example", "score": 99,
                                        "verification": {"status": "invalid"}}})):
        _install(monkeypatch, rec)
        assert F.hunter_find("velmora.example", "Ilsa", "Marrow", **NO_PACE) is None


def test_errors_carry_no_body_or_person(monkeypatch) -> None:
    monkeypatch.setenv(F.HUNTER_ENV, FAKE_KEY)
    _install(monkeypatch, Recorder(status=500, body={"error": "ilsa@velmora.example Marrow"}))
    with pytest.raises(F.FinderError) as exc:
        F.hunter_find("velmora.example", "Ilsa", "Marrow", **NO_PACE)
    assert str(exc.value) == "hunter: HTTP 500"


def test_budget_is_charged_before_sending(monkeypatch) -> None:
    monkeypatch.setenv(F.HUNTER_ENV, FAKE_KEY)
    rec = Recorder(body={"data": {"status": "valid"}})
    _install(monkeypatch, rec)
    budget = Budget(max_fetches=1)
    F.hunter_verify("ilsa@velmora.example", budget=budget, **NO_PACE)
    with pytest.raises(BudgetExceeded):
        F.hunter_verify("ilsa@velmora.example", budget=budget, **NO_PACE)
    assert len(rec.requests) == 1


@pytest.mark.parametrize("status,level", [("verified", "medium"), ("guessed", "low"),
                                          (None, "low")])
def test_apollo_find(monkeypatch, status, level) -> None:
    monkeypatch.setenv(F.APOLLO_ENV, FAKE_KEY)
    rec = Recorder(body={"person": {"email": "ilsa@velmora.example", "email_status": status}})
    _install(monkeypatch, rec)
    got = F.apollo_find("velmora.example", "Ilsa", "Marrow", **NO_PACE)
    assert got == F.FinderResult("ilsa@velmora.example", level, "apollo")
    [req] = rec.requests
    assert req.method == "POST" and req.url.host == "api.apollo.io"
    assert req.headers["X-Api-Key"] == FAKE_KEY and FAKE_KEY not in str(req.url)
    assert json.loads(req.content)["reveal_personal_emails"] is False


def test_apollo_locked_or_missing(monkeypatch) -> None:
    monkeypatch.setenv(F.APOLLO_ENV, FAKE_KEY)
    for body in ({"person": {"email": "email_not_unlocked@domain.com"}}, {"person": None}, {}):
        _install(monkeypatch, Recorder(body=body))
        assert F.apollo_find("velmora.example", "Ilsa", "Marrow", **NO_PACE) is None


@pytest.mark.parametrize("status,level", [("valid", "medium"), ("accept_all", "low"),
                                          ("unknown", "low"), ("webmail", "low"),
                                          ("invalid", None), ("disposable", None)])
def test_hunter_verify(monkeypatch, status, level) -> None:
    monkeypatch.setenv(F.HUNTER_ENV, FAKE_KEY)
    rec = Recorder(body={"data": {"status": status}})
    _install(monkeypatch, rec)
    got = F.hunter_verify("ilsa@velmora.example", **NO_PACE)
    assert got == F.VerifyResult(status, level)
    assert rec.requests[0].url.path == "/v2/email-verifier"


def test_pacing_between_calls(monkeypatch) -> None:
    monkeypatch.setenv(F.HUNTER_ENV, FAKE_KEY)
    _install(monkeypatch, Recorder(body={"data": {"status": "valid"}}))
    slept: list[float] = []
    F.hunter_verify("ilsa@velmora.example", clock=lambda: 10.0, sleep=slept.append)
    F.hunter_verify("ilsa@velmora.example", clock=lambda: 10.25, sleep=slept.append)
    assert slept == [pytest.approx(0.75)]

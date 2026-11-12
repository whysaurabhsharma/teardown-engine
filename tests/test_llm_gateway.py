"""Model gateway: cache, schema retry, fallback, contacts guard, ledger, budget.

No real model or network calls: providers are fakes registered in
gateway.PROVIDERS. Every company and person here is invented.
"""

import sqlite3
from typing import Any

import pytest

from engine import db
from engine.guard.budget import Budget, BudgetExceeded
from engine.llm import gateway
from engine.llm.base import (
    CachedOutputInvalid,
    ContactInPrompt,
    ProviderError,
    ProviderResult,
    QuotaError,
)
from engine.llm.routes import ROUTES, Route, Target, family
from engine.models import Contact
from tests.conftest import add_company

SCHEMA = {
    "type": "object",
    "properties": {"gap": {"type": "string", "enum": ["G1", "G2"]}, "confidence": {"type": "number"}},
    "required": ["gap", "confidence"],
    "additionalProperties": False,
}
GOOD = {"gap": "G1", "confidence": 0.7}
BAD = {"gap": "G9", "confidence": 0.7}
ROUTE = Route("test", Target("fake_a", "model-a"), Target("fake_b", "model-b"))
PROMPT = "Zentrico's pricing page lists three tiers and a contact-sales button."


class Fake:
    """A scripted provider: each call pops the next reply (a result or an exception)."""

    def __init__(self, *replies: Any, model: str = "fake-model"):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.model = model

    def __call__(self, prompt: str, schema: dict, model: str | None) -> ProviderResult:
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, ProviderResult):
            return reply
        return ProviderResult(output=reply, model=self.model, input_tokens=120, output_tokens=30, cost_usd=0.012)

    @property
    def calls(self) -> int:
        return len(self.prompts)


@pytest.fixture
def company(conn: sqlite3.Connection) -> str:
    return add_company(conn)


def _install(monkeypatch: pytest.MonkeyPatch, a: Fake, b: Fake | None = None) -> None:
    monkeypatch.setitem(gateway.PROVIDERS, "fake_a", a)
    monkeypatch.setitem(gateway.PROVIDERS, "fake_b", b or Fake())


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM llm_calls ORDER BY call_id").fetchall()


def _call(conn: sqlite3.Connection, company: str, prompt: str = PROMPT, budget: Budget | None = None, **kw: Any) -> Any:
    return gateway.call("judge", company, "judge-v1", kw.pop("route", ROUTE), prompt, SCHEMA,
                        conn=conn, budget=budget or Budget(), **kw)


# --- happy path, ledger, cache ---------------------------------------------------

def test_valid_output_is_returned_and_recorded_with_tokens_and_cost(
    conn: sqlite3.Connection, company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = Fake(GOOD)
    _install(monkeypatch, a)
    budget = Budget()
    assert _call(conn, company, budget=budget) == GOOD
    assert a.calls == 1 and a.prompts[0] == PROMPT
    assert budget.model_calls == 1
    (row,) = _rows(conn)
    assert (row["stage"], row["company_id"], row["prompt_version"]) == ("judge", company, "judge-v1")
    assert (row["provider"], row["model"]) == ("fake_a", "fake-model")
    assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (120, 30, 0.012)
    assert row["cache_hit"] == 0 and row["output"] is not None


def test_cache_hit_makes_zero_provider_calls_and_writes_nothing(
    conn: sqlite3.Connection, company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, Fake(GOOD))
    _call(conn, company)
    a2, b2 = Fake(), Fake()  # any call would pop from an empty list and fail
    _install(monkeypatch, a2, b2)
    budget = Budget()
    assert _call(conn, company, budget=budget) == GOOD
    assert a2.calls == 0 and b2.calls == 0 and budget.model_calls == 0
    assert len(_rows(conn)) == 1


def test_cache_key_is_stage_company_and_prompt_version(
    conn: sqlite3.Connection, company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = add_company(conn, "quillvane-co", name="Quillvane")
    a = Fake(GOOD, GOOD, GOOD)
    _install(monkeypatch, a)
    _call(conn, company)
    gateway.call("judge", other, "judge-v1", ROUTE, PROMPT, SCHEMA, conn=conn, budget=Budget())
    gateway.call("judge", company, "judge-v2", ROUTE, PROMPT, SCHEMA, conn=conn, budget=Budget())
    assert a.calls == 3


def test_stored_output_that_no_longer_fits_the_schema_is_an_error(
    conn: sqlite3.Connection, company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, Fake(GOOD))
    _call(conn, company)
    a2 = Fake()
    _install(monkeypatch, a2)
    stricter = dict(SCHEMA, required=["gap", "confidence", "need"])
    with pytest.raises(CachedOutputInvalid):
        gateway.call("judge", company, "judge-v1", ROUTE, PROMPT, stricter, conn=conn, budget=Budget())
    assert a2.calls == 0


def test_gateway_opens_the_default_database_when_no_conn_given(
    db_file, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(db.DB_ENV_VAR, str(db_file))
    c = db.connect(db_file)
    add_company(c)
    c.close()
    _install(monkeypatch, Fake(GOOD))
    out = gateway.call("judge", "zentrico-io", "judge-v1", ROUTE, PROMPT, SCHEMA, budget=Budget())
    assert out == GOOD
    c = db.connect(db_file)
    assert c.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1
    c.close()


# --- schema retry ----------------------------------------------------------------

def test_invalid_output_retries_once_with_the_error_appended(
    conn: sqlite3.Connection, company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = Fake(BAD, GOOD)
    _install(monkeypatch, a)
    budget = Budget()
    assert _call(conn, company, budget=budget) == GOOD
    assert a.calls == 2 and budget.model_calls == 2
    retry = a.prompts[1]
    assert retry.startswith(PROMPT)
    assert "rejected" in retry and "at gap:" in retry and "'G9'" in retry
    first, second = _rows(conn)
    assert first["output"] is None and second["output"] is not None
    assert first["input_tokens"] == 120  # tokens are recorded for the failed attempt too


def test_invalid_twice_fails_cleanly_without_falling_back(
    conn: sqlite3.Connection, company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = Fake(BAD, {"gap": "G1"}), Fake()
    _install(monkeypatch, a, b)
    with pytest.raises(gateway.SchemaInvalid, match="twice"):
        _call(conn, company)
    assert a.calls == 2 and b.calls == 0
    rows = _rows(conn)
    assert len(rows) == 2 and all(r["output"] is None for r in rows)
    # nothing valid was stored, so the next run is not a cache hit
    _install(monkeypatch, Fake(GOOD))
    assert _call(conn, company) == GOOD


def test_missing_structured_output_counts_as_no_valid_result(
    conn: sqlite3.Connection, company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = ProviderResult(output=None, model="m", problem="the response had no structured_output")
    a = Fake(empty, empty)
    _install(monkeypatch, a)
    with pytest.raises(gateway.SchemaInvalid, match="structured_output"):
        _call(conn, company)
    assert a.calls == 2
    assert "structured_output" in a.prompts[1]


# --- fallback --------------------------------------------------------------------

def test_quota_error_falls_back_to_the_second_provider(
    conn: sqlite3.Connection, company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = Fake(QuotaError("usage limit reached")), Fake(GOOD, model="model-b")
    _install(monkeypatch, a, b)
    budget = Budget()
    assert _call(conn, company, budget=budget) == GOOD
    assert a.calls == 1 and b.calls == 1 and b.prompts[0] == PROMPT
    (row,) = _rows(conn)
    assert row["provider"] == "fake_b"
    assert budget.model_calls == 2  # the refused call was charged before it was sent


def test_quota_on_the_retry_still_falls_back(
    conn: sqlite3.Connection, company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = Fake(BAD, QuotaError("rate limit")), Fake(GOOD)
    _install(monkeypatch, a, b)
    assert _call(conn, company) == GOOD
    assert b.prompts == [PROMPT]


def test_unconfigured_provider_falls_back(
    conn: sqlite3.Connection, company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    cf = Fake({"family": "sales"})
    monkeypatch.setitem(gateway.PROVIDERS, "cloudflare", cf)
    schema = {"type": "object", "required": ["family"]}
    out = gateway.call("title_classifier", company, "titles-v1", "title_classifier",
                       "Title: Founding Account Executive", schema, conn=conn, budget=Budget())
    assert out == {"family": "sales"} and cf.calls == 1
    assert _rows(conn)[0]["provider"] == "cloudflare"


def test_every_provider_unavailable_fails_clearly_and_records_nothing(
    conn: sqlite3.Connection, company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, Fake(QuotaError("quota")), Fake(QuotaError("quota")))
    with pytest.raises(gateway.AllProvidersUnavailable, match="fake_a.*fake_b"):
        _call(conn, company)
    assert _rows(conn) == []


def test_other_provider_errors_do_not_fall_back(
    conn: sqlite3.Connection, company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = Fake(ProviderError("timed out")), Fake(GOOD)
    _install(monkeypatch, a, b)
    with pytest.raises(ProviderError):
        _call(conn, company)
    assert b.calls == 0


def test_exclude_family_skips_the_family_that_wrote_the_claim(
    conn: sqlite3.Connection, company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    gem, groq = Fake(), Fake(GOOD)
    monkeypatch.setitem(gateway.PROVIDERS, "gemini", gem)
    monkeypatch.setitem(gateway.PROVIDERS, "groq", groq)
    out = gateway.call("verify", company, "verify-v1", "verifier", PROMPT, SCHEMA,
                       conn=conn, budget=Budget(), exclude_family="google")
    assert out == GOOD and gem.calls == 0 and groq.calls == 1


# --- budget ----------------------------------------------------------------------

def test_budget_ceiling_stops_before_the_provider_is_called(
    conn: sqlite3.Connection, company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = Fake(BAD, GOOD)
    _install(monkeypatch, a)
    with pytest.raises(BudgetExceeded):
        _call(conn, company, budget=Budget(max_model_calls=1))
    assert a.calls == 1


# --- contacts guard --------------------------------------------------------------

@pytest.fixture
def contact(conn: sqlite3.Connection, company: str) -> None:
    with conn:
        db.insert(conn, Contact(
            company_id=company, name="Ilsa Marrowfield", title="Head of Revenue Operations",
            email="ilsa@zentrico.example", source_url="https://zentrico.example/team/ilsa-m",
            channel="email",
        ))
        db.insert(conn, Contact(company_id=company, name="Bo", title="VP"))  # too short to match


@pytest.mark.parametrize("prompt", [
    "Write to Ilsa Marrowfield about the pricing page.",  # Gate 1.3
    "write to ILSA MARROWFIELD",
    "Dear Ilsa,",
    "Marrowfield's team",
    "Ilsa\nMarrowfield",
    "Send it to Ilsa@Zentrico.example today.",
    "She is Head of Revenue Operations at Zentrico.",
    "Profile: https://zentrico.example/team/ilsa-m",
])
def test_prompt_with_a_contact_value_is_refused_and_nothing_happens(
    conn: sqlite3.Connection, company: str, contact: None, monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    a, b = Fake(GOOD), Fake(GOOD)
    _install(monkeypatch, a, b)
    budget = Budget()
    with pytest.raises(ContactInPrompt) as exc:
        _call(conn, company, prompt=prompt, budget=budget)
    assert a.calls == 0 and b.calls == 0 and budget.model_calls == 0
    assert _rows(conn) == []
    assert "ilsa" not in str(exc.value).lower() and "marrowfield" not in str(exc.value).lower()


def test_contact_guard_runs_before_the_cache(
    conn: sqlite3.Connection, company: str, contact: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, Fake(GOOD))
    _call(conn, company)
    with pytest.raises(ContactInPrompt):
        _call(conn, company, prompt=PROMPT + " Ask Ilsa Marrowfield.")


def test_contact_value_in_the_schema_is_refused(
    conn: sqlite3.Connection, company: str, contact: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, Fake(GOOD))
    schema = dict(SCHEMA, description="addressed to ilsa@zentrico.example")
    with pytest.raises(ContactInPrompt):
        gateway.call("judge", company, "judge-v1", ROUTE, PROMPT, schema, conn=conn, budget=Budget())


@pytest.mark.parametrize("prompt", [
    PROMPT,
    "The VP of Sales role at Zentrico is open.",  # 'VP' is under 3 characters
    "Bonus: Ilsabel and Marrowfields are other words.",  # whole words only
    "Reply by email.",  # channel is an enum, not personal data
])
def test_prompt_without_contact_values_passes(
    conn: sqlite3.Connection, company: str, contact: None, monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    a = Fake(GOOD)
    _install(monkeypatch, a)
    assert _call(conn, company, prompt=prompt) == GOOD
    assert a.calls == 1


def test_retry_prompt_is_checked_for_contacts_too(
    conn: sqlite3.Connection, company: str, contact: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A validation error that echoes a contact value must not be sent back.
    a = Fake({"gap": "Ilsa Marrowfield", "confidence": 1})
    _install(monkeypatch, a)
    with pytest.raises(ContactInPrompt):
        _call(conn, company)
    assert a.calls == 1


# --- routes ----------------------------------------------------------------------

def test_routes_follow_the_plan() -> None:
    assert ROUTES["title_classifier"].primary.provider == "groq"
    assert ROUTES["judge"].primary.provider == "claude"
    assert ROUTES["drafter"].primary.provider == "claude"
    judge_family = family(ROUTES["judge"].primary)
    assert judge_family == "anthropic"
    for t in ROUTES["verifier"].targets:
        assert family(t) != judge_family
    # when the judge falls back to Gemini, the verifier still has a non-Gemini option
    assert any(family(t) != family(ROUTES["judge"].fallback) for t in ROUTES["verifier"].targets)
    for r in ROUTES.values():
        assert r.fallback is not None
        for t in r.targets:
            assert t.provider in gateway.PROVIDERS


def test_unknown_route_is_an_error(conn: sqlite3.Connection, company: str) -> None:
    with pytest.raises(KeyError, match="unknown route"):
        gateway.call("judge", company, "v1", "nope", PROMPT, SCHEMA, conn=conn, budget=Budget())


def test_invalid_schema_is_an_error(conn: sqlite3.Connection, company: str) -> None:
    with pytest.raises(gateway.LlmError, match="schema itself"):
        gateway.call("judge", company, "v1", ROUTE, PROMPT, {"type": 7}, conn=conn, budget=Budget())

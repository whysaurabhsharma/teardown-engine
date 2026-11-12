"""Title classifier (docs/PLAN.md Step 24): rules, offer type, cached model fallback, CLI.

No real model or network calls: the title_classifier route's providers are
replaced with fakes. Every title and company here is invented.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from engine import db
from engine.cli import main
from engine.llm import gateway
from engine.llm.base import ProviderResult, QuotaError
from engine.models import Contact, Signal
from engine.signal import classify
from tests.conftest import add_company

# --- rules ---------------------------------------------------------------------------

RULE_TABLE = [
    # sdr
    ("SDR", "sdr"),
    ("Senior Sales Development Representative", "sdr"),
    ("BDR - Outbound (US Market)", "sdr"),
    ("Business Development Executive", "sdr"),
    ("Business Development Associate - Bengaluru", "sdr"),
    ("Associate, Business Development", "sdr"),
    ("BDE", "sdr"),
    ("Inside Sales Executive", "sdr"),
    ("Inside Sales Representative (Night Shift)", "sdr"),
    ("Lead Generation Executive", "sdr"),
    ("Telecaller - Hindi", "sdr"),
    ("Tele-sales Associate", "sdr"),
    ("Market Development Representative", "sdr"),
    # ae
    ("Account Executive", "ae"),
    ("Founding AE", "ae"),
    ("Sr. AE, Mid-Market", "ae"),
    ("Enterprise Account Executive - Founding Team", "ae"),
    ("Business Development Manager", "ae"),
    ("Senior Business Development Manager - SaaS", "ae"),
    ("Key Account Manager", "ae"),
    ("Key Accounts Manager", "ae"),
    ("Account Manager", "ae"),
    ("Area Sales Manager", "ae"),
    ("Sales Executive", "ae"),
    ("Enterprise Sales - North India", "ae"),
    ("Channel Sales Representative", "ae"),
    ("Account Director, Strategic", "ae"),
    # sales_lead
    ("Head of Sales", "sales_lead"),
    ("VP Sales", "sales_lead"),
    ("VP, Sales (India)", "sales_lead"),
    ("Vice President of Sales", "sales_lead"),
    ("CRO", "sales_lead"),
    ("Chief Revenue Officer", "sales_lead"),
    ("Sales Director - APAC", "sales_lead"),
    ("Director of Business Development", "sales_lead"),
    ("Head - Inside Sales", "sales_lead"),
    ("Sales Manager", "sales_lead"),
    ("Inside Sales Manager", "sales_lead"),
    ("Regional Sales Manager", "sales_lead"),
    ("SDR Team Lead", "sales_lead"),
    ("Sales Development Manager", "sales_lead"),
    ("Head of GTM", "sales_lead"),
    ("Founding GTM Hire", "sales_lead"),
    ("First Sales Hire", "sales_lead"),
    # revops
    ("RevOps Manager", "revops"),
    ("Revenue Operations Analyst", "revops"),
    ("Sales Ops Specialist", "revops"),
    ("Sales Operations Manager", "revops"),
    ("Sales & Marketing Ops Lead", "revops"),
    ("Marketing Ops Manager", "revops"),
    ("Marketing Operations Specialist", "revops"),
    ("GTM Operations Lead", "revops"),
    ("Salesforce Administrator", "revops"),
    ("HubSpot Admin", "revops"),
    ("Sales Enablement Manager", "revops"),
    ("Deal Desk Analyst", "revops"),
    ("Head of Revenue Operations", "revops"),
    # gtm_engineer
    ("GTM Engineer", "gtm_engineer"),
    ("Founding GTM Engineer", "gtm_engineer"),
    ("Go-To-Market Engineer", "gtm_engineer"),
    ("GTM Engineering Lead", "gtm_engineer"),
    ("GTM Automation Specialist", "gtm_engineer"),
    ("Revenue Engineer", "gtm_engineer"),
    ("Marketing Automation Engineer", "gtm_engineer"),
    ("Software Engineer, GTM", "gtm_engineer"),
    # growth
    ("Growth Engineer", "growth"),
    ("Software Engineer - Growth", "growth"),
    ("Growth Marketing Manager", "growth"),
    ("Head of Growth", "growth"),
    ("Growth Hacker", "growth"),
    ("Founding Growth Marketer", "growth"),
    ("Founder's Office", "growth"),
    ("Founders Office - Associate", "growth"),
    ("Chief of Staff", "growth"),
    ("Chief of Staff to the CEO", "growth"),
    # demand_gen
    ("Demand Generation Manager", "demand_gen"),
    ("Demand Gen Lead", "demand_gen"),
    ("Performance Marketing Manager", "demand_gen"),
    ("Performance Marketer", "demand_gen"),
    ("Paid Acquisition Specialist", "demand_gen"),
    ("PPC Executive", "demand_gen"),
    ("Field Marketing Manager", "demand_gen"),
    ("ABM Manager", "demand_gen"),
    ("Digital Marketing Executive", "demand_gen"),
    ("Head of Marketing", "demand_gen"),
    ("VP of Marketing", "demand_gen"),
    ("SEO & Performance Marketing Lead", "demand_gen"),
    # not_gtm
    ("Software Engineer", "not_gtm"),
    ("Senior Backend Developer (Python)", "not_gtm"),
    ("SDE II", "not_gtm"),
    ("Founding Engineer", "not_gtm"),
    ("DevOps Engineer", "not_gtm"),
    ("Data Scientist", "not_gtm"),
    ("QA Tester", "not_gtm"),
    ("Product Designer", "not_gtm"),
    ("Product Designer, Growth", "not_gtm"),
    ("UX Researcher", "not_gtm"),
    ("HR Manager", "not_gtm"),
    ("HR Business Partner", "not_gtm"),
    ("Talent Acquisition Specialist", "not_gtm"),
    ("Sales Recruiter", "not_gtm"),
    ("People Operations Manager", "not_gtm"),
    ("Accounts Executive", "not_gtm"),
    ("Accounts Manager", "not_gtm"),
    ("Accountant", "not_gtm"),
    ("Revenue Accountant", "not_gtm"),
    ("Financial Analyst", "not_gtm"),
    ("Legal Counsel", "not_gtm"),
    ("Sales Engineer", "not_gtm"),
    ("Solutions Engineer", "not_gtm"),
    ("Solutions Consultant", "not_gtm"),
    ("Pre-Sales Consultant", "not_gtm"),
    ("Presales Manager", "not_gtm"),
    ("Head of Sales Engineering", "not_gtm"),
    ("Customer Success Manager", "not_gtm"),
    ("Customer Support Executive", "not_gtm"),
    ("Technical Account Manager", "not_gtm"),
    ("Implementation Specialist", "not_gtm"),
    ("Product Manager", "not_gtm"),
    ("Product Marketing Manager", "not_gtm"),
    ("Content Writer", "not_gtm"),
    ("SEO Specialist", "not_gtm"),
    ("Social Media Manager", "not_gtm"),
    ("Brand Manager", "not_gtm"),
    ("Executive Assistant to the CEO", "not_gtm"),
    ("Chief of Staff, Engineering", "not_gtm"),
    ("Data Analyst", "not_gtm"),
]


@pytest.mark.parametrize(("title", "family"), RULE_TABLE)
def test_rule_table(title: str, family: str) -> None:
    assert classify.rule_family(title) == family


@pytest.mark.parametrize("title", [
    "Marketing Manager", "Business Development", "Partnerships Manager", "Sales",
    "Business Growth Manager", "Operations Associate", "",
])
def test_rules_leave_unclear_titles_to_the_model(title: str) -> None:
    assert classify.rule_family(title) is None


def test_every_family_is_reachable_by_the_rules() -> None:
    assert {f for _, f in RULE_TABLE} == set(classify.FAMILIES)


# --- offer type ----------------------------------------------------------------------

@pytest.mark.parametrize(("family", "title", "offer"), [
    ("gtm_engineer", "GTM Engineer", "candidate"),
    ("revops", "RevOps Manager", "candidate"),
    ("growth", "Growth Marketing Manager", "candidate"),
    ("growth", "Chief of Staff", "candidate"),
    ("sdr", "SDR", "adjacent"),
    ("sdr", "First SDR (India)", "candidate"),
    ("ae", "Account Executive", "adjacent"),
    ("ae", "Founding AE", "candidate"),
    ("ae", "Founding-Account Executive", "candidate"),
    ("sales_lead", "Head of Sales", "adjacent"),
    ("sales_lead", "Founding GTM Hire", "candidate"),
    ("sales_lead", "First Sales Hire", "candidate"),
    ("demand_gen", "Demand Generation Manager", "adjacent"),
    ("demand_gen", "Founding Marketer, Demand Gen", "candidate"),
    ("not_gtm", "Founding Engineer", None),
    ("not_gtm", "Software Engineer", None),
    (None, "Whatever", None),
])
def test_offer_type(family: str | None, title: str, offer: str | None) -> None:
    assert classify.offer_type(family, title) == offer


def test_offer_type_does_not_read_first_inside_a_word() -> None:
    assert classify.offer_type("ae", "Account Executive, Firstline Accounts") == "adjacent"


def test_unknown_family_is_an_error() -> None:
    with pytest.raises(ValueError):
        classify.offer_type("ceo", "CEO")


# --- model fallback ------------------------------------------------------------------

class Fake:
    """A scripted provider; each call pops the next reply (an output dict or an exception)."""

    def __init__(self, *replies: Any):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str, schema: dict, model: str | None) -> ProviderResult:
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ProviderResult(output=reply, model="fake", input_tokens=50, output_tokens=5)

    @property
    def calls(self) -> int:
        return len(self.prompts)


def _install(monkeypatch: pytest.MonkeyPatch, groq: Fake, cloudflare: Fake | None = None) -> None:
    """Replace the title_classifier route's providers; any other provider is unreachable."""
    for name in list(gateway.PROVIDERS):
        monkeypatch.setitem(gateway.PROVIDERS, name, Fake())
    monkeypatch.setitem(gateway.PROVIDERS, "groq", groq)
    monkeypatch.setitem(gateway.PROVIDERS, "cloudflare", cloudflare or Fake())


def fam(f: str) -> dict:
    return {"role_family": f}


def test_model_places_a_title_the_rules_cannot(conn, monkeypatch) -> None:
    groq = Fake(fam("demand_gen"))
    _install(monkeypatch, groq)
    assert classify.model_family("Marketing Manager", conn=conn) == "demand_gen"
    assert groq.calls == 1
    prompt = groq.prompts[0]
    # the fixed text plus the title: nothing about a company or a person
    assert prompt == classify.build_prompt("Marketing Manager")
    assert prompt.endswith("Job title: Marketing Manager")


def test_prompt_is_fixed_text_plus_title() -> None:
    a, b = classify.build_prompt("Alpha Role"), classify.build_prompt("Beta Role")
    assert a.replace("Alpha Role", "X") == b.replace("Beta Role", "X")


def test_cache_key_is_per_title(conn, monkeypatch) -> None:
    groq = Fake(fam("demand_gen"), fam("ae"))
    _install(monkeypatch, groq)
    classify.model_family("Marketing Manager", conn=conn)
    classify.model_family("Partnerships Manager", conn=conn)
    classify.model_family("Marketing Manager", conn=conn)
    classify.model_family("  marketing   MANAGER ", conn=conn)  # same title, spacing and case aside
    assert groq.calls == 2
    keys = conn.execute(
        "SELECT stage, company_id, prompt_version FROM llm_calls ORDER BY call_id").fetchall()
    assert len(keys) == 2
    assert {k["stage"] for k in keys} == {"classify_title"}
    assert {k["company_id"] for k in keys} == {None}
    v1, v2 = (k["prompt_version"] for k in keys)
    assert v1 != v2 and v1.startswith("c-1.0:") and len(v1) == len("c-1.0:") + 16
    assert classify.prompt_version_for("Marketing Manager") == v1


def test_schema_retry_and_fallback_come_from_the_gateway(conn, monkeypatch) -> None:
    groq = Fake(QuotaError("rate limited"))
    cf = Fake({"role_family": "ceo"}, fam("sdr"))
    _install(monkeypatch, groq, cf)
    assert classify.model_family("Business Development", conn=conn) == "sdr"
    assert groq.calls == 1 and cf.calls == 2


def test_contact_value_in_a_title_is_refused_and_left_null(conn, monkeypatch) -> None:
    cid = add_company(conn)
    with conn:
        db.insert(conn, Contact(company_id=cid, name="Ravindra Kolte", title="Partner Lead"))
    _sig(conn, "s1", "Partner Lead")
    groq = Fake()
    _install(monkeypatch, groq)
    rep = classify.run(conn)
    assert groq.calls == 0
    assert rep.unresolved == ["Partner Lead"] and rep.errors == ["Partner Lead: ContactInPrompt"]
    assert _families(conn) == {"s1": (None, None)}


# --- run over the signals table ------------------------------------------------------

def _sig(conn: sqlite3.Connection, sid: str, title: str, **kw: Any) -> None:
    with conn:
        db.insert(conn, Signal(signal_id=sid, source="manual", company_name_raw="Zentrico",
                               title=title, first_seen=date(2026, 11, 1), **kw))


def _families(conn: sqlite3.Connection) -> dict[str, tuple[str | None, str | None]]:
    return {r["signal_id"]: (r["role_family"], r["offer_type"])
            for r in conn.execute("SELECT * FROM signals ORDER BY signal_id")}


def _seed(conn: sqlite3.Connection) -> None:
    _sig(conn, "s1", "Founding AE")
    _sig(conn, "s2", "GTM Engineer")
    _sig(conn, "s3", "Software Engineer")
    _sig(conn, "s4", "Marketing Manager")
    _sig(conn, "s5", "Marketing Manager")  # same title on another board: one call
    _sig(conn, "s6", "Partnerships Manager")
    _sig(conn, "s7", "SDR", role_family="sdr", offer_type="adjacent")  # already classified


def test_run_classifies_with_rules_then_model_and_rerun_changes_nothing(conn, monkeypatch) -> None:
    _seed(conn)
    groq = Fake(fam("demand_gen"), fam("not_gtm"))
    _install(monkeypatch, groq)
    rep = classify.run(conn)
    assert (rep.rows, rep.by_rules, rep.by_model, rep.updated) == (6, 3, 3, 6)
    assert groq.calls == 2
    assert _families(conn) == {
        "s1": ("ae", "candidate"),
        "s2": ("gtm_engineer", "candidate"),
        "s3": ("not_gtm", None),
        "s4": ("demand_gen", "adjacent"),
        "s5": ("demand_gen", "adjacent"),
        "s6": ("not_gtm", None),
        "s7": ("sdr", "adjacent"),
    }
    before = _families(conn)
    calls = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]

    groq2 = Fake()  # any call would fail
    _install(monkeypatch, groq2)
    again = classify.run(conn)
    assert (again.rows, again.updated) == (0, 0)
    assert groq2.calls == 0
    assert _families(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == calls


def test_classified_rows_are_never_touched(conn, monkeypatch) -> None:
    _sig(conn, "s1", "Head of Sales", role_family="growth", offer_type="candidate")  # set by hand
    _install(monkeypatch, Fake())
    classify.run(conn)
    assert _families(conn) == {"s1": ("growth", "candidate")}


def test_no_model_leaves_unresolved_titles_null(conn, monkeypatch) -> None:
    _seed(conn)
    groq = Fake()
    _install(monkeypatch, groq)
    rep = classify.run(conn, use_model=False)
    assert groq.calls == 0
    assert sorted(rep.unresolved) == ["Marketing Manager", "Partnerships Manager"]
    fams = _families(conn)
    assert fams["s4"] == fams["s5"] == fams["s6"] == (None, None)
    assert fams["s1"] == ("ae", "candidate")


def test_dry_run_writes_nothing_and_calls_nothing(conn, monkeypatch) -> None:
    _seed(conn)
    groq = Fake()
    _install(monkeypatch, groq)
    rep = classify.run(conn, dry_run=True)
    assert (rep.rows, rep.by_rules, rep.by_model, rep.model_titles, rep.model_cached) == (6, 3, 3, 2, 0)
    assert groq.calls == 0
    assert all(v == (None, None) for k, v in _families(conn).items() if k != "s7")


def test_limit_and_provider_failure(conn, monkeypatch) -> None:
    _seed(conn)
    groq = Fake(QuotaError("rate limited"))
    cf = Fake(QuotaError("rate limited"))
    _install(monkeypatch, groq, cf)
    rep = classify.run(conn, limit=4)  # s1..s4
    assert rep.rows == 4 and rep.updated == 3
    assert rep.unresolved == ["Marketing Manager"]
    assert rep.errors == ["Marketing Manager: AllProvidersUnavailable"]
    assert _families(conn)["s4"] == (None, None)


# --- CLI -----------------------------------------------------------------------------

def _cli(db_file: Path, *args: str):
    return CliRunner().invoke(main, ["signal", "classify", "--db", str(db_file), *args])


def test_cli_dry_run(db_file: Path, conn, monkeypatch) -> None:
    _seed(conn)
    groq = Fake()
    _install(monkeypatch, groq)
    r = _cli(db_file, "--dry-run")
    assert r.exit_code == 0, r.output
    assert "unclassified signals 6: rules 3, model 3" in r.output
    assert "model titles 2 (0 cached, 2 would be calls)" in r.output
    assert groq.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM signals WHERE role_family IS NULL").fetchone()[0] == 6


def test_cli_run_prints_counts_by_family_and_is_idempotent(db_file: Path, conn, monkeypatch) -> None:
    _seed(conn)
    _install(monkeypatch, Fake(fam("demand_gen"), fam("not_gtm")))
    r = _cli(db_file)
    assert r.exit_code == 0, r.output
    assert "unclassified signals 6: rules 3, model 3, left null 0" in r.output
    assert "  demand_gen   2" in r.output and "  not_gtm      2" in r.output
    assert "offer types: candidate 2, adjacent 2, none 2" in r.output
    _install(monkeypatch, Fake())
    r2 = _cli(db_file)
    assert r2.exit_code == 0, r2.output
    assert "unclassified signals 0: rules 0, model 0, left null 0" in r2.output


def test_cli_no_model_reports_unresolved(db_file: Path, conn, monkeypatch) -> None:
    _seed(conn)
    groq = Fake()
    _install(monkeypatch, groq)
    r = _cli(db_file, "--no-model")
    assert r.exit_code == 0, r.output
    assert "left null 3" in r.output
    assert "unresolved titles 2:" in r.output and "  Partnerships Manager" in r.output
    assert groq.calls == 0


def test_cli_budget_stop(db_file: Path, conn, monkeypatch) -> None:
    _seed(conn)
    _install(monkeypatch, Fake(fam("demand_gen"), fam("not_gtm")))
    r = _cli(db_file, "--max-model-calls", "1")
    assert r.exit_code == 1
    assert "stopped: model call ceiling reached" in r.output
    assert "left null 1" in r.output

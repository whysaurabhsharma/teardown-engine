"""The People step (Steps 40-43, Gate 3.3). Invented companies and INVENTED people only.

No network: every test runs with sockets blocked. No real keys.
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine import cli as cli_mod
from engine import db
from engine.capture import extract_text as X
from engine.capture.show import show_lines
from engine.cli import main
from engine.debug import export_company
from engine.guard.budget import Budget
from engine.guard.cache import Cache
from engine.llm import gateway
from engine.llm.base import ContactInPrompt
from engine.llm.routes import Route, Target
from engine.people import contacts as P
from engine.people import pattern as PT
from engine.people.rule import is_recruiter_title, recipient_rule, title_fits
from tests.conftest import add_company

ROOT = Path(__file__).resolve().parents[1]
CID = "velmora-example"
WHEN = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)

# Invented people. None of these exist.
NAME = "Ilsa Marrow"
EMAIL = "ilsa@velmora.example"
LINKEDIN = "https://www.linkedin.com/in/ilsa-marrow-invented"
SOURCE = "https://velmora.example/about"

ABOUT_TEXT = "About Velmora\nWe build ledgers for dairy co-operatives.\nOur team\nIlsa Marrow, CEO"
JOB_TEXT = ("Sales Development Representative\nBengaluru, India\n"
            "You will report directly to the Head of Sales and work with two account "
            "executives.\nWhat you will do\nBook meetings.")
HOME_TEXT = ("Velmora ledgers\nWrite to hello@velmora.example or sales@velmora.example.\n"
             "Questions about billing: orrin.vale@velmora.example\n"
             "Our partner: someone@otherco.example")


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_net(*a, **kw):
        raise AssertionError("the People step touched the network")

    monkeypatch.setattr(socket.socket, "connect", no_net)
    monkeypatch.setattr(socket, "create_connection", no_net)


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "cache")


def _art(conn: sqlite3.Connection, cache: Cache, kind: str, url: str, text: str | None,
         outcome: str = "ok", structure: dict | None = None, cid: str = CID) -> None:
    tsha = cache.put_derived("text", text.encode("utf-8")) if text is not None else None
    ssha = (cache.put_derived("structure", json.dumps(structure).encode("utf-8"))
            if structure is not None else None)
    with conn:
        conn.execute(
            "INSERT INTO artifacts (company_id, kind, url, outcome, fetched_at, text_sha256, "
            "structure_sha256, extractor_version, fetcher, bundle_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'http', 'bundle-1.0')",
            (cid, kind, url, outcome, WHEN.isoformat(), tsha, ssha,
             X.EXTRACTOR_VERSION if tsha else None))


@pytest.fixture
def company(conn: sqlite3.Connection, cache: Cache) -> str:
    add_company(conn, CID, name="Velmora", domain="velmora.example", status="extracted",
                pilot=True, headcount=40)
    _art(conn, cache, "homepage", "https://velmora.example/", HOME_TEXT,
         structure={"format": "html", "links": [
             {"text": "Email us", "href": "mailto:press@velmora.example?subject=hi"},
             {"text": "Pricing", "href": "https://velmora.example/pricing"}]})
    _art(conn, cache, "about", SOURCE, ABOUT_TEXT)
    _art(conn, cache, "job_post", "https://jobs.velmora.example/sdr", JOB_TEXT)
    return CID


def run(db_file: Path, *args: str, cache: Cache | None = None):
    extra = ["--db", str(db_file)]
    if cache is not None:
        extra += ["--cache-dir", str(cache.root)]
    return CliRunner().invoke(main, ["people", *args, *extra])


def add(db_file: Path, *extra: str, cid: str = CID, name: str = NAME, title: str = "Founder & CEO"):
    return run(db_file, "add", cid, "--name", name, "--title", title, "--source-url", SOURCE,
               *extra)


def contacts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM contacts ORDER BY contact_id").fetchall()


def assert_no_person(text: str) -> None:
    low = text.lower()
    for v in ("ilsa", "marrow", EMAIL, LINKEDIN.lower()):
        assert v.lower() not in low, f"personal value leaked: {v}"


# --- Step 40: the rule ---------------------------------------------------------------


@pytest.mark.parametrize("n,band", [(1, "under_50"), (49, "under_50"), (50, "50_to_200"),
                                    (200, "50_to_200"), (201, "over_200"), (900, "over_200")])
def test_recipient_rule_bands(n: int, band: str) -> None:
    r = recipient_rule(n)
    assert r.band == band and r.headcount == n and r.known and r.ask is None
    assert "recruit" not in r.target.lower()


def test_recipient_rule_targets_follow_the_plan() -> None:
    assert recipient_rule(10).target == "the founder or CEO"
    assert "head of sales, VP Sales, CRO or head of growth" in recipient_rule(120).target
    assert "hiring manager named in the job post" in recipient_rule(400).target
    assert "director-level sales or RevOps lead" in recipient_rule(400).target


def test_recipient_rule_unknown_headcount_asks() -> None:
    r = recipient_rule(None)
    assert r.band == "unknown" and not r.known
    assert "unknown" in r.target and r.ask and "te resolve review" in r.ask


@pytest.mark.parametrize("bad", [0, -3, True, 12.5])
def test_recipient_rule_rejects_bad_headcount(bad) -> None:
    with pytest.raises(ValueError):
        recipient_rule(bad)


@pytest.mark.parametrize("title", [
    "Technical Recruiter", "Talent Acquisition Lead", "Senior Recruiting Partner",
    "HR Business Partner", "Head of People", "People Operations Manager",
    "Human Resources Manager", "Director of Talent", "VP, People & Culture", "HRBP",
])
def test_recruiter_titles_are_recognised(title: str) -> None:
    assert is_recruiter_title(title)


@pytest.mark.parametrize("title", [
    "Founder & CEO", "Co-founder", "Head of Sales", "VP Sales", "Chief Revenue Officer",
    "Director, Revenue Operations", "Head of Growth", "Sales Director, India",
])
def test_seller_titles_are_not_recruiters(title: str) -> None:
    assert not is_recruiter_title(title)


def test_title_fits() -> None:
    assert title_fits(recipient_rule(20), "Co-founder and CEO") is True
    assert title_fits(recipient_rule(20), "Head of Sales") is False
    assert title_fits(recipient_rule(120), "VP Sales") is True
    assert title_fits(recipient_rule(120), "CRO") is True
    assert title_fits(recipient_rule(120), "Head of Growth") is True
    assert title_fits(recipient_rule(120), "Founder") is False
    assert title_fits(recipient_rule(400), "Director, Revenue Operations") is True
    assert title_fits(recipient_rule(400), "Engineering Manager") is None  # could be the hiring manager
    assert title_fits(recipient_rule(None), "CEO") is None


# --- Step 41: suggest ----------------------------------------------------------------


def test_suggest_prints_rule_and_where_to_look(db_file: Path, conn, cache, company) -> None:
    r = run(db_file, "suggest", CID, cache=cache)
    assert r.exit_code == 0, r.output
    out = r.output
    assert "headcount 40" in out
    assert "recipient rule (recipient-rule-1.0): the founder or CEO" in out
    assert "never the recipient" in out
    assert "recorded: none" in out
    assert out.index("1. the team or about page") < out.index(SOURCE) < out.index("2. a \"reports to\"")
    assert ("| You will report directly to the Head of Sales and work with two account "
            "executives.") in out
    assert "3. press and funding announcements: by hand" in out
    assert "4. your own LinkedIn search: by hand" in out
    # Names on the page are never pulled out by the tool.
    assert "Ilsa" not in out
    assert contacts(conn) == []


def test_suggest_unknown_headcount_and_nothing_captured(db_file: Path, conn, cache) -> None:
    add_company(conn, "quorrel-example", name="Quorrel", domain="quorrel.example",
                status="qualified")
    r = run(db_file, "suggest", "quorrel-example", cache=cache)
    assert r.exit_code == 0, r.output
    assert "headcount unknown" in r.output and "ASK:" in r.output
    assert "no about page captured" in r.output
    assert "look from the homepage: https://quorrel.example/" in r.output
    assert "no job post text captured or extracted" in r.output


def test_suggest_falls_back_to_signal_description(db_file: Path, conn, cache) -> None:
    add_company(conn, "quorrel-example", name="Quorrel", domain="quorrel.example",
                status="qualified", headcount=120)
    with conn:
        conn.execute("INSERT INTO signals (signal_id, source, company_name_raw, title, url, "
                     "first_seen, company_id, description_text) VALUES ('m-1', 'manual', "
                     "'Quorrel', 'Account Executive', 'https://quorrel.example/jobs/ae', "
                     "'2026-11-01', 'quorrel-example', ?)",
                     ("Join us.\nThis role reports to our VP Sales.\nApply now.",))
        conn.execute("UPDATE companies SET best_signal_id = 'm-1' WHERE company_id = "
                     "'quorrel-example'")
    r = run(db_file, "suggest", "quorrel-example", cache=cache)
    assert r.exit_code == 0, r.output
    assert "head of sales, VP Sales" in r.output
    assert "from signal description https://quorrel.example/jobs/ae" in r.output
    assert "| This role reports to our VP Sales." in r.output


def test_suggest_unknown_company(db_file: Path) -> None:
    assert run(db_file, "suggest", "nobody-example").exit_code == 1


# --- Steps 41-43: add ----------------------------------------------------------------


def test_add_email_recipient(db_file: Path, conn, company) -> None:
    r = add(db_file, "--email", EMAIL, "--confidence", "high", "--email-source", "hunter")
    assert r.exit_code == 0, r.output
    assert "added" in r.output and "channel email" in r.output and "confidence high" in r.output
    assert_no_person(r.output)
    [row] = contacts(conn)
    assert (row["name"], row["title"], row["source_url"], row["email"]) == (
        NAME, "Founder & CEO", SOURCE, EMAIL)
    assert (row["confidence"], row["email_confidence"], row["email_source"], row["channel"],
            row["linkedin_url"]) == ("high", 0.9, "hunter", "email", None)


def test_add_is_idempotent_and_replace_is_explicit(db_file: Path, conn, company) -> None:
    args = ("--email", EMAIL, "--confidence", "medium", "--email-source", "apollo")
    assert add(db_file, *args).exit_code == 0
    r = add(db_file, *args)
    assert r.exit_code == 0 and "unchanged" in r.output
    r = add(db_file, "--email", EMAIL, "--confidence", "high", "--email-source", "hunter")
    assert r.exit_code == 1 and "--replace" in r.output
    assert contacts(conn)[0]["confidence"] == "medium"
    r = add(db_file, "--email", EMAIL, "--confidence", "high", "--email-source", "hunter",
            "--replace")
    assert r.exit_code == 0 and "replaced" in r.output
    assert len(contacts(conn)) == 1 and contacts(conn)[0]["confidence"] == "high"


def test_add_dry_run_writes_nothing(db_file: Path, conn, company) -> None:
    r = add(db_file, "--linkedin-url", LINKEDIN, "--dry-run")
    assert r.exit_code == 0 and "would add" in r.output and "channel linkedin" in r.output
    assert contacts(conn) == []


@pytest.mark.parametrize("title", ["Talent Acquisition Partner", "Senior Recruiter",
                                   "HR Manager", "Head of People"])
def test_add_refuses_recruiters(db_file: Path, conn, company, title: str) -> None:
    r = add(db_file, "--linkedin-url", LINKEDIN, title=title)
    assert r.exit_code == 1 and "never the recipient" in r.output
    assert contacts(conn) == []


def test_add_requires_source_url(db_file: Path, conn, company) -> None:
    r = run(db_file, "add", CID, "--name", NAME, "--title", "CEO", "--linkedin-url", LINKEDIN)
    assert r.exit_code == 2  # click: missing --source-url
    r = run(db_file, "add", CID, "--name", NAME, "--title", "CEO", "--source-url", " ",
            "--linkedin-url", LINKEDIN)
    assert r.exit_code == 1 and "--source-url" in r.output
    r = run(db_file, "add", CID, "--name", NAME, "--title", "CEO", "--source-url",
            "velmora.example/about", "--linkedin-url", LINKEDIN)
    assert r.exit_code == 1 and "http(s)" in r.output
    assert contacts(conn) == []


@pytest.mark.parametrize("bad", ["ilsa", "ilsa@", "ilsa@velmora", "il sa@velmora.example",
                                 "ilsa..m@velmora.example", ".ilsa@velmora.example",
                                 "ilsa@-velmora.example"])
def test_add_rejects_bad_address_syntax(db_file: Path, conn, company, bad: str) -> None:
    r = add(db_file, "--email", bad, "--confidence", "high", "--email-source", "manual")
    assert r.exit_code == 1 and "not a valid address" in r.output
    assert bad not in r.output
    assert contacts(conn) == []


def test_add_other_domain_needs_force(db_file: Path, conn, company) -> None:
    other = "ilsa@elsewhere.example"
    r = add(db_file, "--email", other, "--confidence", "high", "--email-source", "manual")
    assert r.exit_code == 1 and "--force" in r.output and "elsewhere.example" in r.output
    assert other not in r.output and contacts(conn) == []
    r = add(db_file, "--email", other, "--confidence", "high", "--email-source", "manual",
            "--force")
    assert r.exit_code == 0, r.output
    assert "warning:" in r.output and other not in r.output
    assert contacts(conn)[0]["email"] == other


def test_add_subdomain_address_counts_as_the_company(db_file: Path, conn, company) -> None:
    r = add(db_file, "--email", "ilsa@mail.velmora.example", "--confidence", "medium",
            "--email-source", "public")
    assert r.exit_code == 0, r.output


def test_channel_rules(db_file: Path, conn, company) -> None:
    # low confidence and no LinkedIn: no channel
    r = add(db_file, "--email", EMAIL, "--confidence", "low", "--email-source", "pattern")
    assert r.exit_code == 1 and "no channel" in r.output and "te people drop" in r.output
    # nothing at all: no channel
    r = add(db_file)
    assert r.exit_code == 1 and "no channel" in r.output
    # low confidence plus LinkedIn: linkedin, address kept at low
    r = add(db_file, "--email", EMAIL, "--confidence", "low", "--email-source", "pattern",
            "--linkedin-url", LINKEDIN)
    assert r.exit_code == 0, r.output
    [row] = contacts(conn)
    assert (row["channel"], row["confidence"], row["email_confidence"]) == ("linkedin", "low", 0.3)
    assert row["linkedin_url"] == LINKEDIN


def test_channel_for() -> None:
    assert P.channel_for("high", None) == "email"
    assert P.channel_for("medium", LINKEDIN) == "email"
    assert P.channel_for("low", LINKEDIN) == "linkedin"
    assert P.channel_for(None, LINKEDIN) == "linkedin"
    assert P.channel_for("low", None) is None
    assert P.channel_for(None, None) is None


def test_pattern_address_is_low_unless_verified(db_file: Path, conn, company) -> None:
    r = add(db_file, "--email", EMAIL, "--confidence", "medium", "--email-source", "pattern")
    assert r.exit_code == 1 and "verifier" in r.output
    r = add(db_file, "--email", EMAIL, "--confidence", "medium", "--email-source", "pattern",
            "--verified")
    assert r.exit_code == 0, r.output


def test_add_option_combinations(db_file: Path, conn, company) -> None:
    r = add(db_file, "--confidence", "high", "--linkedin-url", LINKEDIN)
    assert r.exit_code == 1 and "give --email" in r.output
    r = add(db_file, "--email", EMAIL, "--email-source", "manual")
    assert r.exit_code == 1 and "--confidence" in r.output
    r = add(db_file, "--email", EMAIL, "--confidence", "high")
    assert r.exit_code == 1 and "--email-source" in r.output
    r = add(db_file, "--linkedin-url", "https://velmora.example/ilsa")
    assert r.exit_code == 1 and "linkedin.com" in r.output
    assert contacts(conn) == []


def test_add_warns_on_title_off_rule_and_unknown_headcount(db_file: Path, conn, company) -> None:
    r = add(db_file, "--linkedin-url", LINKEDIN, title="Head of Sales")
    assert r.exit_code == 0 and "does not look like the rule's target" in r.output
    add_company(conn, "quorrel-example", name="Quorrel", domain="quorrel.example",
                status="qualified")
    r = add(db_file, "--linkedin-url", LINKEDIN, cid="quorrel-example")
    assert r.exit_code == 0 and "headcount unknown" in r.output


def test_add_refuses_dropped_and_unknown(db_file: Path, conn, company) -> None:
    add_company(conn, "quorrel-example", name="Quorrel", domain="quorrel.example",
                status="dropped", drop_reason="consumer", drop_stage="qualify")
    assert add(db_file, "--linkedin-url", LINKEDIN, cid="quorrel-example").exit_code == 1
    assert add(db_file, "--linkedin-url", LINKEDIN, cid="nobody-example").exit_code == 1
    assert contacts(conn) == []


# --- drop ----------------------------------------------------------------------------


def test_drop_no_recipient(db_file: Path, conn, company) -> None:
    r = run(db_file, "drop", CID, "--dry-run")
    assert r.exit_code == 0 and "would drop" in r.output
    assert conn.execute("SELECT status FROM companies").fetchone()[0] == "extracted"
    r = run(db_file, "drop", CID)
    assert r.exit_code == 0 and "dropped velmora-example no_recipient" in r.output
    row = conn.execute("SELECT status, drop_reason, drop_stage FROM companies").fetchone()
    assert tuple(row) == ("dropped", "no_recipient", "people")
    r = run(db_file, "drop", CID)
    assert r.exit_code == 0 and "already dropped" in r.output


def test_drop_refuses_a_company_dropped_for_another_reason(db_file: Path, conn) -> None:
    add_company(conn, "quorrel-example", name="Quorrel", domain="quorrel.example",
                status="dropped", drop_reason="consumer", drop_stage="qualify")
    assert run(db_file, "drop", "quorrel-example").exit_code == 1


# --- list ----------------------------------------------------------------------------


def test_list_hides_names_and_addresses(db_file: Path, conn, company) -> None:
    assert add(db_file, "--email", EMAIL, "--confidence", "high", "--email-source",
               "hunter").exit_code == 0
    r = run(db_file, "list", "--pilot")
    assert r.exit_code == 0, r.output
    assert CID in r.output and "under_50" in r.output and "Founder & CEO" in r.output
    assert "email" in r.output and "high" in r.output and "hunter" in r.output
    assert_no_person(r.output)


def test_list_show_contacts_only_to_a_terminal(db_file: Path, conn, company,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    assert add(db_file, "--email", EMAIL, "--confidence", "high", "--email-source",
               "hunter").exit_code == 0
    r = run(db_file, "list", "--show-contacts")  # CliRunner's stdout is not a terminal
    assert r.exit_code == 1 and "only to a terminal" in r.output
    assert_no_person(r.output)
    monkeypatch.setattr(cli_mod, "_stdout_is_terminal", lambda: True)
    r = run(db_file, "list", "--show-contacts")
    assert r.exit_code == 0 and NAME in r.output and EMAIL in r.output


# --- Gate 3.3 ------------------------------------------------------------------------


def _pilot_twelve(conn: sqlite3.Connection) -> list[str]:
    ids = []
    for i in range(12):
        cid = f"pilotco{i:02d}-example"
        add_company(conn, cid, name=f"Pilotco {i}", domain=f"pilotco{i:02d}.example",
                    status="extracted", pilot=True, headcount=30)
        ids.append(cid)
    return ids


def _record(conn: sqlite3.Connection, cid: str, domain: str, **kw) -> None:
    args = dict(name="Tamsin Orrel", title="Founder", source_url=f"https://{domain}/team")
    args.update(kw)
    P.write(conn, P.plan_add(conn, cid, **args))


def test_check_gate_passes_with_twelve_reachable(db_file: Path, conn) -> None:
    ids = _pilot_twelve(conn)
    for i, cid in enumerate(ids):
        dom = f"pilotco{i:02d}.example"
        if i % 2:
            _record(conn, cid, dom, email=f"tamsin@{dom}", confidence="medium",
                    email_source="hunter")
        else:
            _record(conn, cid, dom, linkedin_url=LINKEDIN)
    r = run(db_file, "check", "--pilot")
    assert r.exit_code == 0, r.output
    assert "companies 12: ok 12, fail 0" in r.output
    assert_no_person(r.output)
    assert "tamsin" not in r.output.lower()


def test_check_gate_fails(db_file: Path, conn) -> None:
    ids = _pilot_twelve(conn)
    for i, cid in enumerate(ids[:10]):
        _record(conn, cid, f"pilotco{i:02d}.example", linkedin_url=LINKEDIN)
    # one with no source URL left (written around the CLI's checks)
    with conn:
        conn.execute("UPDATE contacts SET source_url = NULL WHERE company_id = ?", (ids[0],))
    P.drop(conn, ids[11])
    r = run(db_file, "check", "--pilot")
    assert r.exit_code == 1
    assert f"FAIL {ids[0]:<30} recipient has no source URL" in r.output
    assert f"FAIL {ids[10]:<30} no recipient recorded" in r.output
    assert f"FAIL {ids[11]:<30} dropped (no_recipient)" in r.output
    assert "ok 9, fail 3" in r.output


def test_check_gate_fails_short_pilot(db_file: Path, conn, company) -> None:
    assert add(db_file, "--linkedin-url", LINKEDIN).exit_code == 0
    r = run(db_file, "check", "--pilot")
    assert r.exit_code == 1 and "the pilot has 1 companies, not 12" in r.output
    r = run(db_file, "check", "--company", CID)
    assert r.exit_code == 0, r.output


def test_gate_problem_low_email_without_linkedin() -> None:
    c = P.CompanyRow("x-example", "X", "x.example", "extracted", 1, 0, 30, None, None)
    o = P.Overview(c, "CEO", "email", "low", "pattern", True, True, True, False, None, None)
    assert P.gate_problem(o) == ("no address at medium confidence or better, and no "
                                 "LinkedIn channel")


# --- Step 42: pattern ----------------------------------------------------------------


def test_shape() -> None:
    assert PT.shape("orrin") == "{first}"
    assert PT.shape("orrin.vale") == "{first}.{last}"
    assert PT.shape("o.vale") == "{f}.{last}"
    assert PT.shape("orrin_v") == "{first}_{l}"
    assert PT.shape("orrin-vale") == "{first}-{last}"
    assert PT.shape("orrin2") is None and PT.shape("a.b.c") is None


def test_render() -> None:
    assert PT.render("{first}.{last}", "Ilsa", "Marrow", "velmora.example") == \
        "ilsa.marrow@velmora.example"
    assert PT.render("{f}{last}", "Élan", "Dû", "x.example") == "edu@x.example"
    with pytest.raises(ValueError):
        PT.render("{first}.{last}", "Ilsa", "", "velmora.example")


def test_public_addresses_and_proposal(conn, cache, company) -> None:
    found, missing = PT.public_addresses(conn, cache, CID)
    assert missing == 0
    by = {a.address: a for a in found}
    assert set(by) == {"hello@velmora.example", "sales@velmora.example",
                       "orrin.vale@velmora.example", "press@velmora.example"}
    assert by["hello@velmora.example"].role and by["press@velmora.example"].role
    assert by["orrin.vale@velmora.example"].shape == "{first}.{last}"
    prop = PT.propose(found)
    assert prop.pattern == "{first}.{last}"
    assert PT.propose([a for a in found if a.role]).pattern is None


def test_proposal_disagreement() -> None:
    a = PT.PublicAddress("orrin@x.example", "homepage", "u", False, "{first}")
    b = PT.PublicAddress("t.vale@x.example", "homepage", "u", False, "{f}.{last}")
    assert PT.propose([a, b]).pattern is None


def test_cli_pattern(db_file: Path, conn, cache, company, monkeypatch) -> None:
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    r = run(db_file, "pattern", CID, cache=cache)
    assert r.exit_code == 0, r.output
    assert "orrin.vale@velmora.example" in r.output and "someone@otherco.example" not in r.output
    assert "pattern: {first}.{last}" in r.output
    assert "inferred address" not in r.output
    r = run(db_file, "pattern", CID, "--first", "Ilsa", "--last", "Marrow", "--verify",
            cache=cache)
    assert r.exit_code == 0, r.output
    assert "not configured" in r.output
    assert "inferred address: ilsa.marrow@velmora.example (confidence low)" in r.output
    assert "--confidence low --email-source pattern" in r.output
    assert contacts(conn) == []  # nothing stored


# --- contacts stay local -------------------------------------------------------------


def test_gateway_refuses_a_prompt_naming_an_added_contact(db_file: Path, conn, company,
                                                          monkeypatch) -> None:
    assert add(db_file, "--email", EMAIL, "--confidence", "high", "--email-source", "hunter",
               "--linkedin-url", LINKEDIN).exit_code == 0

    def never(*a, **kw):
        raise AssertionError("a provider was called")

    monkeypatch.setitem(gateway.PROVIDERS, "fake_a", never)
    route = Route("test", Target("fake_a", "model-a"))
    schema = {"type": "object"}
    budget = Budget()
    for prompt in (f"Velmora's page names {NAME}.", "Write to MARROW about pricing.",
                   f"Profile: {LINKEDIN}", f"Reach {EMAIL}."):
        with pytest.raises(ContactInPrompt) as exc:
            gateway.call("judge", CID, "judge-v1", route, prompt, schema, conn=conn,
                         budget=budget)
        assert_no_person(str(exc.value))
    assert budget.model_calls == 0
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0


def test_show_and_debug_export_redact_added_contacts(db_file: Path, conn, cache, company,
                                                     tmp_path: Path) -> None:
    assert add(db_file, "--linkedin-url", LINKEDIN).exit_code == 0
    out = "\n".join(show_lines(conn, CID, cache, lines=10))
    assert "Our team" in out and "[redacted], CEO" in out
    assert_no_person(out)
    dest = export_company(conn, CID, tmp_path / "debug", cache)
    for f in dest.rglob("*"):
        if f.is_file():
            assert_no_person(f.read_text(encoding="utf-8"))
    about_id = conn.execute("SELECT artifact_id FROM artifacts WHERE kind = 'about'").fetchone()[0]
    assert "[redacted], CEO" in (dest / "artifacts" / f"{about_id}.txt").read_text(encoding="utf-8")


def test_contact_values_cover_linkedin_url(conn, company) -> None:
    P.write(conn, P.plan_add(conn, CID, name=NAME, title="CEO", source_url=SOURCE,
                             linkedin_url=LINKEDIN))
    assert LINKEDIN in gateway.contact_values(conn)


# --- mail servers are never contacted -------------------------------------------------

_FORBIDDEN = re.compile(r"\bsmtplib\b|\bsocket\b|\bRCPT\b|\bVRFY\b|dns\.resolver|"
                        r"\bimport\s+dns\b|\baiosmtp|\bguard\.dns\b|\bdnspython\b")


def _sources() -> list[Path]:
    files = sorted((ROOT / "engine" / "people").glob("*.py"))
    files.append(ROOT / "engine" / "guard" / "email_finder.py")
    assert len(files) >= 5
    return files


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_mail_server_probing_in_source(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not _FORBIDDEN.search(text), f"{path.name} must not touch mail servers or sockets"


@pytest.mark.parametrize("path", sorted((ROOT / "engine" / "people").glob("*.py")),
                         ids=lambda p: p.name)
def test_people_does_not_touch_the_network(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"\bimport\s+httpx\b|\bfrom\s+httpx\b|\burllib\.request\b|"
                         r"engine\.guard\.(?:http|email_finder|search_api|browser)\b|"
                         r"from\s+engine\.guard\s+import\s+(?:http|email_finder)", text)

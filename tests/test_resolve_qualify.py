"""Step 29: apply the ICP rules (engine/resolve/qualify.py).

Companies are invented. Nothing here touches the network or a model.
"""

from __future__ import annotations

import socket
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine import db
from engine.cli import main
from engine.models import Signal
from engine.resolve import qualify
from tests.conftest import add_company


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a: object, **k: object) -> None:
        raise AssertionError("qualify must not open a socket")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def co(conn: sqlite3.Connection, cid: str, *, segment: str | None = "in_saas",
       name: str = "Zentrico", status: str = "resolved", **kw: object) -> str:
    hq = {"in_saas": "india", "us_india_team": "abroad"}.get(segment or "")
    return add_company(conn, cid, name=name, segment=segment, status=status, hq=hq, **kw)


_n = 0


def sig(conn: sqlite3.Connection, company_id: str, *, title: str = "Account Executive",
        family: str | None = "ae", offer: str | None = "adjacent", fresh: bool | None = True,
        india: bool | None = True, posted: date | None = date(2026, 11, 1),
        first_seen: date = date(2026, 11, 2), description: str | None = None,
        sid: str | None = None) -> str:
    global _n
    _n += 1
    sid = sid or f"greenhouse:{company_id}:{_n:03d}"
    with conn:
        db.insert(conn, Signal(signal_id=sid, source="greenhouse", board_slug=company_id,
                               company_name_raw="Zentrico", title=title, role_family=family,
                               offer_type=offer, location="Bengaluru", posted_at=posted,
                               first_seen=first_seen, company_id=company_id,
                               description_text=description, fresh=fresh,
                               india_located=india))
    return sid


def row(conn: sqlite3.Connection, cid: str) -> sqlite3.Row:
    return conn.execute("SELECT status, drop_reason, drop_stage, best_signal_id, review_reason "
                        "FROM companies WHERE company_id = ?", (cid,)).fetchone()


# -- the rules, in order -----------------------------------------------------------------


def test_rule_order_is_the_icp_table_order() -> None:
    assert qualify.RULE_ORDER == ("out_of_scope", "staffing_or_agency", "consumer",
                                  "open_gtm_role")


def test_out_of_scope_drops_us_company_without_india_role(conn: sqlite3.Connection) -> None:
    co(conn, "kelvori-io", segment="us_india_team")
    sig(conn, "kelvori-io", india=False)
    rep = qualify.run(conn)
    r = row(conn, "kelvori-io")
    assert (r["status"], r["drop_reason"], r["drop_stage"]) == ("dropped", "out_of_scope",
                                                                "resolve")
    assert rep.dropped == {"out_of_scope": 1}


def test_out_of_scope_holds_when_signals_unfiltered(conn: sqlite3.Connection) -> None:
    co(conn, "kelvori-io", segment="us_india_team")
    sig(conn, "kelvori-io", fresh=None, india=None)
    qualify.run(conn)
    r = row(conn, "kelvori-io")
    assert r["status"] == "resolved"
    assert r["review_reason"].startswith("qualify_hold: location_unknown:")


def test_in_saas_passes_location_rule_with_a_us_role(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-in")
    s = sig(conn, "zentrico-in", india=False)
    qualify.run(conn)
    r = row(conn, "zentrico-in")
    assert (r["status"], r["best_signal_id"]) == ("qualified", s)


def test_staffing_drop(conn: sqlite3.Connection) -> None:
    co(conn, "talvent-example", name="Talvent")
    sig(conn, "talvent-example",
        description="We are hiring on behalf of our client, a Series A SaaS company in Pune.")
    rep = qualify.run(conn)
    r = row(conn, "talvent-example")
    assert (r["status"], r["drop_reason"], r["drop_stage"]) == ("dropped", "staffing_or_agency",
                                                                "qualify")
    assert "on behalf of our client" in rep.verdicts[0].detail


@pytest.mark.parametrize("text", [
    "Hiring for one of our esteemed clients in fintech.",
    "Our client is a fast-growing B2B SaaS startup.",
    "This role is for a client of ours based in Mumbai.",
    "We are hiring for a leading SaaS client.",
])
def test_client_behalf_phrases(text: str) -> None:
    assert qualify._CLIENT_BEHALF_RX.search(text)


@pytest.mark.parametrize("text", [
    "You will own the relationship with our clients across India.",
    "We are hiring for our Client Success team.",
    "We are hiring for a client-facing role.",
    "Help agencies hire on behalf of their clients.",
])
def test_saas_phrases_are_not_client_behalf(text: str) -> None:
    assert not qualify._CLIENT_BEHALF_RX.search(text)


def test_staffing_word_in_name_only_holds(conn: sqlite3.Connection) -> None:
    co(conn, "brightpath-example", name="Brightpath Recruitment Labs")
    sig(conn, "brightpath-example")
    qualify.run(conn)
    r = row(conn, "brightpath-example")
    assert r["status"] == "resolved"
    assert r["review_reason"].startswith("qualify_hold: staffing_name:")


def test_consumer_markers_hold_never_drop(conn: sqlite3.Connection) -> None:
    co(conn, "glowly-example", name="Glowly")
    sig(conn, "glowly-example", description="Grow our D2C skincare brand across India.")
    rep = qualify.run(conn)
    r = row(conn, "glowly-example")
    assert r["status"] == "resolved"
    assert r["review_reason"].startswith("qualify_hold: consumer_markers:")
    assert rep.held == {"consumer_markers": 1} and not rep.dropped


def test_consumer_marker_with_b2b_marker_passes(conn: sqlite3.Connection) -> None:
    co(conn, "glowly-example", name="Glowly")
    sig(conn, "glowly-example", description="Our SaaS helps D2C brands run their stores.")
    qualify.run(conn)
    assert row(conn, "glowly-example")["status"] == "qualified"


def test_no_fresh_gtm_signal_holds(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-in")
    sig(conn, "zentrico-in", fresh=False)
    sig(conn, "zentrico-in", title="Product Designer", family="not_gtm", offer=None)
    qualify.run(conn)
    r = row(conn, "zentrico-in")
    assert r["status"] == "resolved"
    assert r["review_reason"].startswith("qualify_hold: no_fresh_gtm_signal:")


def test_first_failure_wins_out_of_scope_before_staffing(conn: sqlite3.Connection) -> None:
    co(conn, "kelvori-io", segment="us_india_team", name="Kelvori Staffing")
    sig(conn, "kelvori-io", india=False, description="Hiring on behalf of our client.")
    qualify.run(conn)
    assert row(conn, "kelvori-io")["drop_reason"] == "out_of_scope"


def test_first_failure_wins_staffing_before_consumer(conn: sqlite3.Connection) -> None:
    co(conn, "talvent-example", name="Talvent")
    sig(conn, "talvent-example", description="Our client is a D2C fashion label.")
    qualify.run(conn)
    assert row(conn, "talvent-example")["drop_reason"] == "staffing_or_agency"


def test_hold_stops_later_rules(conn: sqlite3.Connection) -> None:
    # An open out_of_scope question is answered by a person before anything later.
    co(conn, "kelvori-io", segment="us_india_team")
    sig(conn, "kelvori-io", fresh=None, india=None,
        description="Hiring on behalf of our client.")
    qualify.run(conn)
    r = row(conn, "kelvori-io")
    assert r["status"] == "resolved" and "location_unknown" in r["review_reason"]


# -- one company, one teardown ----------------------------------------------------------------


def test_candidate_preferred_over_adjacent(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-in")
    sig(conn, "zentrico-in", title="VP Sales", family="sales_lead", posted=date(2026, 11, 10))
    cand = sig(conn, "zentrico-in", title="RevOps Manager", family="revops", offer="candidate",
               posted=date(2026, 9, 20))
    rep = qualify.run(conn)
    assert row(conn, "zentrico-in")["best_signal_id"] == cand
    assert rep.verdicts[0].gtm_roles == 2


def test_adjacent_most_senior_then_family(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-in")
    sig(conn, "zentrico-in", title="SDR", family="sdr", posted=date(2026, 11, 10))
    sig(conn, "zentrico-in", title="Account Executive", family="ae")
    head = sig(conn, "zentrico-in", title="Head of Sales", family="sales_lead",
               posted=date(2026, 10, 1))
    qualify.run(conn)
    assert row(conn, "zentrico-in")["best_signal_id"] == head


def test_us_india_team_signal_must_be_india_located(conn: sqlite3.Connection) -> None:
    co(conn, "kelvori-io", segment="us_india_team")
    sig(conn, "kelvori-io", title="VP Sales", family="sales_lead", india=False)
    sdr = sig(conn, "kelvori-io", title="SDR", family="sdr", india=True)
    qualify.run(conn)
    assert row(conn, "kelvori-io")["best_signal_id"] == sdr


def test_candidates_most_recent_then_signal_id(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-in")
    sig(conn, "zentrico-in", title="Growth Lead", family="growth", offer="candidate",
        posted=date(2026, 10, 1), sid="b-old")
    sig(conn, "zentrico-in", title="GTM Engineer", family="gtm_engineer", offer="candidate",
        posted=None, first_seen=date(2026, 11, 5), sid="c-new")
    sig(conn, "zentrico-in", title="RevOps Analyst", family="revops", offer="candidate",
        posted=date(2026, 11, 5), sid="a-new")
    qualify.run(conn)
    assert row(conn, "zentrico-in")["best_signal_id"] == "a-new"


def test_stale_and_unclassified_signals_never_chosen(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-in")
    sig(conn, "zentrico-in", title="GTM Engineer", family="gtm_engineer", offer="candidate",
        fresh=False)
    sig(conn, "zentrico-in", title="Mystery Role", family=None, offer=None)
    ae = sig(conn, "zentrico-in")
    qualify.run(conn)
    assert row(conn, "zentrico-in")["best_signal_id"] == ae


@pytest.mark.parametrize("title,level", [
    ("Chief Revenue Officer", 4), ("Founding AE", 4), ("Sales Manager", 3),
    ("Lead Generation Executive", 1), ("Senior SDR", 2), ("Sales Intern", 0), ("BDR", 1),
])
def test_seniority(title: str, level: int) -> None:
    assert qualify.seniority(title) == level


# -- selection, idempotence, safety ------------------------------------------------------------


def test_other_review_reason_is_skipped(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-in", review_reason="name_mismatch: homepage title scored 60")
    sig(conn, "zentrico-in")
    rep = qualify.run(conn)
    r = row(conn, "zentrico-in")
    assert r["status"] == "resolved" and r["review_reason"].startswith("name_mismatch")
    assert rep.companies == 0 and rep.skipped == {"name_mismatch": 1}


def test_unsegmented_and_other_statuses_untouched(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-in", segment=None)
    co(conn, "kelvori-io", status="sourced")
    sig(conn, "zentrico-in")
    sig(conn, "kelvori-io")
    rep = qualify.run(conn)
    assert rep.companies == 0 and rep.unsegmented == 1
    assert row(conn, "zentrico-in")["status"] == "resolved"
    assert row(conn, "kelvori-io")["status"] == "sourced"


def test_own_hold_cleared_when_inputs_change(conn: sqlite3.Connection) -> None:
    co(conn, "kelvori-io", segment="us_india_team")
    s = sig(conn, "kelvori-io", fresh=None, india=None)
    qualify.run(conn)
    assert row(conn, "kelvori-io")["review_reason"] is not None
    with conn:
        conn.execute("UPDATE signals SET fresh = 1, india_located = 1 WHERE signal_id = ?", (s,))
    qualify.run(conn)
    r = row(conn, "kelvori-io")
    assert (r["status"], r["review_reason"], r["best_signal_id"]) == ("qualified", None, s)


def test_rerun_is_idempotent(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-in")
    sig(conn, "zentrico-in")
    co(conn, "kelvori-io", segment="us_india_team")
    sig(conn, "kelvori-io", india=False)
    co(conn, "glowly-example", name="Glowly")
    sig(conn, "glowly-example", description="A D2C brand.")
    first = qualify.run(conn)
    assert first.changed == 3
    before = conn.execute("SELECT * FROM companies ORDER BY company_id").fetchall()
    again = qualify.run(conn)
    assert again.changed == 0
    after = conn.execute("SELECT * FROM companies ORDER BY company_id").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_dry_run_writes_nothing(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-in")
    sig(conn, "zentrico-in")
    rep = qualify.run(conn, dry_run=True)
    assert rep.qualified == 1 and rep.changed == 0
    assert row(conn, "zentrico-in")["status"] == "resolved"
    assert row(conn, "zentrico-in")["best_signal_id"] is None


def test_company_and_limit(conn: sqlite3.Connection) -> None:
    for cid in ("a-co", "b-co", "c-co"):
        co(conn, cid)
        sig(conn, cid)
    assert qualify.run(conn, company_id="b-co").qualified == 1
    assert row(conn, "a-co")["status"] == "resolved"
    assert qualify.run(conn, limit=1).qualified == 1
    assert row(conn, "a-co")["status"] == "qualified"
    assert row(conn, "c-co")["status"] == "resolved"


def test_no_model_calls(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-in")
    sig(conn, "zentrico-in")
    qualify.run(conn)
    assert conn.execute("SELECT count(*) FROM llm_calls").fetchone()[0] == 0


def test_module_does_not_use_guard_llm_or_offer() -> None:
    src = Path(qualify.__file__).read_text(encoding="utf-8")
    # No network, no model, and no file reads: OFFER.md's list is for a person only.
    for banned in ("engine.guard", "engine.llm", "httpx", "Guard(", "open(", "read_text",
                   "pathlib"):
        assert banned not in src


# -- CLI ---------------------------------------------------------------------------------------


def test_cli_qualify(db_file: Path) -> None:
    conn = db.connect(db_file)
    co(conn, "zentrico-in")
    sig(conn, "zentrico-in")
    co(conn, "talvent-example", name="Talvent")
    sig(conn, "talvent-example", description="Hiring for one of our clients.")
    co(conn, "glowly-example", name="Glowly")
    sig(conn, "glowly-example", description="A D2C brand.")
    conn.close()
    runner = CliRunner()
    dry = runner.invoke(main, ["qualify", "--dry-run", "--db", str(db_file)])
    assert dry.exit_code == 0, dry.output
    assert "dry run, nothing written" in dry.output
    real = runner.invoke(main, ["qualify", "--db", str(db_file)])
    assert real.exit_code == 0, real.output
    assert "qualified 1, dropped 1, held for review 1" in real.output
    assert "dropped staffing_or_agency" in real.output
    assert "held    consumer_markers" in real.output
    assert "changed 3" in real.output
    again = runner.invoke(main, ["qualify", "--db", str(db_file)])
    assert "changed 0" in again.output

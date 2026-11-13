"""Steps 44-45, Gate 4.1: migration 0013, `te gold`, the split, the held-out guard.

Invented companies only: Quorvane and Brindlecart (the facts fixtures, reused
from tests/test_extract_facts.py) for quote checks, and a handful of invented
.example companies for the split. No network, no model call: providers are
fakes, and the real data/ is never read (conftest points the database and the
cache at tmp_path).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine import cli as cli_mod
from engine import db
from engine.cli import main
from engine.guard.budget import Budget
from engine.judge import gold as G
from engine.llm import gateway
from engine.llm.base import HeldOutInPrompt
from engine.models import Gold, GoldEvidence
from tests.conftest import add_company
from tests.test_extract_facts import BID, QID, bundles, cache, offline  # noqa: F401 (fixtures)
from tests.test_llm_gateway import GOOD, ROUTE, SCHEMA, Fake, _install

PRICING_Q = "Talk to our team for a custom quote"
JOB_Q = "Build and own target account lists of dental and diagnostics chains from scratch."
SPF_Q = '"v=spf1 include:_spf.google.com include:amazonses.com -all"'


def add(conn, cache, company_id=QID, *, gap=None, abstain=None, quotes=(), not_found=(),
        **kw) -> G.AddPlan:
    return G.plan_add(conn, cache, company_id, gap=gap, abstain_reason=abstain,
                      quotes=list(quotes), not_found=list(not_found), **kw)


# -- quotes are checked with extract.base.verify -----------------------------------------


@pytest.mark.parametrize("kind, quote", [
    ("pricing", PRICING_Q),
    ("pricing", "Talk  to our\n team for a custom quote"),  # whitespace differs
    ("pricing", "Enterprise Talk to our team"),  # across a line break in the text
    ("homepage", "Diagnostics labs"),  # nav text (removed_text)
    ("homepage", "/static/app.js"),  # a script src in the structure
    ("dns", SPF_Q),
])
def test_verbatim_quotes_pass(conn, cache, bundles, kind, quote) -> None:
    plan = add(conn, cache, abstain="thin: one page", quotes=[(kind, quote)])
    (e,) = plan.row.evidence
    assert e.found and e.kind == kind and e.quote == " ".join(quote.split())
    assert e.url.startswith("https://quorvane.example") or e.url == "dns:quorvane.example"


@pytest.mark.parametrize("kind, quote, why", [
    ("pricing", "Talk to our team for a free quote", "not found verbatim"),
    ("pricing", "talk to our team for a custom quote", "not found verbatim"),  # case
    ("pricing", PRICING_Q.replace("Talk", "Speak"), "not found verbatim"),
    ("job_post", PRICING_Q, "not found verbatim"),  # right text, wrong artifact
    ("about", "We started in Pune.", "outcome is blocked"),
    ("product", "anything", "outcome is not_found"),
    ("careers", "Join us.", "no careers artifact"),
    ("pricing", "   ", "empty"),
])
def test_bad_quotes_are_refused_with_the_rule(conn, cache, bundles, kind, quote, why) -> None:
    with pytest.raises(G.GoldError, match=why) as exc:
        add(conn, cache, abstain="thin: x", quotes=[(kind, quote)])
    assert "Evidence rules" in str(exc.value) or why == "empty"
    if quote.strip():
        assert quote not in str(exc.value)  # quotes are never echoed


def test_not_found_only_on_a_not_found_outcome(conn, cache, bundles) -> None:
    plan = add(conn, cache, abstain="no_evidence: nothing", not_found=["product"])
    (e,) = plan.row.evidence
    assert (e.kind, e.found, e.quote, e.url) == ("product", False, None,
                                                 "https://quorvane.example/product")
    for kind, outcome in (("about", "blocked"), ("pricing", "ok")):
        with pytest.raises(G.GoldError, match=f"outcome is {outcome}") as exc:
            add(conn, cache, abstain="thin: x", not_found=[kind])
        assert "never that it is absent" in str(exc.value)
    with pytest.raises(G.GoldError, match="robots_denied"):
        add(conn, cache, BID, abstain="thin: x", not_found=["job_post"])
    with pytest.raises(G.GoldError, match="no careers artifact row"):
        add(conn, cache, abstain="thin: x", not_found=["careers"])
    with pytest.raises(G.GoldError, match="also quoted"):
        add(conn, cache, BID, abstain="thin: x", quotes=[("pricing", "Contact sales")],
            not_found=["pricing"])


def test_gap_needs_two_kinds_and_one_found_quote(conn, cache, bundles) -> None:
    with pytest.raises(G.GoldError, match="at least two artifact kinds"):
        add(conn, cache, gap="G2", quotes=[("pricing", PRICING_Q)])
    with pytest.raises(G.GoldError, match="at least two artifact kinds"):  # one kind, twice
        add(conn, cache, gap="G2", quotes=[("pricing", PRICING_Q),
                                           ("pricing", "Simple pricing for every clinic")])
    with pytest.raises(G.GoldError, match="at least one must be a found quote"):
        add(conn, cache, BID, gap="G4", not_found=["customers", "compare"])
    plan = add(conn, cache, gap="G1", quotes=[("job_post", JOB_Q)], not_found=["product"])
    assert plan.row.gap_type == "G1" and not plan.row.abstain
    assert [e.found for e in plan.row.evidence] == [True, False]


@pytest.mark.parametrize("reason", ["", "maybe: unclear", "No_evidence: x", "thin:", "thin:  ",
                                    "contradicted"])
def test_abstain_needs_a_reason_code(conn, cache, bundles, reason) -> None:
    with pytest.raises(G.GoldError, match="abstain_reason|abstention"):
        add(conn, cache, abstain=reason)


@pytest.mark.parametrize("reason", ["no_evidence: nothing on the pages",
                                    "contradicted: G1 expected; the nav names Dental chains",
                                    "thin: fits, below 0.6"])
def test_abstain_with_a_code_and_no_evidence_is_accepted(conn, cache, bundles, reason) -> None:
    plan = add(conn, cache, abstain=reason)
    assert plan.row.abstain and plan.row.abstain_reason == reason and plan.row.evidence == []
    assert G.write(conn, plan) == "added"


def test_gap_and_abstain_are_exclusive(conn, cache, bundles) -> None:
    with pytest.raises(G.GoldError, match="either --gap or --abstain"):
        add(conn, cache, gap="G1", abstain="thin: x")
    with pytest.raises(G.GoldError, match="either --gap or --abstain"):
        add(conn, cache)


def test_g6_is_accepted_and_stored(conn, cache, bundles) -> None:
    plan = add(conn, cache, gap="G6", quotes=[("dns", SPF_Q), ("job_post", JOB_Q)])
    assert any("provisional" in w for w in plan.warnings)
    assert G.write(conn, plan) == "added"
    row = conn.execute("SELECT * FROM gold WHERE company_id = ?", (QID,)).fetchone()
    assert row["gap_type"] == "G6" and row["split"] == "held_out"
    assert row["gaps_version"] == G.default_gaps_version()
    ev = json.loads(row["evidence"])
    assert [set(e) for e in ev] == [{"kind", "found", "url", "quote"}] * 2


def test_add_is_idempotent_per_company_and_version(conn, cache, bundles) -> None:
    kw = dict(gap="G2", quotes=[("pricing", PRICING_Q), ("homepage", "Book a demo")])
    assert G.write(conn, add(conn, cache, **kw)) == "added"
    assert G.write(conn, add(conn, cache, **kw)) == "unchanged"
    other = add(conn, cache, abstain="thin: on reflection")
    with pytest.raises(G.GoldError, match="--replace"):
        G.write(conn, other)
    assert G.write(conn, other, replace=True) == "replaced"
    # another gaps version is another row, and keeps the company's split
    assert G.write(conn, add(conn, cache, gaps_version="v2.0", **kw)) == "added"
    assert conn.execute("SELECT COUNT(*) FROM gold").fetchone()[0] == 2
    with pytest.raises(G.GoldError, match="te gold split"):
        add(conn, cache, gaps_version="v2.1", split="teach", **kw)
    with pytest.raises(G.GoldError, match="look like v1.2"):
        add(conn, cache, gaps_version="1.2", **kw)


def test_no_company(conn, cache, bundles) -> None:
    with pytest.raises(G.GoldError, match="no company"):
        add(conn, cache, "nobody-example", abstain="thin: x")


def test_cli_gold_add(db_file, conn, cache, bundles, offline) -> None:
    base = ["gold", "add", QID, "--db", str(db_file), "--cache-dir", str(cache.root)]
    args = base + ["--gap", "G2", "--quote", "pricing", PRICING_Q,
                   "--quote", "homepage", "Book a demo", "--note", "pricing vs demo CTA"]
    r = CliRunner().invoke(main, args + ["--dry-run"])
    assert r.exit_code == 0, r.output
    assert "would add" in r.output and PRICING_Q not in r.output
    assert conn.execute("SELECT COUNT(*) FROM gold").fetchone()[0] == 0
    r = CliRunner().invoke(main, args)
    assert r.exit_code == 0, r.output
    assert "added: held_out" in r.output and "from homepage, pricing" in r.output
    r = CliRunner().invoke(main, args)
    assert r.exit_code == 0 and "unchanged" in r.output
    r = CliRunner().invoke(main, base + ["--gap", "G2", "--quote", "pricing", PRICING_Q])
    assert r.exit_code == 1 and "at least two artifact kinds" in r.output
    r = CliRunner().invoke(main, base + ["--abstain", "unsure"])
    assert r.exit_code == 1 and "no_evidence:" in r.output
    r = CliRunner().invoke(main, base + ["--gap", "G7"])
    assert r.exit_code == 2
    row = conn.execute("SELECT notes FROM gold").fetchone()
    assert row["notes"] == "pricing vs demo CTA"


# -- the database ------------------------------------------------------------------------


def test_database_accepts_g6_and_checks_abstain_reason(conn) -> None:
    add_company(conn)
    sql = ("INSERT INTO gold (company_id, gap_type, abstain, abstain_reason, evidence, split, "
           "gaps_version) VALUES ('zentrico-io', ?, ?, ?, '[]', 'teach', ?)")
    with conn:
        conn.execute(sql, ("G6", 0, None, "v1"))
        conn.execute(sql, (None, 1, "contradicted: G1; segments named", "v2"))
    for params in ((None, 1, None, "v3"), (None, 1, "because", "v3"), (None, 1, "thin: ", "v3"),
                   ("G1", 0, "thin: x", "v3"), ("G7", 0, None, "v3")):
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute(sql, params)
    with conn:
        conn.execute("INSERT INTO diagnoses (company_id, prompt_version, attempted, gap_type, "
                     "need, cannot_see) VALUES ('zentrico-io', 'j-1.0', 1, 'G6', 'n', 'c')")


@pytest.mark.parametrize("evidence", [
    '{"kind": "pricing"}',
    '[{"kind": "pricing", "found": true, "url": "https://z.example/p"}]',
    '[{"kind": "pricing", "found": true, "url": "https://z.example/p", "quote": " "}]',
    '[{"kind": "pricing", "found": false, "url": "https://z.example/p", "quote": "x"}]',
    '[{"kind": "pricing", "found": true, "url": "", "quote": "x"}]',
    '[{"found": true, "url": "https://z.example/p", "quote": "x"}]',
    '["a quote"]',
])
def test_database_rejects_malformed_evidence(conn, evidence) -> None:
    add_company(conn)
    with pytest.raises(sqlite3.IntegrityError, match="gold evidence"), conn:
        conn.execute("INSERT INTO gold (company_id, gap_type, abstain, evidence, split, "
                     "gaps_version) VALUES ('zentrico-io', 'G1', 0, ?, 'teach', 'v1')",
                     (evidence,))


def test_migration_0013_preserves_rows(tmp_path: Path) -> None:
    old = tmp_path / "migs12"
    old.mkdir()
    upto13 = tmp_path / "migs13"  # later migrations are tested on their own
    upto13.mkdir()
    for f in db.MIGRATIONS_DIR.glob("*.sql"):
        if int(f.name[:4]) <= 12:
            shutil.copy(f, old / f.name)
        if int(f.name[:4]) <= 13:
            shutil.copy(f, upto13 / f.name)
    path, _ = db.init_db(tmp_path / "data" / "engine.db", old)
    conn = db.connect(path)
    assert db.user_version(conn) == 12
    add_company(conn)
    claims = json.dumps([{"text": "t", "artifact_kind": "pricing", "found": True,
                          "url": "https://zentrico-io.example/pricing", "quote": "Contact us"}])
    with conn:
        conn.execute("INSERT INTO gold (gold_id, company_id, gap_type, abstain, evidence, split, "
                     "gaps_version) VALUES (7, 'zentrico-io', 'G3', 0, '[\"legacy\"]', 'teach', "
                     "'v1.1'), (9, 'zentrico-io', NULL, 1, '{}', 'held_out', 'v1.2')")
        conn.execute("DELETE FROM gold WHERE gold_id = 7")  # the high-water mark stays at 9
        conn.execute("INSERT INTO gold (gold_id, company_id, gap_type, abstain, evidence, split, "
                     "gaps_version) VALUES (7, 'zentrico-io', 'G3', 0, '[\"legacy\"]', 'teach', "
                     "'v1.1')")
        conn.execute("INSERT INTO diagnoses (diagnosis_id, company_id, prompt_version, attempted, "
                     "gap_type, confidence, claims, abstain_reason, need, cannot_see, "
                     "created_at) VALUES "
                     "(4, 'zentrico-io', 'j-1.0', 1, 'G2', 0.9, ?, NULL, 'n', 'c', "
                     "'2026-11-12T00:00:00Z'), (5, 'zentrico-io', 'j-1.1', 0, NULL, NULL, '[]', "
                     "'no_evidence: x', NULL, NULL, '2026-11-12T00:00:01Z')", (claims,))
    conn.close()

    applied, backup = db.migrate(path, upto13)
    assert [m.version for m in applied] == [13] and backup is not None

    conn = db.connect(path)
    gold = [dict(r) for r in conn.execute("SELECT * FROM gold ORDER BY gold_id")]
    assert gold == [
        {"gold_id": 7, "company_id": "zentrico-io", "gap_type": "G3", "abstain": 0,
         "abstain_reason": None, "evidence": '["legacy"]', "split": "teach",
         "gaps_version": "v1.1", "notes": None},
        {"gold_id": 9, "company_id": "zentrico-io", "gap_type": None, "abstain": 1,
         "abstain_reason": "no_evidence: not recorded (row predates migration 0013)",
         "evidence": "{}", "split": "held_out", "gaps_version": "v1.2", "notes": None},
    ]
    diag = [dict(r) for r in conn.execute("SELECT * FROM diagnoses ORDER BY diagnosis_id")]
    assert [(d["diagnosis_id"], d["gap_type"], d["claims"], d["abstain_reason"], d["created_at"])
            for d in diag] == [(4, "G2", claims, None, "2026-11-12T00:00:00Z"),
                               (5, None, "[]", "no_evidence: x", "2026-11-12T00:00:01Z")]
    seq = dict(conn.execute("SELECT name, seq FROM sqlite_sequence"))
    assert seq["gold"] == 9 and seq["diagnoses"] == 5
    assert not {"gold_0012", "diagnoses_0012"} & seq.keys()
    # triggers are back, and the schema is exactly what a fresh database has
    with pytest.raises(sqlite3.IntegrityError, match="diagnoses claim"), conn:
        conn.execute("INSERT INTO diagnoses (company_id, prompt_version, attempted, gap_type, "
                     "need, cannot_see, claims) VALUES ('zentrico-io', 'j-2', 1, 'G6', 'n', 'c', "
                     "'[{\"found\": true, \"url\": \"u\"}]')")
    with conn:
        conn.execute("INSERT INTO gold (company_id, gap_type, abstain, split, gaps_version) "
                     "VALUES ('zentrico-io', 'G6', 0, 'teach', 'v2.0')")
    assert conn.execute("SELECT gold_id FROM gold WHERE gaps_version = 'v2.0'").fetchone()[0] == 10
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    fresh_path, _ = db.init_db(tmp_path / "fresh.db", upto13)
    fresh = db.connect(fresh_path)
    q = "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    assert [tuple(r) for r in conn.execute(q)] == [tuple(r) for r in fresh.execute(q)]
    conn.close()
    fresh.close()


# -- the split ---------------------------------------------------------------------------

# Seven invented companies: (company_id, segment, gap or None for an abstention)
SET = [
    ("arvel-example", "in_saas", "G1"),
    ("brisk-example", "in_saas", "G2"),
    ("cobalt-example", "us_india_team", "G3"),
    ("dunmore-example", "us_india_team", None),
    ("elkway-example", "in_saas", "G5"),
    ("fernlow-example", "us_india_team", "G4"),
    ("gantry-example", "in_saas", "G2"),
]
TEACH = ["arvel-example", "brisk-example", "cobalt-example", "dunmore-example", "elkway-example"]
HELD_QUOTE = "Fernlow hires its first account executive for the Gulf region"
TEACH_QUOTE = "Arvel builds payroll for Indian clinics and pharmacies"


def _quote(cid: str) -> str:
    return {"arvel-example": TEACH_QUOTE, "fernlow-example": HELD_QUOTE}.get(
        cid, f"The {cid} pricing page lists three plans and a demo form")


def _gold_set(conn: sqlite3.Connection, set_=SET) -> None:
    for cid, seg, gap in set_:
        add_company(conn, cid, name=cid.split("-")[0].title(), segment=seg, pilot=True)
        ev = [GoldEvidence(kind="pricing", found=True, url=f"https://{cid}.example/pricing",
                           quote=_quote(cid)),
              GoldEvidence(kind="customers", found=False, url=f"https://{cid}.example/")]
        with conn:
            db.insert(conn, Gold(company_id=cid, gap_type=gap, abstain=gap is None,
                                 abstain_reason=None if gap else "contradicted: G1; segments",
                                 evidence=ev, split="held_out", gaps_version="v1.2"))


def test_split_marks_five_teach_and_the_rest_held_out(conn) -> None:
    _gold_set(conn)
    plan = G.plan_split(conn, TEACH)
    assert plan.teach == sorted(TEACH) and plan.held_out == ["fernlow-example", "gantry-example"]
    G.write_split(conn, plan)
    got = dict(conn.execute("SELECT company_id, split FROM gold"))
    assert got == {c: ("teach" if c in TEACH else "held_out") for c, _, _ in SET}
    assert G.split_status(conn) == []


@pytest.mark.parametrize("teach, why", [
    (["arvel-example", "brisk-example", "cobalt-example", "elkway-example", "fernlow-example"],
     "at least one abstention"),
    (["arvel-example", "brisk-example", "elkway-example", "gantry-example", "dunmore-example"],
     "missing in_saas|missing us_india_team"),
    (["arvel-example", "brisk-example", "gantry-example", "dunmore-example", "hollin-example"],
     "at least 3 gap types, not 2"),
    (TEACH[:4] + ["nobody-example"], "no gold row"),
    (TEACH[:4] + ["arvel-example"], "named twice"),
])
def test_split_rules(conn, teach, why) -> None:
    _gold_set(conn, SET + [("hollin-example", "in_saas", None)])
    if "missing" in why:  # five with one segment only
        teach = ["arvel-example", "brisk-example", "elkway-example", "gantry-example",
                 "hollin-example"]
        why = "missing us_india_team"
    with pytest.raises(G.GoldError, match=why):
        G.plan_split(conn, teach)
    assert set(dict(conn.execute("SELECT company_id, split FROM gold")).values()) == {"held_out"}


def test_split_gap_types_count_only_named_gaps(conn) -> None:
    _gold_set(conn, [("arvel-example", "in_saas", "G1"), ("brisk-example", "in_saas", "G1"),
                     ("cobalt-example", "us_india_team", "G2"),
                     ("dunmore-example", "us_india_team", None),
                     ("elkway-example", "in_saas", None)])
    with pytest.raises(G.GoldError, match="at least 3 gap types, not 2"):
        G.plan_split(conn, TEACH)


def test_resplit_needs_force(conn) -> None:
    _gold_set(conn)
    G.write_split(conn, G.plan_split(conn, TEACH))
    other = TEACH[:4] + ["fernlow-example"]  # G4 for G5: still three gap types
    with pytest.raises(G.GoldError, match="--force"):
        G.plan_split(conn, other)
    G.write_split(conn, G.plan_split(conn, other, force=True))
    assert G.teach_companies(conn) == set(other)
    G.plan_split(conn, other)  # the same five again needs no force


def test_cli_split_needs_exactly_five(db_file, conn) -> None:
    _gold_set(conn)
    base = ["gold", "split", "--db", str(db_file), "--teach"]
    r = CliRunner().invoke(main, base + TEACH[:4])
    assert r.exit_code == 2
    r = CliRunner().invoke(main, base + TEACH + ["--dry-run"])
    assert r.exit_code == 0, r.output
    assert "would set teach 5" in r.output
    assert G.teach_companies(conn) == set()
    r = CliRunner().invoke(main, base + TEACH)
    assert r.exit_code == 0, r.output
    assert "set held_out 2" in r.output and G.teach_companies(conn) == set(TEACH)
    r = CliRunner().invoke(main, base + TEACH[:3] + ["elkway-example", "fernlow-example"])
    assert r.exit_code == 1 and "abstention" in r.output
    r = CliRunner().invoke(main, base + TEACH[:4] + ["gantry-example"])
    assert r.exit_code == 1 and "--force" in r.output


def test_add_inherits_split_and_caps_teach(conn, cache, bundles) -> None:
    _gold_set(conn)
    G.write_split(conn, G.plan_split(conn, TEACH))
    with pytest.raises(G.GoldError, match="already 5 teach"):
        add(conn, cache, abstain="thin: x", split="teach")
    plan = add(conn, cache, abstain="thin: x")
    assert plan.row.split == "held_out"


# -- list hides held_out -----------------------------------------------------------------


def test_list_hides_held_out(db_file, conn) -> None:
    _gold_set(conn)
    G.write_split(conn, G.plan_split(conn, TEACH))
    r = CliRunner().invoke(main, ["gold", "list", "--db", str(db_file)])
    assert r.exit_code == 0, r.output
    lines = {ln.split()[0]: ln for ln in r.output.splitlines()[1:-1]}
    assert lines["fernlow-example"].split() == ["fernlow-example", "held_out"]
    assert lines["gantry-example"].split() == ["gantry-example", "held_out"]
    assert "G4" not in r.output and HELD_QUOTE not in r.output
    assert "G1" in lines["arvel-example"] and "contradicted" in lines["dunmore-example"]
    assert "teach 5, held_out 2" in r.output
    assert TEACH_QUOTE not in r.output  # list never prints quotes


def test_list_reveal_only_to_a_terminal_with_a_warning(db_file, conn, monkeypatch) -> None:
    _gold_set(conn)
    G.write_split(conn, G.plan_split(conn, TEACH))
    args = ["gold", "list", "--db", str(db_file), "--reveal-held-out"]
    r = CliRunner().invoke(main, args)
    assert r.exit_code == 1 and "only to a terminal" in r.output and "G4" not in r.output
    monkeypatch.setattr(cli_mod, "_stdout_is_terminal", lambda: True)
    r = CliRunner().invoke(main, args)
    assert r.exit_code == 0, r.output
    assert r.output.startswith("WARNING: held-out") and "never paste" in r.output.lower()
    fern = next(ln for ln in r.output.splitlines() if ln.startswith("fernlow-example"))
    assert "G4" in fern


# -- export-teach ------------------------------------------------------------------------


def test_export_refuses_before_the_split(db_file, conn) -> None:
    _gold_set(conn)
    with pytest.raises(G.GoldError, match="split is not recorded"):
        G.teach_export(conn)
    r = CliRunner().invoke(main, ["gold", "export-teach", "--db", str(db_file)])
    assert r.exit_code == 1 and "split is not recorded" in r.output


def test_export_has_the_five_and_no_held_out(db_file, conn, tmp_path) -> None:
    _gold_set(conn)
    G.write_split(conn, G.plan_split(conn, TEACH))
    rows = G.teach_export(conn)
    assert [r["company_id"] for r in rows] == sorted(TEACH)
    arvel = rows[0]
    assert arvel["segment"] == "in_saas" and arvel["gap_type"] == "G1"
    assert arvel["evidence"][0]["quote"] == TEACH_QUOTE
    assert [r["abstain"] for r in rows].count(True) == 1
    out = tmp_path / "teach.json"
    r = CliRunner().invoke(main, ["gold", "export-teach", "--db", str(db_file), "--out", str(out)])
    assert r.exit_code == 0, r.output
    text = out.read_text(encoding="utf-8")
    assert json.loads(text) == rows
    for held in ("fernlow-example", "gantry-example", HELD_QUOTE, "G4"):
        assert held not in text
    r = CliRunner().invoke(main, ["gold", "export-teach", "--db", str(db_file)])
    assert r.exit_code == 0 and json.loads(r.output) == rows


def test_export_refuses_a_teach_quote_holding_a_held_out_quote(conn) -> None:
    _gold_set(conn)
    G.write_split(conn, G.plan_split(conn, TEACH))
    leaky = json.dumps([{"kind": "pricing", "found": True, "url": "https://a.example/",
                         "quote": f"As the page says: {HELD_QUOTE.upper()}"}])
    with conn:
        conn.execute("UPDATE gold SET evidence = ? WHERE company_id = 'arvel-example'", (leaky,))
    with pytest.raises(G.GoldError, match="held-out company or quote"):
        G.teach_export(conn)


# -- held-out guard ----------------------------------------------------------------------


def test_held_out_values(conn) -> None:
    _gold_set(conn)
    G.write_split(conn, G.plan_split(conn, TEACH))
    with conn:
        conn.execute("UPDATE gold SET evidence = json_insert(evidence, '$[#]', json(?)) "
                     "WHERE company_id = 'gantry-example'",
                     (json.dumps({"kind": "jd", "found": True, "url": "u", "quote": "Short one"}),))
    h = G.held_out_values(conn)
    assert h.company_ids == {"fernlow-example", "gantry-example"}
    assert G.guard_norm(HELD_QUOTE) in h.quotes
    assert G.guard_norm(TEACH_QUOTE) not in h.quotes  # teach rows are fine
    assert "short one" not in h.quotes  # under 20 characters


@pytest.fixture
def split_set(conn) -> None:
    _gold_set(conn)
    G.write_split(conn, G.plan_split(conn, TEACH))


@pytest.mark.parametrize("prompt", [
    f"Worked example: {HELD_QUOTE}.",
    "worked example: " + HELD_QUOTE.upper().replace(" ", "\n  "),
])
def test_gateway_refuses_a_held_out_quote(conn, split_set, monkeypatch, prompt) -> None:
    a = Fake(GOOD)
    _install(monkeypatch, a)
    budget = Budget()
    with pytest.raises(HeldOutInPrompt) as exc:
        gateway.call("judge", "arvel-example", "j-1.0", ROUTE, prompt, SCHEMA, conn=conn,
                     budget=budget)
    assert a.calls == 0 and budget.model_calls == 0
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
    assert "fernlow" not in str(exc.value).lower() and "gulf" not in str(exc.value).lower()
    with pytest.raises(HeldOutInPrompt):  # a call about no company is refused too
        gateway.call("classify", None, "c-1", ROUTE, prompt, SCHEMA, conn=conn, budget=budget)


def test_gateway_refuses_a_held_out_quote_in_the_schema(conn, split_set, monkeypatch) -> None:
    _install(monkeypatch, Fake(GOOD))
    schema = dict(SCHEMA, description=HELD_QUOTE)
    with pytest.raises(HeldOutInPrompt):
        gateway.call("judge", "arvel-example", "j-1.0", ROUTE, "p", schema, conn=conn,
                     budget=Budget())


@pytest.mark.parametrize("company, prompt", [
    ("fernlow-example", f"Fernlow's job post: {HELD_QUOTE}."),  # its own pages (Step 50)
    ("arvel-example", f"Worked example: {TEACH_QUOTE}."),  # a teach quote
    ("arvel-example", "Fernlow hires"),  # too short a fragment to match
])
def test_gateway_allows(conn, split_set, monkeypatch, company, prompt) -> None:
    a = Fake(GOOD)
    _install(monkeypatch, a)
    assert gateway.call("judge", company, "j-1.0", ROUTE, prompt, SCHEMA, conn=conn,
                        budget=Budget()) == GOOD
    assert a.calls == 1


# -- Gate 4.1 ----------------------------------------------------------------------------


def _eighteen(conn) -> None:
    extra = [(f"filler{i:02d}-example", "in_saas" if i % 2 else "us_india_team", "G2")
             for i in range(11)]
    _gold_set(conn, SET + extra)
    G.write_split(conn, G.plan_split(conn, TEACH))


def test_gate_fails_at_gaps_1_2(db_file, conn) -> None:
    _eighteen(conn)
    assert G.gaps_file_version() == (1, 2)  # protocol/GAPS.md today
    lines, _ = G.gate(conn)
    assert [ln.ok for ln in lines] == [True, True, False]
    assert "GAPS.md at version 1.2" in lines[2].text
    r = CliRunner().invoke(main, ["gold", "check", "--db", str(db_file)])
    assert r.exit_code == 1 and "Gate 4.1 fails" in r.output
    assert "FAIL GAPS.md at version 1.2" in r.output


def test_gate_passes_at_gaps_2(conn, tmp_path) -> None:
    _eighteen(conn)
    gaps = tmp_path / "GAPS.md"
    gaps.write_text("# GAPS.md\n\nVersion 2.0 · 2026-11-20\n", encoding="utf-8")
    lines, warnings = G.gate(conn, gaps)
    assert all(ln.ok for ln in lines), lines
    assert any("not at GAPS.md's v2.0" in w for w in warnings)


def test_gate_counts_and_split(conn, tmp_path) -> None:
    gaps = tmp_path / "GAPS.md"
    gaps.write_text("Version 2\n", encoding="utf-8")
    lines, _ = G.gate(conn, gaps)
    assert [ln.ok for ln in lines] == [False, False, True]
    _gold_set(conn)  # seven, none teach
    lines, _ = G.gate(conn, gaps)
    assert "7 companies" in lines[0].text and not lines[1].ok
    assert "teach 0" in lines[1].text

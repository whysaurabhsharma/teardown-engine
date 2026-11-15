"""Steps 83-85, Gate 7.1 (te report), the hand minutes gap and the debug export gap.

Invented companies and people only: the campaign fixture of
tests/test_outreach_campaign.py (Quillfern, Brasswick and the rest). Sockets
are blocked; everything is under tmp_path.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine.cli import main
from engine.debug import EXCLUDED_TABLES, REDACTED, export_company
from engine.guard.cache import Cache
from engine.outreach import metrics as MX
from engine.outreach import replies as RP
from engine.report import anonymise as RA
from engine.report import figures as RF
from engine.report import numbers as RN
from engine.report.stats import fmt, rate, wilson
from engine.resolve import hand as H
from engine.sensitive import Terms, terms
from tests.test_outreach_campaign import NAMES, PEOPLE, TODAY, campaign, no_net  # noqa: F401

# -- Wilson -------------------------------------------------------------------------------


@pytest.mark.parametrize("k,n,lo,hi", [
    (0, 10, 0.0, 0.2775),     # textbook values
    (1, 10, 0.0179, 0.4042),
    (5, 10, 0.2366, 0.7634),
    (10, 10, 0.7225, 1.0),
    (14, 70, 0.1230, 0.3082),  # Step 83: "roughly 12 to 31 percent"
])
def test_wilson_known_values(k, n, lo, hi) -> None:
    got = wilson(k, n)
    assert got is not None
    assert got[0] == pytest.approx(lo, abs=1e-3) and got[1] == pytest.approx(hi, abs=1e-3)


def test_wilson_edges_and_format() -> None:
    assert wilson(0, 0) is None
    with pytest.raises(ValueError):
        wilson(3, 2)
    assert rate(0, 0) == {"n": 0, "of": 0, "rate": None, "low": None, "high": None}
    assert fmt(rate(14, 70)) == "14/70  20.0%  (95% CI 12.3 to 30.8%)"
    assert fmt(rate(0, 0)) == "0/0  n/a"


# -- numbers ------------------------------------------------------------------------------


def _rec(cid: str, seg: str, sent: bool = True) -> MX.Rec:
    r = MX.Rec(cid, MX.Status.SENT, None, None, False, {"segment": seg})
    r.sent = sent
    return r


def test_small_groups_merge_into_other() -> None:
    groups = {"big": [_rec(f"b{i}", "big") for i in range(3)],
              "tiny": [_rec("t1", "tiny")],
              "small": [_rec("s1", "small"), _rec("s2", "small")],
              "other": [_rec("o1", "other")],
              "unsent": [_rec("u1", "unsent", sent=False)]}
    out = RN.merge_small(groups)
    assert [label for label, _ in out] == ["big", "other"]
    assert sorted(r.company_id for r in dict(out)["other"]) == ["o1", "s1", "s2", "t1"]
    assert RN.merge_small(groups, min_group=1)[0][0] == "big"


def test_numbers_every_rate_has_an_interval(campaign, no_net) -> None:
    with campaign:
        campaign.execute("INSERT INTO judge_scores (prompt_version, held_out, same_call, "
                         "false_gap, faithfulness_failures, not_judged, passed) VALUES "
                         "('j-1.0', 13, 9, 1, 0, 0, 1)")
    n = RN.compute(campaign)
    for key, _ in RN.RATE_LABELS:
        r = n["rates"][key]
        assert set(r) == {"n", "of", "rate", "low", "high"}, key
        assert r["low"] <= r["rate"] <= r["high"], key
    assert (n["rates"]["held_out_agreement"]["n"], n["rates"]["held_out_agreement"]["of"]) \
        == (9, 13)
    m = MX.compute(campaign)
    assert n["rates"]["reply_rate"]["n"] == m["overall"]["reply_rate"]["n"]
    # every split row has at least MIN_GROUP sent companies, unless it is "other"
    for rows in n["by"].values():
        for label, b in rows.items():
            assert label == RN.OTHER or b["sent"] >= RN.MIN_GROUP
    text = "\n".join(RN.format_text(n))
    assert RN.SOFT in text and "95% CI" in text and "same call on 9 of 13" in text
    blob = text + RN.format_json(n)
    for cid, name in NAMES.items():
        assert name not in blob and f"{cid}.example" not in blob
    for person in PEOPLE.values():
        assert person[0] not in blob
    assert "Keen to talk" not in blob  # reply notes never reach the report
    assert RN.format_json(RN.compute(campaign)) == RN.format_json(n)


def test_numbers_cli_json(campaign, db_file, no_net) -> None:
    r = CliRunner().invoke(main, ["report", "numbers", "--json", "--db", str(db_file)])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["version"] == RN.REPORT_VERSION


# -- figures and report all ---------------------------------------------------------------


def test_figures_deterministic_and_anonymous(campaign, no_net) -> None:
    a = RF.render_all(campaign)
    b = RF.render_all(campaign)
    assert a == b and sorted(a) == sorted(RF.FIGURES)
    blob = "".join(a.values())
    for cid, name in NAMES.items():
        assert name not in blob and f"{cid}.example" not in blob and f">{cid}<" not in blob
    assert "Funnel" in a["funnel.svg"] and "no_gap" in a["funnel.svg"]
    assert "Wilson" in a["reply_rate.svg"] and "Hand-made" in a["minutes.svg"]
    assert all(s.startswith("<svg") and s.endswith("</svg>\n") for s in a.values())
    assert not RA.scan_text("x", blob, terms(campaign))


def test_report_all_regenerates_byte_identical(campaign, db_file, tmp_path, no_net) -> None:
    runner = CliRunner()
    for out in ("one", "two"):
        r = runner.invoke(main, ["report", "all", "--out", str(tmp_path / out),
                                 "--db", str(db_file)])
        assert r.exit_code == 0, r.output
    names = sorted(p.name for p in (tmp_path / "one").iterdir())
    assert names == sorted([*RF.FIGURES, "numbers.json", "numbers.txt"])
    for nm in names:
        assert (tmp_path / "one" / nm).read_bytes() == (tmp_path / "two" / nm).read_bytes()
    r = runner.invoke(main, ["report", "figures", "--out", str(tmp_path / "f"),
                             "--db", str(db_file)])
    assert r.exit_code == 0 and sorted(p.name for p in (tmp_path / "f").iterdir()) == \
        sorted(RF.FIGURES)


def test_report_all_refuses_when_output_would_leak(campaign, db_file, tmp_path,
                                                    no_net) -> None:
    # a segment value that happens to be a company's name must not be published
    with campaign:
        campaign.execute("UPDATE companies SET segment = 'Quillfern'")
    r = CliRunner().invoke(main, ["report", "all", "--out", str(tmp_path / "o"),
                                  "--db", str(db_file)])
    assert r.exit_code == 1 and "refused" in r.output
    assert "company_name" in r.output and "Quillfern" not in r.output
    assert not (tmp_path / "o").exists()


# -- anonymise-check ----------------------------------------------------------------------


def test_anonymise_check_finds_every_kind_and_never_prints_values(campaign, db_file,
                                                                  tmp_path, no_net) -> None:
    with campaign:
        campaign.execute("INSERT INTO closes (company_id, outcome, next_step, closed_on) "
                         "VALUES ('a1', 'not_now', 'Write again after the March budget', "
                         "'2026-11-12')")
    draft = tmp_path / "writeup.md"
    draft.write_text("\n".join([
        "# Results",                                        # 1 clean
        "We wrote to QUILLFERN first.",                     # 2 company name, any case
        "Their site, https://www.a2.example/pricing, was",  # 3 domain
        "read by ilsa@a1.example on day two.",              # 4 email, domain, name part
        "Doran Petch replied.",                             # 5 contact name
        "The VP Sales said it was",                         # 6 contact title (two words)
        "keen to talk",                                     # 7 note across a line break
        "next week.",
        "Quillferns and xa2.example and a2.examples stay.",  # 9 not whole word / label
    ]) + "\n", encoding="utf-8")
    hits = {(h.line, h.kind) for h in RA.scan_files([draft], terms(campaign))}
    assert hits == {(2, "company_name"), (3, "domain"), (4, "contact_email"), (4, "domain"),
                    (4, "contact_name"),
                    (5, "contact_name"), (6, "contact_title"), (7, "note")}
    r = CliRunner().invoke(main, ["report", "anonymise-check", str(tmp_path),
                                  "--db", str(db_file)])
    assert r.exit_code == 1
    assert "writeup.md:2: company_name" in r.output
    for value in ("Quillfern", "QUILLFERN", "a2.example", "ilsa@", "Doran", "Petch",
                  "VP Sales", "keen to talk"):
        assert value not in r.output, value
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "ok.md").write_text("A founder replied in week two.\n", encoding="utf-8")
    r = CliRunner().invoke(main, ["report", "anonymise-check", str(clean), "--db", str(db_file)])
    assert r.exit_code == 0 and "clean: 1 file(s)" in r.output


def test_terms_rules() -> None:
    t = Terms([("contact_title", "Founder"), ("company_name", "Ab"),
               ("note", "short note"), ("domain", "www.Zentrico.example")])
    # one-word titles, names under 3 characters and notes under 20 are not terms
    assert t.kinds_in("Founder Ab short note") == set()
    assert t.kinds_in("mail@zentrico.example") == {"domain"}


# -- hand minutes (te company add --hand) -------------------------------------------------


def _entry(**kw) -> H.HandEntry:
    base = dict(company="Larkspen", domain="larkspen.example", segment="in_saas",
                role="Founding SDR", role_url="https://larkspen.example/jobs/1",
                offer="adjacent", family="sdr", sent_on=date(2026, 10, 20))
    base.update(kw)
    return H.HandEntry(**base)


def test_hand_minutes_recorded_and_used_by_metrics(conn: sqlite3.Connection) -> None:
    res = H.add(conn, _entry(minutes_research=90, minutes_writing=60, minutes_slice=30),
                today=TODAY)
    assert any(c.startswith("minutes ") for c in res.changes)
    row = conn.execute("SELECT research, writing, slice FROM hand_minutes").fetchone()
    assert tuple(row) == (90, 60, 30)
    m = MX.compute(conn)
    assert m["minutes_per_teardown"]["hand"] == {"teardowns": 1, "mean": 180.0,
                                                 "median": 180.0}
    assert m["minutes_per_teardown"]["hand_not_recorded"] == 0
    assert m["overall"]["review_hours"] == 3.0
    # idempotent; a different value is reported, never overwritten
    again = H.add(conn, _entry(minutes_research=90, minutes_writing=60, minutes_slice=30),
                  today=TODAY)
    assert again.changes == []
    diff = H.add(conn, _entry(minutes_research=45), today=TODAY)
    assert any("minutes research is 90" in w for w in diff.warnings)
    with pytest.raises(H.HandError):
        H.prepare(_entry(minutes_slice=-1), TODAY)


def test_hand_minutes_cli(db_file: Path) -> None:
    r = CliRunner().invoke(main, [
        "company", "add", "--hand", "--company", "Larkspen", "--domain", "larkspen.example",
        "--segment", "in_saas", "--role", "Founding SDR",
        "--role-url", "https://larkspen.example/jobs/1", "--offer", "adjacent",
        "--sent-on", "2026-10-20", "--minutes-research", "120", "--minutes-writing", "45",
        "--db", str(db_file)])
    assert r.exit_code == 0, r.output
    assert "minutes research 120, writing 45" in r.output


# -- debug export: replies, meetings, closes ----------------------------------------------


def test_debug_export_includes_conversations_redacted(campaign, tmp_path, no_net) -> None:
    # `te reply` and `te close` refuse a note naming a person; a row written by
    # other means must still come out redacted
    with campaign:
        campaign.execute("INSERT INTO replies (company_id, touch_no, type, replied_on, note) "
                         "VALUES ('a1', 1, 'question', '2026-11-07', 'Ilsa asked about price')")
        campaign.execute("INSERT INTO closes (company_id, outcome, next_step, closed_on) VALUES "
                         "('a1', 'not_now', 'Write to Ilsa Marwick in March', '2026-11-12')")
        campaign.execute("INSERT INTO hubspot_sync (object, te_key, hubspot_id) VALUES "
                         "('deal', 'a1', 'hs-deal-991')")
    dest = export_company(campaign, "a1", tmp_path / "debug", Cache(tmp_path / "cache"))
    rec = json.loads((dest / "record.json").read_text(encoding="utf-8"))
    assert len(rec["replies"]) == 2 and len(rec["meetings"]) == 1 and len(rec["closes"]) == 1
    blob = json.dumps(rec)
    assert "Ilsa" not in blob and "Marwick" not in blob and REDACTED in blob
    assert "hubspot_sync" not in rec and "hs-deal-991" not in blob
    assert "hubspot_sync" in EXCLUDED_TABLES
    assert rec["export_version"] == "debug-export-v2"

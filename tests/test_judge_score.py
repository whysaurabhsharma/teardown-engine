"""Step 50: `te judge score` on the held-out gold rows, and the rounds record (0014).

An invented eighteen-row gold set: the teach five from tests/test_gold.py, and
thirteen invented held-out companies (Quorvane, from tests/test_extract_facts.py,
among them, so one is judged through the fake judge). No real model call: the
providers are scripted fakes and sockets are blocked.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from click.testing import CliRunner

from engine import cli as cli_mod
from engine import db
from engine.cli import main
from engine.judge import gold as G
from engine.judge import judge as J
from engine.judge import score as S
from engine.models import Diagnosis, Gold, GoldEvidence
from tests.conftest import add_company
from tests.test_extract_facts import QID, bundles, cache  # noqa: F401 (fixtures)
from tests.test_gold import SET, TEACH, _quote
from tests.test_judge import GAP, JOB, PRICING, extracted, install, no_net  # noqa: F401

# Twelve invented held-out companies besides Quorvane.
HELD = [f"held{i:02d}-example" for i in range(12)]


def gold(conn: sqlite3.Connection, cid: str, gap: str | None, split: str,
         segment: str = "in_saas") -> None:
    if conn.execute("SELECT 1 FROM companies WHERE company_id = ?", (cid,)).fetchone() is None:
        add_company(conn, cid, name=cid.split("-")[0].title(), segment=segment, pilot=True)
    ev = [GoldEvidence(kind="pricing", found=True, url=f"https://{cid}.example/pricing",
                       quote=_quote(cid))]
    with conn:
        db.insert(conn, Gold(company_id=cid, gap_type=gap, abstain=gap is None,
                             abstain_reason=None if gap else "no_evidence: human abstained",
                             evidence=ev, split=split, gaps_version="v1.2"))


def diagnose(conn: sqlite3.Connection, cid: str, gap: str | None, claims=()) -> None:
    with conn:
        db.insert(conn, Diagnosis(
            company_id=cid, prompt_version=J.PROMPT_VERSION, attempted=gap is not None,
            gap_type=gap, need="n" if gap else None, cannot_see="c" if gap else None,
            abstain_reason=None if gap else "no_evidence: nothing shown", claims=list(claims)))


def eighteen(conn: sqlite3.Connection, held: list[tuple[str | None, str | None]],
             quorvane_gap: str | None = "G3") -> None:
    """The teach five (each diagnosed as its gold call), Quorvane held out with the
    human calling quorvane_gap (not diagnosed yet), and twelve held-out companies,
    each given (human call, judge call); None is an abstention."""
    assert len(held) == len(HELD)
    for cid, seg, gap in SET[:5]:
        gold(conn, cid, gap, "teach", seg)
        diagnose(conn, cid, gap)
    gold(conn, QID, quorvane_gap, "held_out")
    for cid, (human, judge) in zip(HELD, held, strict=True):
        gold(conn, cid, human, "held_out")
        diagnose(conn, cid, judge)
    assert sorted(G.teach_companies(conn)) == sorted(TEACH)


# 12 held-out besides Quorvane: 10 agree, one false gap, one different gap
GOOD = ([("G1", "G1"), ("G2", "G2"), ("G4", "G4"), ("G5", "G5"), (None, None), (None, None),
         ("G2", "G2"), ("G1", "G1"), ("G3", "G3"), ("G5", "G5")]
        + [(None, "G2"), ("G1", "G4")])


@pytest.fixture
def judge_ready(conn, cache, extracted, monkeypatch):
    """Quorvane extracted; the fake claude judge answers G3 once."""
    claude, gemini = install(monkeypatch, GAP)
    return claude


def run_cli(db_file, cache, *args):
    return CliRunner().invoke(main, ["judge", "score", "--db", str(db_file), "--cache-dir",
                                     str(cache.root), *args])


# -- counting ----------------------------------------------------------------------------


def test_score_meets_targets_and_judges_the_undiagnosed(conn, cache, judge_ready) -> None:
    eighteen(conn, GOOD)
    s = S.run(conn, cache)
    assert (s.held_out, s.same_call, s.false_gap, s.faithfulness_failures, s.not_judged) == (
        13, 11, 1, 0, 0)
    assert s.passed and judge_ready.calls == 1 and s.judged_now == 1 and s.calls == 1
    q = next(r for r in s.rows if r.company_id == QID)
    assert q.same and q.judge_gap == "G3"
    s2 = S.run(conn, cache)  # stored diagnoses: no call
    assert judge_ready.calls == 1 and s2.calls == 0 and s2.same_call == 11


def test_rows_are_held_out_only(conn, cache, judge_ready) -> None:
    eighteen(conn, GOOD)
    s = S.run(conn, cache)
    assert {r.company_id for r in s.rows} == set(HELD) | {QID}
    assert not {r.company_id for r in s.rows} & set(TEACH)


@pytest.mark.parametrize("held, why", [
    ([("G1", "G2")] * 2 + GOOD[2:], "same call as the human: 9 of 13"),
    (GOOD[:10] + [(None, "G2"), (None, "G1")], "where the human abstained: 2"),
])
def test_below_target_fails(conn, cache, judge_ready, held, why) -> None:
    eighteen(conn, held)
    s = S.run(conn, cache)
    assert not s.passed
    assert any(not ok and why in text for ok, text in s.checks())


def test_faithfulness_failure_on_a_named_gap_fails(conn, cache, judge_ready) -> None:
    eighteen(conn, GOOD)
    with conn:  # one held-out diagnosis carries a claim no captured artifact holds
        conn.execute("DELETE FROM diagnoses WHERE company_id = ?", (HELD[0],))
    diagnose(conn, HELD[0], "G1", [{"text": "t", "artifact_kind": "pricing", "found": True,
                                    "url": f"https://{HELD[0]}.example/pricing",
                                    "quote": "An invented sentence"}])
    s = S.run(conn, cache)
    assert s.faithfulness_failures == 1 and not s.passed
    assert s.same_call == 11  # the call still counts; the claim would not reach a draft


def test_an_abstention_reaches_no_draft(conn, cache, judge_ready) -> None:
    eighteen(conn, GOOD)
    with conn:
        conn.execute("DELETE FROM diagnoses WHERE company_id = ?", (HELD[4],))
    diagnose(conn, HELD[4], None, [{"text": "t", "artifact_kind": "pricing", "found": True,
                                    "url": "https://x.example/", "quote": "Invented words"}])
    assert S.run(conn, cache).faithfulness_failures == 0


def test_not_judged_rows_are_not_a_same_call(conn, cache, judge_ready, monkeypatch) -> None:
    eighteen(conn, GOOD)
    with conn:  # dropped before extract: the judge cannot run on it
        conn.execute("UPDATE companies SET status = 'dropped', drop_reason = 'x', "
                     "drop_stage = 'resolve' WHERE company_id = ?", (QID,))
    s = S.run(conn, cache)
    q = next(r for r in s.rows if r.company_id == QID)
    assert not q.judged and q.note == "not extracted" and not q.same
    assert (s.same_call, s.not_judged, judge_ready.calls) == (10, 1, 0)


def test_twelve_held_out_rows_fail_the_gate(conn, cache, judge_ready) -> None:
    eighteen(conn, GOOD)
    with conn:
        conn.execute("DELETE FROM gold WHERE company_id = ?", (QID,))
    s = S.run(conn, cache)
    assert s.held_out == 12 and not s.passed


def test_dry_run_makes_no_call(conn, cache, judge_ready) -> None:
    eighteen(conn, GOOD)
    s = S.run(conn, cache, dry_run=True)
    assert judge_ready.calls == 0 and s.not_judged == 1
    assert J.stored(conn, QID, J.PROMPT_VERSION) is None


def test_refuses_without_the_split(conn, cache) -> None:
    for cid, seg, gap in SET:
        gold(conn, cid, gap, "held_out", seg)
    with pytest.raises(S.ScoreError, match="split"):
        S.run(conn, cache)
    with conn:
        conn.execute("DELETE FROM gold")
    with pytest.raises(S.ScoreError, match="no gold rows"):
        S.run(conn, cache)


# -- the CLI and what it prints ----------------------------------------------------------


def _held_out_secrets(conn: sqlite3.Connection) -> list[str]:
    out = []
    for r in conn.execute("SELECT evidence FROM gold WHERE split = 'held_out'"):
        out += [e["quote"] for e in json.loads(r["evidence"])]
    return out


def test_cli_exit_0_when_targets_met_and_no_held_out_detail(conn, db_file, cache,
                                                            judge_ready) -> None:
    eighteen(conn, GOOD)
    r = run_cli(db_file, cache)
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    held_lines = [ln for ln in lines if ln.split(":")[0] in set(HELD) | {QID}]
    assert len(held_lines) == 13
    for ln in held_lines:
        assert ln.split(": ", 1)[1] in ("agree", "disagree"), ln
    for q in _held_out_secrets(conn):
        assert q not in r.output
    body = "\n".join(held_lines)
    for gap in ("G1", "G2", "G3", "G4", "G5", "abstain"):
        assert gap not in body
    assert "ok   same call as the human: 11 of 13" in r.output
    assert "round 1 of at most 3" in r.output and "recorded" in r.output
    assert "Gate 4.2 targets met" in r.output


def test_cli_exit_1_below_targets(conn, db_file, cache, judge_ready) -> None:
    eighteen(conn, [("G1", "G2")] * 2 + GOOD[2:])
    r = run_cli(db_file, cache)
    assert r.exit_code == 1
    assert "FAIL same call as the human: 9 of 13" in r.output and "not met" in r.output


def test_cli_reveal_only_to_a_terminal(conn, db_file, cache, judge_ready, monkeypatch) -> None:
    eighteen(conn, GOOD)
    r = run_cli(db_file, cache, "--reveal-held-out")
    assert r.exit_code == 1 and "only to a terminal" in r.output
    assert "human" not in r.output and "G1" not in r.output
    assert judge_ready.calls == 0  # refused before anything ran
    monkeypatch.setattr(cli_mod, "_stdout_is_terminal", lambda: True)
    r = run_cli(db_file, cache, "--reveal-held-out")
    assert r.exit_code == 0, r.output
    assert r.output.startswith("WARNING: held-out")
    assert f"{HELD[10]}: disagree: human abstain, judge G2" in r.output
    for q in _held_out_secrets(conn):  # never a quote, even to a terminal
        assert q not in r.output


# -- rounds ------------------------------------------------------------------------------


def test_rounds_recorded_once_per_result(conn, db_file, cache, judge_ready) -> None:
    eighteen(conn, GOOD)
    run_cli(db_file, cache)
    r = run_cli(db_file, cache)
    assert "not recorded again" in r.output
    rows = S.history(conn)
    assert len(rows) == 1
    row = rows[0]
    assert (row["prompt_version"], row["held_out"], row["same_call"], row["false_gap"],
            row["faithfulness_failures"], row["not_judged"], row["passed"]) == (
        "j-1.0", 13, 11, 1, 0, 0, 1)
    with conn:  # the result changes: a new row, still round 1
        conn.execute("UPDATE diagnoses SET gap_type = 'G5' WHERE company_id = ?", (HELD[0],))
    r = run_cli(db_file, cache)
    assert "round 1 of at most 3" in r.output and len(S.history(conn)) == 2
    assert S.rounds(conn) == ["j-1.0"]


def test_dry_run_records_nothing(conn, db_file, cache, judge_ready) -> None:
    eighteen(conn, GOOD)
    r = run_cli(db_file, cache, "--dry-run")
    assert "nothing recorded" in r.output and S.history(conn) == []
    assert judge_ready.calls == 0 and r.exit_code == 0, r.output  # 10 of 13 still
    assert f"{QID}: not judged (no diagnosis stored)" in r.output


def test_fourth_round_warns(conn, db_file, cache, judge_ready) -> None:
    eighteen(conn, GOOD)
    with conn:
        for v in ("j-0.1", "j-0.2", "j-0.3"):
            conn.execute("INSERT INTO judge_scores (prompt_version, held_out, same_call, "
                         "false_gap, faithfulness_failures, not_judged, passed) VALUES "
                         "(?, 13, 8, 2, 0, 0, 0)", (v,))
    assert S.round_number(conn, "j-0.3") == 3 and S.round_warning(conn, "j-0.3") is None
    r = run_cli(db_file, cache)
    assert "warning: round 4 against the held-out set" in r.output
    assert "allows 3 at most" in r.output
    assert S.rounds(conn) == ["j-0.1", "j-0.2", "j-0.3", "j-1.0"]


def test_third_round_does_not_warn(conn, db_file, cache, judge_ready) -> None:
    eighteen(conn, GOOD)
    with conn:
        for v in ("j-0.1", "j-0.2"):
            conn.execute("INSERT INTO judge_scores (prompt_version, held_out, same_call, "
                         "false_gap, faithfulness_failures, not_judged, passed) VALUES "
                         "(?, 13, 8, 2, 0, 0, 0)", (v,))
    r = run_cli(db_file, cache)
    assert "round 3 of at most 3" in r.output and "warning: round" not in r.output

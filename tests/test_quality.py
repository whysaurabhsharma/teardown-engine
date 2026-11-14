"""Step 65: the automated quality gates.

Invented companies only: Quorvane, drafted by the scripted fake drafter of
tests/test_drafter.py. After drafting, any model call fails the test; the
gates read the database and the files only.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from engine.build import quality as QG
from engine.build.slices import store as SS
from engine.cli import main
from engine.judge import verify as V
from engine.llm import gateway
from tests.test_drafter import (  # noqa: F401 (fixtures)
    _db,
    _fresh_model_budget,
    drafted,
    inputs,
    load,
    paths,
    save,
)
from tests.test_extract_facts import QID, bundles, cache  # noqa: F401 (fixtures)
from tests.test_judge import extracted, no_net, split  # noqa: F401 (fixtures)
from tests.test_judge_verify import judged  # noqa: F401 (fixtures)


@pytest.fixture
def ready(conn, drafted, monkeypatch) -> dict:
    """A drafted Quorvane teardown; from here on no model may be called."""

    def refuse(*a, **kw):
        raise AssertionError("the quality gates made a model call")

    monkeypatch.setattr(gateway, "call", refuse)
    for name in list(gateway.PROVIDERS):
        monkeypatch.setitem(gateway.PROVIDERS, name, refuse)
    return drafted


def gate(conn, paths, **kw) -> QG.Result:
    return QG.run_company(conn, QID, directory=paths["td"], sender_file=paths["sender"], **kw)


def edit(conn, paths, change) -> QG.Result:
    d = load(paths)
    change(d)
    save(paths, d)
    return gate(conn, paths)


def llm_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]


# -- a clean teardown ----------------------------------------------------------------------


def test_a_clean_drafted_teardown_passes_and_is_recorded(conn, ready) -> None:
    before = llm_rows(conn)
    r = gate(conn, ready)
    assert r.reasons == [] and r.passed and r.recorded == "written"
    row = QG.stored(conn, QID)
    assert (row["passed"], json.loads(row["reasons"]), row["version"]) == (1, [], "q-1.0")
    assert row["file_sha256"] == r.file_sha256
    assert gate(conn, ready).recorded == "unchanged"
    assert llm_rows(conn) == before  # zero model calls


# -- each gate -----------------------------------------------------------------------------


def _untraced(d: dict[str, Any]) -> None:
    d["need"]["sentences"].append({"text": "Quorvane reps spend hours on lists.", "claims": []})


def _one_kind(d: dict[str, Any]) -> None:
    d["need"]["sentences"] = [d["need"]["sentences"][0]]
    d["need"]["evidence"] = [0]
    d["email"]["claims"] = [0]


def _no_name(d: dict[str, Any]) -> None:
    for s in d["need"]["sentences"]:
        s["text"] = s["text"].replace("The Quorvane ", "The ")
    d["why_now"] = d["why_now"].replace("Quorvane", "The company")
    d["slice"]["title"] = "Ten dental chains in Pune."


@pytest.mark.parametrize("change,want", [
    (_untraced, "untraced"),
    (_one_kind, "artifact_kinds"),
    (lambda d: d.update(why_now="Quorvane " + "is hiring now " * 15), "word_limit"),
    (lambda d: d.update(why_now="Quorvane is hiring; we can circle back on the CRM."),
     "banned_phrase"),
    (_no_name, "company_name"),
    (lambda d: d.update(why_now="Quorvane is hiring now; its compare page names Routely."),
     "competitor"),
    (lambda d: d["header"].update(offer_type="candidate"), "offer_type"),
    (lambda d: d.update(why_now="I applied for this Quorvane role, so the CRM work is mine."),
     "offer_type"),
    (lambda d: d["email"].update(observation="Two sentences here. Not one."), "email"),
    (lambda d: d.pop("email"), "email"),
    (lambda d: d["slice"]["rows"][0].update(quote="Twelve clinics across Pune"), "slice"),
], ids=["untraced", "artifact_kinds", "word_limit", "banned_phrase", "company_name",
        "competitor", "offer_type", "offer_applied", "email", "no_email", "slice_source"])
def test_each_gate_fails_with_its_named_reason(conn, ready, change, want) -> None:
    r = edit(conn, ready, change)
    assert not r.passed and want in r.gates, [str(x) for x in r.reasons]
    row = QG.stored(conn, QID)
    assert row["passed"] == 0 and want in {x["gate"] for x in json.loads(row["reasons"])}


def test_verifier_review_outcome_fails_even_for_an_uncited_claim(conn, ready) -> None:
    def cite_0_1(d):  # the page and the email cite claims 0 and 1 only
        d["need"]["sentences"] = [{"text": d["need"]["sentences"][0]["text"], "claims": [0]},
                                  {"text": "The Quorvane pricing page routes buyers to a custom "
                                           "quote.", "claims": [1]}]
        d["need"]["evidence"] = [0, 1]

    assert edit(conn, ready, cite_0_1).passed
    v = V.VerifierVerdict.model_validate_json(
        conn.execute("SELECT verifier_verdict FROM diagnoses").fetchone()[0])
    v.claims[2].verdict, v.not_supported, v.outcome = "not_supported", 1, "review"
    with conn:
        conn.execute("UPDATE diagnoses SET verifier_verdict = ?", (v.model_dump_json(),))
    r = gate(conn, ready)
    assert r.gates == {"verifier"}
    assert "review" in r.reasons[0].message and "[2]" in r.reasons[0].message


def test_not_found_on_an_unseen_page_fails_evidence_source(conn, ready) -> None:
    with conn:
        conn.execute("UPDATE artifacts SET outcome = 'blocked' WHERE company_id = ? AND "
                     "kind = 'product'", (QID,))
    r = gate(conn, ready)
    assert "evidence_source" in r.gates
    assert any("blocked" in x.message for x in r.reasons if x.gate == "evidence_source")


def test_unknown_signal_offer_type_fails(conn, ready) -> None:
    with conn:
        conn.execute("UPDATE signals SET offer_type = NULL WHERE company_id = ?", (QID,))
    assert "offer_type" in gate(conn, ready).gates


def test_no_selected_slice_or_a_thin_hold_fails(conn, ready) -> None:
    SS.select(conn, QID, None)
    r = gate(conn, ready)
    assert "slice" in r.gates and any("no proof slice" in x.message for x in r.reasons)
    SS.select(conn, QID, "icp_accounts")
    assert gate(conn, ready).passed
    SS.hold(conn, QID, "role_funnel: 1 row, needs 3")
    r = gate(conn, ready)
    assert r.gates == {"slice"} and "slice_thin" in r.reasons[0].message


def test_dry_run_records_nothing(conn, ready) -> None:
    r = gate(conn, ready, dry_run=True)
    assert r.passed and r.recorded == "dry_run" and QG.stored(conn, QID) is None


def test_cli_quality(conn, ready) -> None:
    args = ["--dir", str(ready["td"]), "--sender", str(ready["sender"]), "--db", _db(conn)]
    r = CliRunner().invoke(main, ["quality", QID, *args])
    assert r.exit_code == 0 and f"{QID}: pass" in r.output, r.output
    d = load(ready)
    d["why_now"] = "Quorvane is hiring; let us circle back."
    save(ready, d)
    r = CliRunner().invoke(main, ["quality", QID, *args])
    assert r.exit_code == 1 and f"{QID}: FAIL banned_phrase" in r.output
    r = CliRunner().invoke(main, ["quality", *args])
    assert r.exit_code == 1 and "give a company_id" in r.output

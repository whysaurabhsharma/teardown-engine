"""Steps 46-48: the judge prompt, the judge, the faithfulness check.

Invented companies only: Quorvane and Brindlecart (the facts fixtures, reused
from tests/test_extract_facts.py) and the invented gold set from
tests/test_gold.py. No real model call: the "claude" and "gemini" providers in
gateway.PROVIDERS are scripted fakes, and sockets are blocked.
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
from typing import Any

import jsonschema
import pytest
from click.testing import CliRunner

from engine import db
from engine.cli import main
from engine.extract import run as R
from engine.extract.base import Art
from engine.judge import faithfulness as F
from engine.judge import gold as G
from engine.judge import judge as J
from engine.llm import claude_headless, gateway
from engine.llm.base import ProviderResult
from engine.models import Claim, Contact, Diagnosis
from tests.test_extract_facts import (  # noqa: F401 (fixtures)
    BID,
    Q_DOMAIN,
    QID,
    art,
    bundles,
    cache,
    quorvane,
)
from tests.test_gold import HELD_QUOTE, TEACH, TEACH_QUOTE, _gold_set
from tests.test_llm_gateway import Fake

HOME = f"https://{Q_DOMAIN}/"
PRICING = f"https://{Q_DOMAIN}/pricing"
JOB = f"https://jobs.{Q_DOMAIN}/sdr"
PRODUCT = f"https://{Q_DOMAIN}/product"
ABOUT = f"https://{Q_DOMAIN}/about"
PRICING_Q = "Talk to our team for a custom quote"
JOB_Q = "Set up HubSpot properties and pipelines as the first sales hire."
NAV_Q = "Dental chains"
FOOTER_Q = "2026 Quorvane Health Private Limited, Pune, India"
ROLE = "Founding Sales Development Representative"


def claim(kind: str, url: str, quote: str | None, text: str = "The page says so.") -> dict:
    return {"text": text, "artifact_kind": kind, "found": quote is not None, "url": url,
            "quote": quote}


GAP = {
    "attempted": True, "gap_type": "G3", "confidence": 0.9,
    "need": "Tooling and process around the first sales hire.",
    "claims": [claim("job_post", JOB, JOB_Q), claim("pricing", PRICING, PRICING_Q),
               claim("product", PRODUCT, None, "No product page was found.")],
    "secondary": ["G5", "G3"], "abstain_reason": None,
    "cannot_see": "Whether a CRM is already set up.",
}
CONTRADICTED = {
    "attempted": False, "gap_type": None, "confidence": 0.8, "need": None,
    "claims": [claim("homepage", HOME, NAV_Q)], "secondary": [],
    "abstain_reason": "contradicted: G1 expected; the nav names Dental chains",
    "cannot_see": None,
}


# -- fixtures ----------------------------------------------------------------------------


@pytest.fixture
def no_net(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a, **kw):
        raise AssertionError("the judge touched the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def extracted(conn: sqlite3.Connection, cache, bundles, no_net) -> None:
    """Quorvane and Brindlecart captured, extracted and at status extracted."""
    rep = R.run(conn, cache, [QID, BID])
    assert rep.rejected == [] and rep.advanced == 2


@pytest.fixture
def split(conn: sqlite3.Connection) -> None:
    """The invented gold set with its teach five recorded (two held out)."""
    _gold_set(conn)
    G.write_split(conn, G.plan_split(conn, TEACH))


def install(monkeypatch: pytest.MonkeyPatch, *replies: Any) -> tuple[Fake, Fake]:
    claude, gemini = Fake(*replies), Fake()  # gemini has no reply: any call fails the test
    monkeypatch.setitem(gateway.PROVIDERS, "claude", claude)
    monkeypatch.setitem(gateway.PROVIDERS, "gemini", gemini)
    return claude, gemini


def status(conn: sqlite3.Connection, cid: str = QID) -> tuple:
    r = conn.execute("SELECT status, drop_reason, drop_stage FROM companies WHERE company_id = ?",
                     (cid,)).fetchone()
    return tuple(r)


def diag_row(conn: sqlite3.Connection, cid: str = QID,
             version: str = J.PROMPT_VERSION) -> sqlite3.Row | None:
    return J.stored(conn, cid, version)


# -- Step 46: the prompt -----------------------------------------------------------------


PARTS = ["## 1. The task", "## 2. The gaps", "## 3. The evidence rules",
         "## 4. Anchored confidence", "## 5. Worked examples", "## 6. The company"]


def test_template_is_versioned_with_six_parts_in_order() -> None:
    assert J.template_version() == J.PROMPT_VERSION == "j-1.0"
    t = J.template()
    heads = re.findall(r"^## .+$", t, re.MULTILINE)
    assert heads == PARTS
    assert "<!--" not in t  # comments are stripped before sending
    assert t.index("{{teach_examples}}") > t.index(PARTS[4])
    assert t.index("{{company}}") > t.index(PARTS[5])
    assert "0.9" in t and "0.6" in t and "0.3" in t and "Below 0.6, abstain" in t
    assert "provisional" in t and "G6" in t


def test_gap_table_matches_gaps_md() -> None:
    mine = J.gap_table(J.template_text())
    theirs = J.gap_table(G.GAPS_PATH.read_text(encoding="utf-8"))
    assert len(mine) == 2 + 6 and mine == theirs
    assert [line.split("|")[1].strip() for line in mine[2:]] == [
        "G1", "G2", "G3", "G4", "G5", "G6 (provisional)"]
    assert J.table_problem() is None


def test_a_stale_gap_table_refuses_to_run(tmp_path, monkeypatch, db_file) -> None:
    stale = tmp_path / "prompt.md"
    stale.write_text(J.template_text().replace("Pricing and CTA agree", "Pricing agrees"),
                     encoding="utf-8")
    monkeypatch.setattr(J, "PROMPT_PATH", stale)
    assert "differs from protocol/GAPS.md" in J.table_problem()
    r = CliRunner().invoke(main, ["judge", "--db", str(db_file), "--no-examples"])
    assert r.exit_code == 1 and "differs from protocol/GAPS.md" in r.output


def test_prompt_holds_the_company_and_six_parts(conn, cache, extracted, split) -> None:
    prompt, masked = J.build_prompt(conn, cache, QID, J.teach_examples(conn))
    assert masked == 0 and "{{" not in prompt
    pos = [prompt.index(p) for p in PARTS]
    assert pos == sorted(pos)
    company = prompt[pos[5]:]
    for s in ("Company: Quorvane", f"Signal role: {ROLE}", "### Facts", "pricing_model",
              PRICING_Q, f"URL: {PRICING}", "### product\nURL: " + PRODUCT,
              "Outcome: not_found", "Outcome: blocked", "make no claim", NAV_Q,
              "v=spf1 include:_spf.google.com"):
        assert s.lower() in company.lower(), s
    assert "We started in Pune" not in company  # the blocked page's body is never shown
    assert "### careers\nNo artifact row" in company


def test_teach_examples_in_and_no_held_out(conn, cache, extracted, split) -> None:
    prompt, _ = J.build_prompt(conn, cache, QID, J.teach_examples(conn))
    examples = prompt[prompt.index(PARTS[4]):prompt.index(PARTS[5])]
    assert examples.count("### Example") == 5
    assert TEACH_QUOTE in examples
    assert "abstain_reason: contradicted: G1; segments" in examples
    for held in (HELD_QUOTE, "fernlow-example", "gantry-example", "Fernlow", "Gantry"):
        assert held not in prompt
    assert not G.held_out_quote_in(conn, QID, prompt)
    assert "company_id" not in examples  # the examples carry the call and evidence only


def test_a_teach_company_never_sees_its_own_example(conn, cache, split) -> None:
    block = J.examples_block(G.teach_export(conn), "arvel-example")
    assert block.count("### Example") == 4 and TEACH_QUOTE not in block


def test_judge_refuses_without_the_split_unless_no_examples(conn, cache, extracted,
                                                            db_file, monkeypatch) -> None:
    with pytest.raises(J.JudgeError, match="--no-examples"):
        J.teach_examples(conn)
    claude, _ = install(monkeypatch)
    r = CliRunner().invoke(main, ["judge", "--db", str(db_file), "--company", QID])
    assert r.exit_code == 1 and "refused" in r.output and "--no-examples" in r.output
    assert claude.calls == 0


def test_cut_keeps_original_characters_and_marks_the_cut() -> None:
    text = "\n".join(f"word{i}  next{i}" for i in range(500))  # 1,000 words
    out = J.cut(text, 600, 100_000)
    assert out.endswith("[cut: 600 of 1000 words shown]")
    assert out.split("\n[cut")[0] == text[:len(out.split("\n[cut")[0])]  # verbatim prefix
    assert "word299  next299" in out and "word300" not in out
    short = J.cut(text, 600, 50)
    assert len(short.split("\n[cut")[0]) <= 50 and "words shown]" in short
    assert J.cut("a b c", 600, 4000) == "a b c"


def test_job_post_is_kept_whole_and_other_artifacts_cut(conn, cache, extracted,
                                                        monkeypatch) -> None:
    monkeypatch.setattr(J, "TEXT_WORDS", 5)
    block = J.company_block(conn, cache, QID)
    job = block[block.index("### job_post"):block.index("### ats_board")]
    assert JOB_Q in job and "[cut" not in job.split("Nav and footer")[0]
    pricing = block[block.index("### pricing"):block.index("### product")]
    assert "[cut: 5 of" in pricing


# -- contacts ----------------------------------------------------------------------------


def test_contacts_are_masked_before_the_call(conn, cache, extracted, split,
                                             monkeypatch) -> None:
    with conn:  # an invented recipient whose title is the signal role on the page
        db.insert(conn, Contact(company_id=QID, name="Ishaan Varkeya", title=ROLE,
                                email="ishaan@quorvane.example"))
    claude, _ = install(monkeypatch, GAP)
    r = J.judge_company(conn, cache, QID, examples=J.teach_examples(conn))
    assert r.outcome == "diagnosed" and r.masked >= 2
    (sent,) = claude.prompts
    assert ROLE.lower() not in sent.lower() and J.MASK in sent
    assert "Founding Sales Development" not in sent


def test_gateway_still_refuses_an_unmasked_contact(conn, cache, extracted, split,
                                                   monkeypatch) -> None:
    with conn:
        db.insert(conn, Contact(company_id=QID, name="Ishaan Varkeya", title=ROLE))
    claude, _ = install(monkeypatch, GAP)
    monkeypatch.setattr(J, "mask_contacts", lambda conn, text: (text, 0))  # mask broken
    r = J.judge_company(conn, cache, QID, examples=J.teach_examples(conn))
    assert r.outcome == "refused" and claude.calls == 0
    assert diag_row(conn) is None and status(conn)[0] == "extracted"


# -- Step 47: the judge ------------------------------------------------------------------


def test_schema_contract() -> None:
    s = J.schema()
    jsonschema.validators.validator_for(s).check_schema(s)
    jsonschema.validate(GAP, s)
    jsonschema.validate(CONTRADICTED, s)
    assert "attempted" in s["required"]
    assert s["properties"]["gap_type"]["enum"] == ["G1", "G2", "G3", "G4", "G5", "G6", None]
    for bad in ({k: v for k, v in GAP.items() if k != "attempted"},
                dict(GAP, gap_type="G7"), dict(GAP, attempted="yes"),
                dict(GAP, claims=[{"text": "x", "artifact_kind": "pricing", "found": True}])):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, s)
    # it travels as a --json-schema argument, possibly through claude.cmd
    assert not claude_headless._CMD_UNSAFE.search(claude_headless.schema_text(s))


def test_gap_diagnosis_is_stored_and_moves_to_diagnosed(conn, cache, extracted, split,
                                                        monkeypatch) -> None:
    claude, gemini = install(monkeypatch, GAP)
    r = J.judge_company(conn, cache, QID, examples=J.teach_examples(conn))
    assert (r.outcome, r.calls, claude.calls, gemini.calls) == ("diagnosed", 1, 1, 0)
    row = diag_row(conn)
    assert (row["attempted"], row["gap_type"], row["confidence"]) == (1, "G3", 0.9)
    assert json.loads(row["secondary"]) == ["G5"]  # its own gap is dropped from secondary
    claims = json.loads(row["claims"])
    assert [c["artifact_kind"] for c in claims] == ["job_post", "pricing", "product"]
    assert json.loads(row["faithfulness"]) == {"checked": 3, "passed": 3, "near_miss": 0,
                                               "failed": [], "converted": False}
    assert status(conn) == ("diagnosed", None, None)
    call = conn.execute("SELECT stage, company_id, prompt_version, provider FROM llm_calls"
                        ).fetchone()
    assert tuple(call) == ("judge", QID, "j-1.0", "claude")


def test_rerun_and_cache_hit_make_zero_calls(conn, cache, extracted, split,
                                             monkeypatch) -> None:
    install(monkeypatch, GAP)
    examples = J.teach_examples(conn)
    J.judge_company(conn, cache, QID, examples=examples)
    claude, gemini = install(monkeypatch)  # no replies: any call fails
    r = J.judge_company(conn, cache, QID, examples=examples)
    assert r.outcome == "existing" and claude.calls == gemini.calls == 0
    with conn:  # the row is gone; the gateway's cache still answers
        conn.execute("DELETE FROM diagnoses")
    r = J.judge_company(conn, cache, QID, examples=examples)
    assert (r.outcome, r.calls, claude.calls) == ("diagnosed", 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1
    assert diag_row(conn)["gap_type"] == "G3"


def test_contradicted_abstention_drops_as_no_gap(conn, cache, extracted, split,
                                                 monkeypatch) -> None:
    install(monkeypatch, CONTRADICTED)
    r = J.judge_company(conn, cache, QID, examples=J.teach_examples(conn))
    assert r.outcome == "abstained" and r.detail.startswith("contradicted:")
    row = diag_row(conn)
    assert (row["attempted"], row["gap_type"]) == (0, None)
    assert row["abstain_reason"].startswith("contradicted: G1")
    assert len(json.loads(row["claims"])) == 1
    assert status(conn) == ("dropped", "no_gap", "judge")


def test_low_confidence_gap_becomes_a_thin_abstention(conn, cache, extracted, split,
                                                      monkeypatch) -> None:
    install(monkeypatch, dict(GAP, confidence=0.5))
    r = J.judge_company(conn, cache, QID, examples=J.teach_examples(conn))
    row = diag_row(conn)
    assert r.outcome == "abstained" and row["attempted"] == 0
    assert row["abstain_reason"].startswith("thin: confidence 0.5")
    assert status(conn)[0] == "dropped"


def test_missing_structured_output_is_scoring_failed(conn, cache, extracted, split,
                                                     monkeypatch) -> None:
    none = ProviderResult(output=None, model="opus", problem="no structured_output")
    claude, _ = install(monkeypatch, none, none)
    r = J.judge_company(conn, cache, QID, examples=J.teach_examples(conn))
    assert (r.outcome, claude.calls) == ("scoring_failed", 2)
    assert diag_row(conn) is None and status(conn)[0] == "extracted"


@pytest.mark.parametrize("output, why", [
    (dict(GAP, gap_type=None), "gap_type is null"),
    (dict(CONTRADICTED, gap_type="G1"), "gap_type is set"),
    (dict(CONTRADICTED, abstain_reason="maybe: unclear"), "must start with"),
    (dict(GAP, need=None), "need"),
])
def test_contract_breaking_output_is_scoring_failed(conn, cache, extracted, split,
                                                    monkeypatch, output, why) -> None:
    install(monkeypatch, output)
    r = J.judge_company(conn, cache, QID, examples=J.teach_examples(conn))
    assert r.outcome == "scoring_failed" and why in r.detail
    assert diag_row(conn) is None and status(conn)[0] == "extracted"


def test_hand_company_past_diagnosed_keeps_its_status(conn, cache, extracted, split,
                                                      monkeypatch) -> None:
    with conn:
        conn.execute("UPDATE companies SET status = 'sent', hand = 1 WHERE company_id = ?",
                     (QID,))
    install(monkeypatch, CONTRADICTED)
    r = J.judge_company(conn, cache, QID, examples=J.teach_examples(conn))
    assert r.outcome == "abstained" and status(conn) == ("sent", None, None)


def test_no_examples_run_is_keyed_apart_and_moves_nothing(conn, cache, extracted,
                                                          monkeypatch) -> None:
    claude, _ = install(monkeypatch, CONTRADICTED)
    r = J.judge_company(conn, cache, QID, examples=None)
    assert r.outcome == "abstained" and r.status == ""
    assert diag_row(conn, version="j-1.0+noex") is not None and diag_row(conn) is None
    assert status(conn)[0] == "extracted"
    assert "No worked examples" in claude.prompts[0]


def test_select(conn, cache, extracted) -> None:
    assert J.select(conn) == [BID, QID]
    assert J.select(conn, limit=1) == [BID]
    with conn:
        conn.execute("UPDATE companies SET status = 'dropped', drop_reason = 'no_gap', "
                     "drop_stage = 'judge' WHERE company_id = ?", (BID,))
    assert J.select(conn, pilot=True) == [QID]
    for cid in ("arvel-example", "nobody-example"):
        if cid == "arvel-example":
            from tests.conftest import add_company
            add_company(conn, cid)
        with pytest.raises(J.JudgeError):
            J.select(conn, company_id=cid)


def test_cli_judge_run_and_dry_run(conn, db_file, cache, extracted, split,
                                   monkeypatch) -> None:
    claude, gemini = install(monkeypatch)  # dry run: any call would fail
    base = ["judge", "--db", str(db_file), "--cache-dir", str(cache.root)]
    r = CliRunner().invoke(main, base + ["--dry-run", "--pilot"])
    assert r.exit_code == 0, r.output
    assert "projected calls 2" in r.output and "would call" in r.output
    assert re.search(rf"{QID}: prompt \d+ chars", r.output)
    assert claude.calls == gemini.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0] == 0

    claude, _ = install(monkeypatch, GAP)
    r = CliRunner().invoke(main, base + ["--company", QID])
    assert r.exit_code == 0, r.output
    assert f"{QID}: diagnosed: G3; claims 3/3 passed; extracted -> diagnosed" in r.output
    assert "provider calls 1" in r.output
    r = CliRunner().invoke(main, base + ["--dry-run", "--company", QID])
    assert "no call: diagnosed already" in r.output and "projected calls 0" in r.output


def test_cli_dry_run_without_split_warns(db_file, cache, extracted, monkeypatch) -> None:
    claude, _ = install(monkeypatch)
    r = CliRunner().invoke(main, ["judge", "--db", str(db_file), "--cache-dir",
                                  str(cache.root), "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "a real run would refuse" in r.output and "dry run j-1.0+noex" in r.output
    assert claude.calls == 0


# -- Step 48: faithfulness ---------------------------------------------------------------


@pytest.fixture
def arts() -> dict[str, Art]:
    a = dict(quorvane().arts)
    a["about"] = Art("about", ABOUT, "blocked")
    del a["careers"]  # no artifacts row for this kind
    a["contact"] = art("contact", "<main><p>We’re the clinic’s first call, "
                                  "“every shift”.</p></main>".encode("utf-8"),
                       f"https://{Q_DOMAIN}/contact")
    return a


def check(arts: dict[str, Art], *claims: dict) -> tuple[list[Claim], Any]:
    return F.check_claims(list(claims), arts)


@pytest.mark.parametrize("kind, url, quote", [
    ("pricing", PRICING, PRICING_Q),  # exact
    ("pricing", PRICING, "talk  TO our\nteam for a CUSTOM quote"),  # whitespace and case
    ("pricing", PRICING, "Enterprise Talk to our team"),  # across a line break
    ("contact", f"https://{Q_DOMAIN}/contact", "We're the clinic's first call, \"every shift\""),
    ("homepage", HOME, FOOTER_Q),  # footer text (removed_text)
    ("homepage", HOME, NAV_Q),  # nav text
    ("homepage", HOME, "/static/app.js"),  # a string in the structure
    ("job_post", JOB, JOB_Q),
])
def test_found_quotes_that_pass(arts, kind, url, quote) -> None:
    kept, f = check(arts, claim(kind, url, quote))
    assert (f.checked, f.passed, f.failed) == (1, 1, []) and kept[0].quote == quote


def test_near_miss_fails_and_is_logged_apart_from_a_miss(arts) -> None:
    near = PRICING_Q.replace("custom", "custum")
    kept, f = check(arts, claim("pricing", PRICING, near),
                    claim("pricing", PRICING, "We never invented this sentence anywhere"))
    assert kept == [] and (f.checked, f.passed, f.near_miss) == (2, 0, 1)
    assert [x.category for x in f.failed] == ["near_miss", "miss"]
    assert f.failed[0].score >= F.NEAR_MISS > f.failed[1].score
    assert f.failed[0].quote == near and f.failed[0].index == 0


@pytest.mark.parametrize("raw, category", [
    (claim("pricing", HOME, PRICING_Q), "wrong_url"),  # right quote, other artifact's URL
    (claim("pricing", PRICING + "/", PRICING_Q), "wrong_url"),
    (claim("job_post", JOB, PRICING_Q), "miss"),  # right text, wrong artifact
    (claim("about", ABOUT, "We started in Pune."), "unseen"),  # blocked
    (claim("careers", f"https://{Q_DOMAIN}/careers", "Join us."), "unseen"),  # no such row
    (claim("about", ABOUT, None), "not_found_invalid"),  # blocked is unseen, not absent
    (claim("pricing", PRICING, None), "not_found_invalid"),  # ok page: not absent
    (claim("product", "https://elsewhere.example/", None), "wrong_url"),
    ({"text": "x", "artifact_kind": "pricing", "found": True, "url": PRICING, "quote": None},
     "malformed"),
    ({"text": "x", "artifact_kind": "product", "found": False, "url": PRODUCT, "quote": "q"},
     "malformed"),
])
def test_claims_that_fail(arts, raw, category) -> None:
    kept, f = check(arts, raw)
    assert kept == [] and [x.category for x in f.failed] == [category]


@pytest.mark.parametrize("url", [PRODUCT, HOME, f"https://{Q_DOMAIN}/sitemap.xml"])
def test_not_found_claim_passes_on_a_not_found_outcome(arts, url) -> None:
    kept, f = check(arts, claim("product", url, None))
    assert f.passed == 1 and kept[0].found is False and kept[0].quote is None


def _diag(**kw) -> Diagnosis:
    base = dict(company_id=QID, prompt_version="j-1.0", attempted=True, gap_type="G3",
                confidence=0.9, need="n", cannot_see="c", secondary=["G5"])
    base.update(kw)
    return Diagnosis(**base)


def test_diagnosis_with_two_kinds_and_a_found_claim_stands(arts) -> None:
    d = F.apply(_diag(), [claim("job_post", JOB, JOB_Q), claim("product", PRODUCT, None),
                          claim("pricing", PRICING, "invented words here and there")], arts)
    assert d.attempted and d.gap_type == "G3" and len(d.claims) == 2
    assert (d.faithfulness.passed, d.faithfulness.converted) == (2, False)


@pytest.mark.parametrize("claims, why", [
    ([claim("pricing", PRICING, PRICING_Q), claim("pricing", PRICING, "Simple pricing")],
     "from 1 artifact kind"),
    ([claim("job_post", JOB, JOB_Q), claim("pricing", HOME, PRICING_Q)], "from 1 artifact kind"),
    ([claim("product", PRODUCT, None), claim("compare", f"https://{Q_DOMAIN}/compare", "x")],
     "from 1 artifact kind"),
    ([claim("product", PRODUCT, None), claim("about", ABOUT, None)], "from 1 artifact kind"),
    ([], "0 passing claim"),
])
def test_too_little_evidence_becomes_an_abstention(arts, claims, why) -> None:
    d = F.apply(_diag(), claims, arts)
    assert d.attempted is False and d.gap_type is None
    assert d.abstain_reason.startswith("thin: faithfulness") and why in d.abstain_reason
    assert "the judge named G3" in d.abstain_reason and d.faithfulness.converted


def test_absence_alone_never_names_a_gap() -> None:
    a = {"product": Art("product", PRODUCT, "not_found"),
         "customers": Art("customers", f"https://{Q_DOMAIN}/customers", "not_found")}
    d = F.apply(_diag(), [claim("product", PRODUCT, None),
                          claim("customers", f"https://{Q_DOMAIN}/customers", None)], a)
    assert d.attempted is False and "no passing found claim" in d.abstain_reason


def test_abstention_keeps_its_reason_and_loses_failed_claims(arts) -> None:
    d = F.apply(_diag(attempted=False, gap_type=None, abstain_reason="no_evidence: none"),
                [claim("homepage", HOME, "made up")], arts)
    assert d.abstain_reason == "no_evidence: none" and d.claims == []
    assert not d.faithfulness.converted and d.faithfulness.failed[0].category == "miss"


def test_judge_drops_after_faithfulness_turns_a_gap_into_an_abstention(
        conn, cache, extracted, split, monkeypatch) -> None:
    bad = dict(GAP, claims=[claim("job_post", JOB, JOB_Q),
                            claim("pricing", PRICING, PRICING_Q.replace("custom", "custum"))])
    install(monkeypatch, bad)
    r = J.judge_company(conn, cache, QID, examples=J.teach_examples(conn))
    assert r.outcome == "abstained"
    row = diag_row(conn)
    f = json.loads(row["faithfulness"])
    assert (f["checked"], f["passed"], f["near_miss"], f["converted"]) == (2, 1, 1, True)
    assert f["failed"][0]["category"] == "near_miss"
    assert [c["artifact_kind"] for c in json.loads(row["claims"])] == ["job_post"]
    assert row["attempted"] == 0 and row["abstain_reason"].startswith("thin: faithfulness")
    assert status(conn) == ("dropped", "no_gap", "judge")


def test_cli_faithfulness_rerun_reads_the_original_output(conn, db_file, cache, extracted,
                                                          split, monkeypatch) -> None:
    install(monkeypatch, GAP)
    J.judge_company(conn, cache, QID, examples=J.teach_examples(conn))
    base = ["judge", "faithfulness", "--db", str(db_file), "--cache-dir", str(cache.root),
            "--company", QID]
    r = CliRunner().invoke(main, base)
    assert r.exit_code == 0, r.output
    assert "claims 3/3 passed" in r.output and status(conn)[0] == "diagnosed"
    with conn:  # the pricing page now lives elsewhere: its claim's url no longer matches
        conn.execute("UPDATE artifacts SET url = ? WHERE company_id = ? AND kind = 'pricing'",
                     (PRICING + "-new", QID))
    r = CliRunner().invoke(main, base + ["--dry-run"])
    assert "dry run:" in r.output and "claims 2/3 passed" in r.output
    assert diag_row(conn)["attempted"] == 1  # dry run wrote nothing
    with conn:  # and the job post is gone: one kind is left, the gap cannot stand
        conn.execute("UPDATE artifacts SET outcome = 'blocked' WHERE company_id = ? AND "
                     "kind = 'job_post'", (QID,))
    r = CliRunner().invoke(main, base)
    assert r.exit_code == 0, r.output
    assert "abstained: thin: faithfulness" in r.output and "wrong_url" in r.output
    row = diag_row(conn)
    assert row["attempted"] == 0 and json.loads(row["faithfulness"])["converted"] is True
    assert [c["artifact_kind"] for c in json.loads(row["claims"])] == ["product"]
    assert status(conn) == ("dropped", "no_gap", "judge")
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1  # no new call

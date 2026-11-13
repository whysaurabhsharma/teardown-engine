"""Step 49: the verifier.

Invented companies only (Quorvane and Brindlecart from tests/test_extract_facts.py,
the invented gold set from tests/test_gold.py). No real model call: claude,
gemini and groq in gateway.PROVIDERS are scripted fakes, and sockets are blocked.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from click.testing import CliRunner

from engine import db
from engine.cli import main
from engine.extract.base import Art
from engine.judge import faithfulness as F
from engine.judge import judge as J
from engine.judge import verify as V
from engine.llm import gateway, routes
from engine.llm.base import QuotaError
from engine.models import Claim, Contact
from tests.test_extract_facts import QID, bundles, cache  # noqa: F401 (fixtures)
from tests.test_judge import (  # noqa: F401 (fixtures)
    GAP,
    JOB,
    JOB_Q,
    PRICING_Q,
    PRODUCT,
    extracted,
    no_net,
    split,
    status,
)
from tests.test_llm_gateway import Fake

SUP = {"verdict": "supported", "reason": "The quote says it."}
NOT = {"verdict": "not_supported", "reason": "The quote is about something else."}
UNSURE = {"verdict": "unsure", "reason": "Cannot tell."}


def providers(monkeypatch: pytest.MonkeyPatch, *, claude=(), gemini=(), groq=()) -> dict:
    fakes = {"claude": Fake(*claude), "gemini": Fake(*gemini),
             "groq": Fake(*groq, model="llama-3.3-70b-versatile")}
    for name, f in fakes.items():
        monkeypatch.setitem(gateway.PROVIDERS, name, f)
    return fakes


@pytest.fixture
def judged(conn, cache, extracted, split, monkeypatch) -> None:
    """Quorvane diagnosed G3 by the claude judge: three surviving claims."""
    providers(monkeypatch, claude=[GAP])
    r = J.judge_company(conn, cache, QID, examples=J.teach_examples(conn))
    assert r.outcome == "diagnosed" and status(conn)[0] == "diagnosed"


def verdict(conn: sqlite3.Connection) -> dict:
    return json.loads(J.stored(conn, QID, J.PROMPT_VERSION)["verifier_verdict"])


def verify_calls(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM llm_calls WHERE stage = 'verify' ORDER BY call_id"
                        ).fetchall()


# -- family ------------------------------------------------------------------------------


def test_claude_writer_is_verified_by_gemini(conn, cache, judged, monkeypatch) -> None:
    f = providers(monkeypatch, gemini=[SUP, SUP, SUP])
    r = V.verify_company(conn, cache, QID)
    assert (r.outcome, r.calls) == ("pass", 3)
    assert (f["gemini"].calls, f["groq"].calls, f["claude"].calls) == (3, 0, 0)
    v = verdict(conn)
    assert (v["writer"], v["writer_family"], v["outcome"], v["not_supported"]) == (
        "claude", "anthropic", "pass", 0)
    assert {c["family"] for c in v["claims"]} == {"google"}
    keys = [r["prompt_version"] for r in verify_calls(conn)]
    assert keys == [V.call_version(v["key"], i) for i in range(3)]
    assert all(k.startswith("v-1.0:") for k in keys)
    assert status(conn) == ("diagnosed", None, None)


def test_gemini_writer_excludes_gemini_and_uses_groq(conn, cache, extracted, split,
                                                     monkeypatch) -> None:
    providers(monkeypatch, claude=[QuotaError("over quota")], gemini=[GAP])
    r = J.judge_company(conn, cache, QID, examples=J.teach_examples(conn))
    assert r.outcome == "diagnosed"
    f = providers(monkeypatch, groq=[SUP, UNSURE, SUP])  # gemini has no reply: a call fails
    r = V.verify_company(conn, cache, QID)
    assert r.outcome == "pass" and f["gemini"].calls == 0 and f["groq"].calls == 3
    v = verdict(conn)
    assert v["writer_family"] == "google" and {c["family"] for c in v["claims"]} == {"meta"}


def test_refuses_when_no_target_of_another_family(conn, cache, extracted, split,
                                                  monkeypatch) -> None:
    providers(monkeypatch, claude=[QuotaError("over quota")], gemini=[GAP])
    J.judge_company(conn, cache, QID, examples=J.teach_examples(conn))
    only_gemini = routes.Route("verifier", routes.Target("gemini", "gemini-2.5-pro"))
    monkeypatch.setitem(routes.ROUTES, "verifier", only_gemini)
    f = providers(monkeypatch)
    r = V.verify_company(conn, cache, QID)
    assert r.outcome == "refused" and "google" in r.detail
    assert f["gemini"].calls == f["groq"].calls == 0
    assert J.stored(conn, QID, J.PROMPT_VERSION)["verifier_verdict"] is None


def test_refuses_without_a_judge_call_on_record(conn, cache, judged, monkeypatch) -> None:
    with conn:
        conn.execute("DELETE FROM llm_calls")
    f = providers(monkeypatch)
    r = V.verify_company(conn, cache, QID)
    assert r.outcome == "refused" and "no judge call on record" in r.detail
    assert f["gemini"].calls == 0


def test_unknown_writer_family_is_refused() -> None:
    with pytest.raises(V.VerifyError, match="cannot prove"):
        V.allowed_targets("unknown:mystery:model")
    assert [t.provider for t in V.allowed_targets("anthropic")] == ["gemini", "groq"]
    assert [t.provider for t in V.allowed_targets("google")] == ["groq"]


def test_a_verdict_from_the_writers_family_is_not_used(conn, cache, judged,
                                                       monkeypatch) -> None:
    # a broken route that puts claude on the verifier: the gateway excludes it,
    # and if a same-family verdict were ever on record it is refused
    monkeypatch.setattr(V, "answered_by", lambda *a: routes.Target("claude", "opus"))
    providers(monkeypatch, gemini=[SUP])
    r = V.verify_company(conn, cache, QID)
    assert r.outcome == "refused" and "another family" in r.detail
    assert J.stored(conn, QID, J.PROMPT_VERSION)["verifier_verdict"] is None


def test_a_verdict_from_an_unknown_family_is_not_used(conn, cache, extracted, split,
                                                      monkeypatch) -> None:
    providers(monkeypatch, claude=[QuotaError("over quota")], gemini=[GAP])
    J.judge_company(conn, cache, QID, examples=J.teach_examples(conn))
    f = providers(monkeypatch, groq=[SUP])
    f["groq"].model = "mystery-model"  # the provider reports a model no family is known for
    r = V.verify_company(conn, cache, QID)
    assert r.outcome == "refused" and "another family" in r.detail


# -- what the verifier sees --------------------------------------------------------------


def test_context_is_300_characters_around_the_quote(conn, cache, extracted) -> None:
    arts = F.load_arts(conn, cache, QID)
    text = arts["job_post"].text
    assert len(text) > 400
    ev = V.evidence(Claim(text="t", artifact_kind="job_post", found=True, url=JOB,
                          quote=JOB_Q), arts)
    assert ev.where == "text" and len(ev.context) == V.VERIFY_CONTEXT
    start = text.index(JOB_Q)
    before, after = V.window(text, start, start + len(JOB_Q))
    assert before + after == ev.context and len(before) + len(after) == 300
    assert f"[[{JOB_Q}]]" in ev.text and f'"{JOB_Q}"' in ev.text


def test_window_shifts_at_the_edges() -> None:
    text = "a" * 50 + "QUOTE" + "b" * 1000
    before, after = V.window(text, 50, 55)
    assert (len(before), len(after)) == (50, 250)
    before, after = V.window("xy" + "QUOTE" + "z" * 10, 2, 7)
    assert (before, after) == ("xy", "z" * 10)  # short text: all of it


def test_locate_matches_after_normalisation() -> None:
    text = "Hello  World,\nwe’re the “first”\n call."
    s, e = V.locate(text, "we're the \"FIRST\" call")
    assert text[s:e] == "we’re the “first”\n call"
    assert V.locate(text, "absent words") is None


def test_not_found_claim_gets_the_outcome_and_where_looked(conn, cache, extracted) -> None:
    arts = F.load_arts(conn, cache, QID)
    ev = V.evidence(Claim(text="No product page.", artifact_kind="product", found=False,
                          url=PRODUCT), arts)
    assert ev.where == "not_found" and "not_found" in ev.text and PRODUCT in ev.text


def test_nav_text_and_structure_quotes(conn, cache, extracted) -> None:
    arts = F.load_arts(conn, cache, QID)
    home = arts["homepage"].url
    ev = V.evidence(Claim(text="t", artifact_kind="homepage", found=True, url=home,
                          quote="Dental chains"), arts)
    assert ev.where == "removed_text"
    ev = V.evidence(Claim(text="t", artifact_kind="homepage", found=True, url=home,
                          quote="/static/app.js"), arts)
    assert ev.where == "structure" and "/static/app.js" in ev.text


def test_gap_definition_comes_from_gaps_md() -> None:
    d = V.gap_definition("G3")
    assert "Tooling and process around them" in d and "Abstain when:" in d
    assert "An operations role is open" in d
    assert "G6" in V.gap_definition("G6").split("\n")[0]
    with pytest.raises(V.VerifyError):
        V.gap_definition("G9")


def test_prompt_holds_quote_context_and_gap(conn, cache, judged, monkeypatch) -> None:
    f = providers(monkeypatch, gemini=[SUP, SUP, SUP])
    V.verify_company(conn, cache, QID)
    job, pricing, product = f["gemini"].prompts
    assert JOB_Q in job and "Tooling and process around them" in job
    assert PRICING_Q in pricing and "the page text" in pricing
    assert "Capture outcome for the product page: not_found" in product


def test_contacts_are_masked_before_the_call(conn, cache, judged, monkeypatch) -> None:
    with conn:  # an invented recipient whose title appears in the quoted job post
        db.insert(conn, Contact(company_id=QID, name="Ishaan Varkeya", title="first sales hire",
                                email="ishaan@quorvane.example"))
    f = providers(monkeypatch, gemini=[SUP, SUP, SUP])
    r = V.verify_company(conn, cache, QID)
    assert r.outcome == "pass" and r.masked >= 1
    assert all("first sales hire" not in p.lower() for p in f["gemini"].prompts)
    assert J.MASK in f["gemini"].prompts[0]


# -- outcomes ----------------------------------------------------------------------------


def test_two_not_supported_turn_the_diagnosis_into_an_abstention(conn, cache, judged,
                                                                 monkeypatch) -> None:
    providers(monkeypatch, gemini=[NOT, SUP, NOT])
    r = V.verify_company(conn, cache, QID)
    assert r.outcome == "abstained"
    row = J.stored(conn, QID, J.PROMPT_VERSION)
    assert (row["attempted"], row["gap_type"]) == (0, None)
    assert row["abstain_reason"] == ("thin: verifier: 2 of 3 claims not_supported; the judge "
                                     "named G3")
    assert len(json.loads(row["claims"])) == 3  # claims are kept for the audit
    v = verdict(conn)
    assert (v["outcome"], v["not_supported"], v["gap_type"]) == ("abstained", 2, "G3")
    assert [c["verdict"] for c in v["claims"]] == ["not_supported", "supported",
                                                   "not_supported"]
    assert status(conn) == ("dropped", "no_gap", "judge")


def test_one_not_supported_is_recorded_for_the_quality_gate(conn, cache, judged,
                                                            monkeypatch) -> None:
    providers(monkeypatch, gemini=[SUP, NOT, UNSURE])
    r = V.verify_company(conn, cache, QID)
    assert r.outcome == "review" and "quality gate" in r.detail
    row = J.stored(conn, QID, J.PROMPT_VERSION)
    assert (row["attempted"], row["gap_type"]) == (1, "G3")
    v = verdict(conn)
    assert (v["outcome"], v["not_supported"]) == ("review", 1)
    assert status(conn) == ("diagnosed", None, None)


def test_rerun_and_cache_hit_make_zero_calls(conn, cache, judged, monkeypatch) -> None:
    providers(monkeypatch, gemini=[SUP, NOT, NOT])
    V.verify_company(conn, cache, QID)
    f = providers(monkeypatch)  # no replies: any call fails
    r = V.verify_company(conn, cache, QID)
    assert (r.outcome, r.calls) == ("existing", 0)
    with conn:  # the verdict is gone and the row restored; the gateway's cache answers
        conn.execute("UPDATE diagnoses SET verifier_verdict = NULL, attempted = 1, "
                     "gap_type = 'G3', abstain_reason = NULL")
    r = V.verify_company(conn, cache, QID)
    assert (r.outcome, r.calls) == ("abstained", 0)
    assert f["gemini"].calls == f["groq"].calls == 0
    assert len(verify_calls(conn)) == 3


def test_a_faithfulness_rerun_does_not_undo_a_verifier_abstention(conn, cache, judged,
                                                                  monkeypatch) -> None:
    providers(monkeypatch, gemini=[NOT, NOT, SUP])
    V.verify_company(conn, cache, QID)
    J.refaithful(conn, cache, QID)  # rebuilds the diagnosis from the judge's output
    assert J.stored(conn, QID, J.PROMPT_VERSION)["attempted"] == 1
    providers(monkeypatch)
    r = V.verify_company(conn, cache, QID)
    assert r.outcome == "existing"
    row = J.stored(conn, QID, J.PROMPT_VERSION)
    assert row["attempted"] == 0 and row["abstain_reason"].startswith("thin: verifier")


def test_changed_claims_get_a_new_key(conn, cache, judged) -> None:
    row = J.stored(conn, QID, J.PROMPT_VERSION)
    k1 = V.diagnosis_key(J.PROMPT_VERSION, row["claims"])
    claims = json.loads(row["claims"])[:2]
    assert V.diagnosis_key(J.PROMPT_VERSION, json.dumps(claims)) != k1
    assert V.diagnosis_key("j-1.1", row["claims"]) != k1


def test_select(conn, cache, judged) -> None:
    assert V.select(conn, prompt_version=J.PROMPT_VERSION) == [QID]
    assert V.select(conn, prompt_version="j-9.9") == []


# -- CLI ---------------------------------------------------------------------------------


def test_cli_verify_dry_run_and_run(conn, db_file, cache, judged, monkeypatch) -> None:
    f = providers(monkeypatch)
    base = ["judge", "verify", "--db", str(db_file), "--cache-dir", str(cache.root)]
    r = CliRunner().invoke(main, base + ["--dry-run"])
    assert r.exit_code == 0, r.output
    assert "dry run: " in r.output and "writer claude (anthropic)" in r.output
    assert f["gemini"].calls == 0 and not verify_calls(conn)
    f = providers(monkeypatch, gemini=[SUP, NOT, SUP])
    r = CliRunner().invoke(main, base + ["--company", QID])
    assert r.exit_code == 0, r.output
    assert f"{QID}: review: not_supported 1 of 3" in r.output
    assert "claim 1 [pricing]: not_supported (gemini, google)" in r.output
    assert "provider calls 3" in r.output
    r = CliRunner().invoke(main, base + ["--pilot"])
    assert r.exit_code == 0 and "existing" in r.output and "provider calls 0" in r.output


def test_cli_verify_refused_exits_1(conn, db_file, cache, judged, monkeypatch) -> None:
    with conn:
        conn.execute("DELETE FROM llm_calls")
    providers(monkeypatch)
    r = CliRunner().invoke(main, ["judge", "verify", "--db", str(db_file), "--cache-dir",
                                  str(cache.root)])
    assert r.exit_code == 1 and "refused" in r.output


def test_art_without_quote_is_refused() -> None:
    arts = {"pricing": Art("pricing", "https://x.example/pricing", "ok", "Other words.", {})}
    with pytest.raises(V.VerifyError, match="faithfulness"):
        V.evidence(Claim(text="t", artifact_kind="pricing", found=True,
                         url="https://x.example/pricing", quote=PRICING_Q), arts)

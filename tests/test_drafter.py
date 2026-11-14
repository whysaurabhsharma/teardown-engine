"""Step 64: the drafter.

Invented companies only: Quorvane, judged and verified through the scripted
fakes of tests/test_judge_verify.py, with an invented proof slice. The
"claude" provider is a scripted fake drafter; sockets are blocked; every path
(teardowns, sender, voice examples, database) is under tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from click.testing import CliRunner

from engine import db
from engine.build import drafter as DR
from engine.build import teardown as T
from engine.build.slices import common as SC
from engine.build.slices import store as SS
from engine.cli import main
from engine.judge import verify as V
from engine.llm import gateway
from engine.models import Contact
from tests.test_extract_facts import QID, bundles, cache  # noqa: F401 (fixtures)
from tests.test_gold import HELD_QUOTE
from tests.test_judge import extracted, no_net, split  # noqa: F401 (fixtures)
from tests.test_judge_verify import SUP, judged, providers  # noqa: F401 (fixtures)
from tests.test_teardown import write_sender

SLICE_URL = "https://brightsmile-dental.example/locations"
SLICE_QUOTE = "Twelve clinics across Pune and Nashik"

DRAFT: dict[str, Any] = {
    "section3": [
        {"sentence": "The Quorvane job post asks the first sales hire to set up HubSpot "
                     "properties and pipelines.", "claim_indices": [0]},
        {"sentence": "The Quorvane pricing page routes buyers to a custom quote, and no "
                     "product page is linked from the site.", "claim_indices": [1, 2]},
    ],
    "section4": ("Quorvane is hiring its founding SDR now, so the CRM and pipeline work lands "
                 "on that hire in week one."),
    "section5": ["A HubSpot pipeline with stages that match the demo form.",
                 "An account list of dental chains, scored by clinic count.",
                 "A weekly report of replies by segment."],
    "section7": ("This is an outside view from public pages. I cannot see whether a CRM is "
                 "already set up."),
    "email": {"observation": "That hire is asked to set up HubSpot pipelines while pricing "
                             "routes every buyer to a quote.",
              "observation_claims": [0, 1],
              "slice_name": "a list of dental chains",
              "subject_observation": "CRM before calls"},
}

VOICE = [
    {"variant": "adjacent", "subject": "{Company}'s {role} hire: pricing before demos",
     "body": "Hi {first_name},\n\nI saw {Company} is hiring. The pricing page asks for a "
             "call before any number.\n\nNotes and a list for you: {link}\n\nSaurabh\n"},
    {"variant": "candidate", "subject": "Applied for {role} at {Company}, with notes",
     "body": "Hi {first_name},\n\nI applied today. The careers page lists four sales roles "
             "and no operations role.\n\n{link}\n\nSaurabh\n"},
    {"variant": "adjacent", "subject": "{Company}'s {role} hire: one sending domain",
     "body": "Hi {first_name},\n\nYour records show one sending domain for both sides.\n\n"
             "{link}\n\nSaurabh\n"},
]


# -- fixtures ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_model_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.guard.budget import Budget

    monkeypatch.setattr(gateway, "_budget", Budget())


def store_slice(conn) -> None:
    sl = SC.Slice("icp_accounts", "icp-test", "Ten dental chains in Pune that match "
                  "Quorvane's customers page.", ["Account", "Pricing page"],
                  rows=[{"cells": ["Brightsmile Dental", "not found"], "url": SLICE_URL,
                         "quote": SLICE_QUOTE}])
    SS.write_slice(conn, QID, sl)
    SS.select(conn, QID, "icp_accounts")


@pytest.fixture
def inputs(conn, cache, judged, monkeypatch) -> None:
    """Quorvane diagnosed G3, every claim supported, adjacent, a slice selected."""
    providers(monkeypatch, gemini=[SUP, SUP, SUP])
    assert V.verify_company(conn, cache, QID).outcome == "pass"
    with conn:
        conn.execute("UPDATE signals SET offer_type = 'adjacent' WHERE company_id = ?", (QID,))
    store_slice(conn)


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    p = {"td": tmp_path / "td", "sender": tmp_path / "sender.yaml",
         "voice": tmp_path / "voice.yaml"}
    write_sender(p["sender"])
    write_voice(p["voice"], VOICE)
    return p


def write_voice(path: Path, examples: list[dict[str, Any]]) -> None:
    path.write_text(yaml.safe_dump({"version": DR.VOICE_VERSION, "examples": examples},
                                   sort_keys=False, allow_unicode=True), encoding="utf-8")


def drafter(monkeypatch, *replies):
    """The scripted drafter: claude answers; gemini (the fallback) must not be called."""
    return providers(monkeypatch, claude=replies)


def run(conn, paths, **kw) -> DR.Result:
    kw.setdefault("voice_file", paths["voice"])
    return DR.draft_company(conn, QID, directory=paths["td"], **kw)


def draft_calls(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM llm_calls WHERE stage = 'draft'").fetchone()[0]


def load(paths) -> dict[str, Any]:
    return yaml.safe_load(T.file_for(QID, paths["td"]).read_text(encoding="utf-8"))


def save(paths, data: dict[str, Any]) -> None:
    T.file_for(QID, paths["td"]).write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


@pytest.fixture
def drafted(conn, inputs, paths, monkeypatch) -> dict[str, Path]:
    f = drafter(monkeypatch, DRAFT)
    r = run(conn, paths)
    assert (r.outcome, r.calls, f["claude"].calls) == ("drafted", 1, 1), r.detail
    return paths


# -- the prompt and schema ---------------------------------------------------------------


def test_prompt_is_versioned_and_the_schema_takes_the_example() -> None:
    assert DR.template_version() == DR.PROMPT_VERSION == "d-1.0"
    assert "<!--" not in DR.template()
    for ph in ("{{spec}}", "{{limits}}", "{{banned}}", "{{offer}}", "{{voice}}", "{{company}}"):
        assert ph in DR.template()
    jsonschema.validate(DRAFT, DR.schema())
    bad = json.loads(json.dumps(DRAFT))
    bad["section5"] = bad["section5"][:2]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, DR.schema())
    bad = json.loads(json.dumps(DRAFT))
    bad["section3"][0]["claim_indices"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, DR.schema())


def test_prompt_holds_spec_banned_offer_voice_and_only_passing_claims(conn, inputs,
                                                                      paths) -> None:
    ctx = T.load_context(conn, QID)
    prompt, masked = DR.build_prompt(conn, ctx, DR.load_voice(paths["voice"]))
    assert masked == 0
    assert DR.spec_text() in prompt  # the spec, in full
    for p in T.BANNED_PHRASES:
        assert p in prompt
    assert '"leverage" as a verb' in prompt and "world-class" in prompt
    assert "Offer type: adjacent" in prompt and "{three-word observation}" in prompt
    for v in VOICE:
        assert v["body"].strip().splitlines()[2] in prompt
    assert "[0] job_post" in prompt and "[2] product" in prompt
    assert SLICE_QUOTE in prompt and "icp_accounts" in prompt
    assert HELD_QUOTE not in prompt  # never held-out gold content


def test_prompt_masks_contacts_and_never_holds_a_contact_value(conn, inputs, paths,
                                                               monkeypatch) -> None:
    with conn:  # a contact whose title happens to be on the slice
        db.insert(conn, Contact(company_id=QID, name="Mirela Dunstan",
                                title="Brightsmile Dental", email="mirela@quorvane.example"))
    f = drafter(monkeypatch, DRAFT)
    r = run(conn, paths)
    assert r.outcome == "drafted" and r.masked >= 1
    sent = f["claude"].prompts[0]
    for v in gateway.contact_values(conn):
        assert gateway.contact_pattern(v).search(sent) is None
    assert "Brightsmile Dental" not in sent and "[redacted]" in sent
    assert "Mirela" not in sent and "{first_name}" in sent  # the greeting stays a placeholder


def test_dropped_unverified_review_or_no_slice_is_refused(conn, inputs, paths,
                                                          monkeypatch) -> None:
    f = drafter(monkeypatch)  # no reply: any call fails the test
    SS.select(conn, QID, None)
    r = run(conn, paths)
    assert r.outcome == "refused" and "proof slice" in r.detail
    SS.select(conn, QID, "icp_accounts")
    v = V.VerifierVerdict.model_validate_json(
        conn.execute("SELECT verifier_verdict FROM diagnoses").fetchone()[0])
    v.claims[1].verdict, v.not_supported, v.outcome = "not_supported", 1, "review"
    with conn:
        conn.execute("UPDATE diagnoses SET verifier_verdict = ?", (v.model_dump_json(),))
    r = run(conn, paths)
    assert r.outcome == "refused" and "review" in r.detail
    assert f["claude"].calls == 0 and draft_calls(conn) == 0


def test_unknown_offer_type_is_refused(conn, inputs, paths, monkeypatch) -> None:
    f = drafter(monkeypatch)
    with conn:
        conn.execute("UPDATE signals SET offer_type = NULL WHERE company_id = ?", (QID,))
    r = run(conn, paths)
    assert r.outcome == "refused" and "offer type" in r.detail and f["claude"].calls == 0


# -- the file ------------------------------------------------------------------------------


def test_draft_writes_schema_valid_sections_into_the_file(conn, drafted) -> None:
    p = T.file_for(QID, drafted["td"])
    text = p.read_text(encoding="utf-8")
    _, doc = T.load(p)
    assert [s.text for s in doc.need.sentences] == [x["sentence"] for x in DRAFT["section3"]]
    assert [s.claims for s in doc.need.sentences] == [[0], [1, 2]]
    assert doc.need.evidence == [0, 1, 2]
    assert doc.why_now == DRAFT["section4"] and doc.build == DRAFT["section5"]
    assert doc.might_have_wrong == DRAFT["section7"]
    assert doc.email is not None and doc.email.claims == [0, 1]
    assert doc.email.subject == "CRM before calls" and doc.email.slice == "a list of dental chains"
    assert doc.drafted is not None and doc.drafted.by == "d-1.0"
    assert doc.drafted.key.startswith("d-1.0:") and set(doc.drafted.sections) == set(DR.SECTIONS)
    assert doc.slice.rows[0].url == SLICE_URL  # section 6 from the stored slice
    assert "# The diagnosis claims you may cite" in text  # init's comments are kept
    assert "# email:" not in text
    assert T._todos(yaml.safe_load(text)) == []
    chk = T.check(conn, QID, directory=drafted["td"], sender_file=drafted["sender"])
    assert chk.failures == []


def test_candidate_drafts_have_no_subject(conn, inputs, paths, monkeypatch) -> None:
    with conn:
        conn.execute("UPDATE signals SET offer_type = 'candidate' WHERE company_id = ?", (QID,))
    drafter(monkeypatch, DRAFT)
    assert run(conn, paths).outcome == "drafted"
    _, doc = T.load(T.file_for(QID, paths["td"]))
    assert doc.email is not None and doc.email.subject is None


def test_cache_hit_makes_no_call_and_unchanged_makes_no_write(conn, drafted,
                                                              monkeypatch) -> None:
    p = T.file_for(QID, drafted["td"])
    first = p.read_text(encoding="utf-8")
    f = drafter(monkeypatch)  # no reply scripted
    r = run(conn, drafted)
    assert (r.outcome, r.calls, f["claude"].calls) == ("unchanged", 0, 0)
    p.unlink()
    r = run(conn, drafted)  # the file is gone; the gateway cache answers
    assert (r.outcome, r.calls, f["claude"].calls) == ("drafted", 0, 0)
    assert p.read_text(encoding="utf-8") == first
    assert draft_calls(conn) == 1


def test_human_edit_blocks_overwrite_without_force(conn, drafted, monkeypatch) -> None:
    data = load(drafted)
    data["why_now"] = "Quorvane is hiring now; the pipeline work lands on the new hire."
    save(drafted, data)
    write_voice(drafted["voice"], VOICE[:2])  # changed inputs: a new key
    f = drafter(monkeypatch, DRAFT)
    r = run(conn, drafted)
    assert r.outcome == "refused" and "section 4" in r.detail and "--force" in r.detail
    assert f["claude"].calls == 0
    assert load(drafted)["why_now"].startswith("Quorvane is hiring now;")
    r = run(conn, drafted, force=True)
    assert r.outcome == "drafted" and f["claude"].calls == 1
    assert load(drafted)["why_now"] == DRAFT["section4"]


def test_hand_written_file_without_a_marker_is_not_overwritten(conn, inputs, paths,
                                                                monkeypatch) -> None:
    T.init(conn, QID, directory=paths["td"])
    data = load(paths)
    data["might_have_wrong"] = "Written by a person."
    save(paths, data)
    f = drafter(monkeypatch, DRAFT)
    r = run(conn, paths)
    assert r.outcome == "refused" and "section 7" in r.detail and f["claude"].calls == 0
    # a fresh skeleton (all TODO) is drafted into
    T.init(conn, QID, directory=paths["td"], force=True)
    assert run(conn, paths).outcome == "drafted"


# -- voice examples ------------------------------------------------------------------------


def test_fewer_than_three_voice_examples_warns(conn, inputs, paths, monkeypatch) -> None:
    write_voice(paths["voice"], VOICE[:1] + [{"variant": "adjacent", "subject": "TODO",
                                              "body": "TODO"}])
    drafter(monkeypatch, DRAFT)
    r = run(conn, paths)
    assert r.outcome == "drafted" and r.voice == 1
    assert any("1 voice example" in w for w in r.warnings)


def test_no_voice_examples_refuses_unless_no_voice(conn, inputs, paths, monkeypatch) -> None:
    f = drafter(monkeypatch, DRAFT)
    r = run(conn, paths, voice_file=paths["voice"].with_name("missing.yaml"))
    assert r.outcome == "refused" and "--no-voice" in r.detail and f["claude"].calls == 0
    r = run(conn, paths, voice_file=paths["voice"].with_name("missing.yaml"), no_voice=True)
    assert (r.outcome, r.voice) == ("drafted", 0)
    assert "No voice examples in this run" in f["claude"].prompts[0]


@pytest.mark.parametrize("body,why", [
    ("Hi Mirela,\n\nNotes for you: {link}\n", "name from the contacts table"),
    ("Hi {first_name},\n\nWrite to me at avery@post.example any time.\n", "email address"),
])
def test_voice_example_with_a_contact_or_address_is_refused(conn, inputs, paths, monkeypatch,
                                                            body, why) -> None:
    with conn:
        db.insert(conn, Contact(company_id=QID, name="Mirela Dunstan", title="Head of Growth"))
    write_voice(paths["voice"], [*VOICE[:2], {"variant": "adjacent", "subject": "", "body": body}])
    f = drafter(monkeypatch, DRAFT)
    r = run(conn, paths)
    assert r.outcome == "refused" and why in r.detail and "voice example 3" in r.detail
    assert "Mirela" not in r.detail and "avery@" not in r.detail
    assert f["claude"].calls == 0 and draft_calls(conn) == 0


def test_held_out_quote_in_a_voice_example_is_refused(conn, inputs, paths, monkeypatch) -> None:
    write_voice(paths["voice"], [*VOICE[:2], {"variant": "adjacent", "subject": "",
                                              "body": f"Hi {{first_name}},\n\n{HELD_QUOTE}.\n"}])
    f = drafter(monkeypatch, DRAFT)
    r = run(conn, paths)
    assert r.outcome == "refused" and "held_out" in r.detail
    assert f["claude"].calls == 0 and draft_calls(conn) == 0


def test_init_voice_skeleton_is_not_counted_and_never_overwritten(tmp_path) -> None:
    p = DR.init_voice(tmp_path / "v.yaml")
    assert DR.load_voice(p) == []
    with pytest.raises(DR.DrafterError):
        DR.init_voice(p)
    r = CliRunner().invoke(main, ["draft", "init-voice", "--voice", str(tmp_path / "w.yaml")])
    assert r.exit_code == 0 and (tmp_path / "w.yaml").exists()


# -- dry run and CLI -----------------------------------------------------------------------


def _db(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


def test_dry_run_makes_no_call_and_no_file(conn, inputs, paths, monkeypatch) -> None:
    f = drafter(monkeypatch)
    r = run(conn, paths, dry_run=True)
    assert r.outcome == "dry_run" and "would call" in r.detail
    assert f["claude"].calls == 0 and not T.file_for(QID, paths["td"]).exists()


def test_cli_draft_by_company_id(conn, inputs, paths, monkeypatch) -> None:
    drafter(monkeypatch, DRAFT)
    args = ["--dir", str(paths["td"]), "--voice", str(paths["voice"]), "--db", _db(conn)]
    r = CliRunner().invoke(main, ["draft", QID, *args])
    assert r.exit_code == 0, r.output
    assert f"{QID}: drafted: d-1.0:" in r.output and "provider calls 1" in r.output
    r = CliRunner().invoke(main, ["draft", QID, *args])
    assert r.exit_code == 0 and f"{QID}: unchanged" in r.output
    r = CliRunner().invoke(main, ["draft", *args])
    assert r.exit_code == 1 and "give a company_id" in r.output

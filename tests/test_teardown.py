"""Steps 52-53: teardown authoring (init, check) and renderer v1.

Invented companies only: Quorvane from tests/test_extract_facts.py, judged and
verified through the scripted fakes of tests/test_judge_verify.py. Every path
(teardowns, sender, site, database, cache) is under tmp_path; sockets are
blocked and no model is called after the fixture's scripted judge and verifier.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from engine import db
from engine.build import render as R
from engine.build import sender as S
from engine.build import teardown as T
from engine.cli import main
from engine.judge import verify as V
from engine.llm import gateway
from engine.models import Contact
from tests.test_extract_facts import QID, Q_DOMAIN, bundles, cache  # noqa: F401 (fixtures)
from tests.test_judge import (  # noqa: F401 (fixtures)
    JOB,
    JOB_Q,
    PRICING,
    PRICING_Q,
    PRODUCT,
    extracted,
    no_net,
    split,
)
from tests.test_judge_verify import NOT, SUP, judged, providers  # noqa: F401 (fixtures)

HOME = f"https://{Q_DOMAIN}/"
SLICE_URL = "https://brightsmile-dental.example/locations"


# -- fixtures ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_model_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test's scripted judge and verifier calls start from a fresh per-process
    budget, so these fixtures never exhaust the shared one for later tests."""
    from engine.guard.budget import Budget

    monkeypatch.setattr(gateway, "_budget", Budget())


@pytest.fixture
def verified(conn, cache, judged, monkeypatch) -> None:
    """Quorvane diagnosed G3 with three claims, all supported by the verifier."""
    providers(monkeypatch, gemini=[SUP, SUP, SUP])
    assert V.verify_company(conn, cache, QID).outcome == "pass"
    with conn:
        conn.execute("UPDATE signals SET offer_type = 'adjacent' WHERE company_id = ?", (QID,))

    def refuse(*a, **kw):
        raise AssertionError("the teardown tooling made a model call")

    monkeypatch.setattr(gateway, "call", refuse)


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    return {"td": tmp_path / "td", "sender": tmp_path / "sender.yaml", "site": tmp_path / "site"}


GOOD_SENDER: dict[str, Any] = {
    "version": S.SENDER_VERSION,
    "name": "Avery Example",
    "identity": "I build outbound and enrichment systems.",
    "about": ["I build outbound and enrichment systems.",
              "I have run a revenue number at a small company.",
              "I write the Python and SQL myself."],
    "links": [{"label": "CV", "url": "https://avery.example/cv"},
              {"label": "Code", "url": "https://code.example/avery"}],
    "offer": {"candidate": "I have applied for the {role} role at {company}.",
              "adjacent": "I am looking for a GTM engineer role, and I am open to a project."},
    "postal_address": "12 Example Road, Pune 411001",
    "opt_out": 'Not relevant? Reply "no" to my email and I will not write again.',
}


def write_sender(path: Path, **change: Any) -> None:
    data = {**GOOD_SENDER, **change}
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def fill(doc: dict[str, Any]) -> dict[str, Any]:
    """The hand-written sections of a passing teardown."""
    doc["need"] = {
        "sentences": [
            {"text": "The Quorvane job post asks the first sales hire to set up HubSpot "
                     "properties and pipelines.", "claims": [0]},
            {"text": "The Quorvane pricing page routes buyers to a custom quote, and no "
                     "product page is linked from the site.", "claims": [1, 2]},
        ],
        "evidence": [0, 1, 2],
    }
    doc["why_now"] = ("Quorvane is hiring its founding SDR now, so the CRM and pipeline work "
                      "lands on that hire in week one.")
    doc["build"] = ["A HubSpot pipeline with stages that match the demo form.",
                    "An account list of dental chains, scored by clinic count.",
                    "A weekly report of replies by segment."]
    doc["slice"] = {
        "title": "Ten dental chains in Pune that match Quorvane's customers page.",
        "columns": ["Account", "Pricing page"],
        "rows": [{"cells": ["Brightsmile Dental", "not found"], "url": SLICE_URL,
                  "quote": "Twelve clinics across Pune and Nashik"}],
    }
    doc["might_have_wrong"] = ("This is an outside view from public pages. I cannot see "
                               "whether a CRM is already set up.")
    return doc


def path_for(dirs: dict[str, Path]) -> Path:
    return T.file_for(QID, dirs["td"])


def load_doc(dirs: dict[str, Path]) -> dict[str, Any]:
    return yaml.safe_load(path_for(dirs).read_text(encoding="utf-8"))


def save_doc(dirs: dict[str, Path], doc: dict[str, Any]) -> None:
    path_for(dirs).write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
                              encoding="utf-8")


@pytest.fixture
def ready(conn, verified, dirs) -> dict[str, Path]:
    """A passing teardown file and sender file for Quorvane."""
    T.init(conn, QID, directory=dirs["td"])
    save_doc(dirs, fill(load_doc(dirs)))
    write_sender(dirs["sender"])
    return dirs


def run_check(conn, dirs) -> T.Checked:
    return T.check(conn, QID, directory=dirs["td"], sender_file=dirs["sender"])


def rules(chk: T.Checked) -> set[str]:
    return {f.rule for f in chk.failures}


def edit(conn, dirs, change) -> T.Checked:
    doc = load_doc(dirs)
    change(doc)
    save_doc(dirs, doc)
    return run_check(conn, dirs)


def render(conn, dirs, **kw) -> R.Rendered:
    return R.render_company(conn, QID, teardowns=dirs["td"], sender_file=dirs["sender"],
                            site=dirs["site"], **kw)


# -- init ----------------------------------------------------------------------------------


def test_init_prefills_from_the_diagnosis_and_capture(conn, verified, dirs) -> None:
    p = T.init(conn, QID, directory=dirs["td"])
    assert p == dirs["td"] / f"{QID}.yaml"
    text = p.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert doc["header"]["company"] == "Quorvane"
    assert doc["header"]["role"] == "Founding Sales Development Representative"
    assert doc["header"]["offer_type"] == "adjacent"
    kinds = {r["kind"]: r for r in doc["looked_at"]}
    assert "about" not in kinds  # blocked: left out
    assert kinds["product"]["found"] is False and kinds["pricing"]["found"] is True
    assert kinds["pricing"]["url"] == PRICING
    # the claims are listed by index, with their state
    assert "[0] job_post" in text and JOB_Q in text and "[2] product, NOT FOUND" in text
    assert text.count("#       passing") == 3
    assert doc["might_have_wrong"].startswith("TODO: ") and "CRM" in doc["might_have_wrong"]
    assert doc["need"]["evidence"] == [0, 1, 2]
    assert {c["name"] for c in doc["competitors"]} >= {"Routely"}
    assert all(c["confirmed"] is False for c in doc["competitors"])
    # the skeleton fails until written
    chk = run_check(conn, dirs)
    assert {"unfinished", "untraced"} <= rules(chk)


def test_init_never_overwrites_without_force(conn, verified, dirs) -> None:
    p = T.init(conn, QID, directory=dirs["td"])
    p.write_text("# mine\n", encoding="utf-8")
    with pytest.raises(T.TeardownError, match="--force"):
        T.init(conn, QID, directory=dirs["td"])
    assert p.read_text(encoding="utf-8") == "# mine\n"
    r = CliRunner().invoke(main, ["teardown", "init", QID, "--dir", str(dirs["td"]),
                                  "--db", conn.execute("PRAGMA database_list").fetchone()[2]])
    assert r.exit_code == 1 and "--force" in r.output
    assert p.read_text(encoding="utf-8") == "# mine\n"
    T.init(conn, QID, directory=dirs["td"], force=True)
    assert "version: td-1.0" in p.read_text(encoding="utf-8")


def test_init_refuses_without_a_diagnosis(conn, cache, extracted, dirs) -> None:
    with pytest.raises(T.TeardownError, match="no diagnosis"):
        T.init(conn, QID, directory=dirs["td"])
    assert not path_for(dirs).exists()


def test_init_sender_skeleton_and_no_overwrite(tmp_path) -> None:
    p = tmp_path / "sender.yaml"
    offer = ("# OFFER.md\n\nVersion 1 · 2026-01-01 · Avery Example\n\n## Who I am, in one line\n\n"
             "A builder of GTM systems.\n\n---\n\n## Offer type 2 — `adjacent`\n\n"
             "> I am looking for a GTM engineer role.\n\n---\n\n## Companies I will not "
             "contact\n\n| Zentrico | reason |\n")
    o = S.from_offer(offer)
    assert o == {"name": "Avery Example", "identity": "A builder of GTM systems.",
                 "adjacent": "I am looking for a GTM engineer role."}
    S.init(p)
    s = S.load(p)
    assert S.problems(s)  # TODOs left
    assert "Zentrico" not in p.read_text(encoding="utf-8")
    with pytest.raises(S.SenderError, match="--force"):
        S.init(p)
    r = CliRunner().invoke(main, ["teardown", "init-sender", "--sender", str(p)])
    assert r.exit_code == 1


def test_real_offer_md_parses_without_the_do_not_contact_list() -> None:
    sk = S.skeleton()
    offer = S.OFFER_PATH.read_text(encoding="utf-8")
    table = offer[offer.index("## Companies I will not contact"):]
    rows = [ln.split("|")[1].strip() for ln in table.splitlines()
            if ln.startswith("|") and "---" not in ln][1:]
    assert rows and not any(r and r in sk for r in rows)


# -- check: each rule ----------------------------------------------------------------------


def test_a_good_teardown_passes(conn, ready) -> None:
    chk = run_check(conn, ready)
    assert chk.failures == []
    r = CliRunner().invoke(main, ["teardown", "check", QID, "--dir", str(ready["td"]),
                                  "--sender", str(ready["sender"]),
                                  "--db", conn.execute("PRAGMA database_list").fetchone()[2]])
    assert r.exit_code == 0 and f"{QID}: ok" in r.output


def test_word_limit(conn, ready) -> None:
    long = " ".join(["Quorvane"] + ["word"] * 45) + "."
    chk = edit(conn, ready, lambda d: d.update(why_now=long))
    assert rules(chk) == {"word_limit"}
    assert any("section 4 has 46 words" in f.message for f in chk.failures)


def test_section_8_word_limit_counts_the_sender(conn, ready) -> None:
    write_sender(ready["sender"], about=["word " * 25, "word " * 20, "word " * 10])
    assert "word_limit" in rules(run_check(conn, ready))


def test_build_items_exactly_three(conn, ready) -> None:
    chk = edit(conn, ready, lambda d: d["build"].pop())
    assert rules(chk) == {"build_items"}


def test_banned_phrase(conn, ready) -> None:
    def ch(d):
        d["why_now"] = "Quick question: Quorvane is hiring now, so we can leverage the CRM."

    chk = edit(conn, ready, ch)
    assert rules(chk) == {"banned_phrase"}
    msgs = " ".join(f.message for f in chk.failures)
    assert "quick question" in msgs and "leverage" in msgs
    # leverage as a noun passes
    chk = edit(conn, ready, lambda d: d.update(
        why_now="Quorvane is hiring now; the pricing page gives buyers little leverage."))
    assert chk.failures == []


def test_superlative_about_the_sender(conn, ready) -> None:
    write_sender(ready["sender"], about=["A world-class builder.", "Line two.", "Line three."])
    assert rules(run_check(conn, ready)) == {"superlative"}


def test_company_name_count(conn, ready) -> None:
    def ch(d):
        for s in d["need"]["sentences"]:
            s["text"] = s["text"].replace("Quorvane", "The")
        d["why_now"] = d["why_now"].replace("Quorvane", "The company")
        d["slice"]["title"] = "Ten dental chains in Pune."

    chk = edit(conn, ready, ch)
    assert rules(chk) == {"company_name"}


def test_untraced_sentence(conn, ready) -> None:
    def ch(d):
        d["need"]["sentences"].append({"text": "Quorvane reps spend hours on lists.",
                                       "claims": []})

    chk = edit(conn, ready, ch)
    assert rules(chk) == {"untraced"}
    chk = edit(conn, ready, lambda d: d["need"]["sentences"][-1].update(claims=[7]))
    assert rules(chk) == {"untraced"} and "no claim [7]" in chk.failures[0].message


def test_sentence_on_a_not_supported_or_abstained_claim(conn, cache, ready,
                                                        monkeypatch) -> None:
    # the verifier finds claim 1 not_supported (outcome review): citing it fails
    v = V.VerifierVerdict.model_validate_json(
        conn.execute("SELECT verifier_verdict FROM diagnoses").fetchone()[0])
    v.claims[1].verdict = "not_supported"
    v.not_supported, v.outcome = 1, "review"
    with conn:
        conn.execute("UPDATE diagnoses SET verifier_verdict = ?", (v.model_dump_json(),))
    chk = run_check(conn, ready)
    assert "untraced" in rules(chk) and "evidence" in rules(chk)
    assert any("not_supported" in f.message for f in chk.failures)
    # no verdict at all: nothing passes
    with conn:
        conn.execute("UPDATE diagnoses SET verifier_verdict = NULL")
    chk = run_check(conn, ready)
    assert any("verifier has not run" in f.message for f in chk.failures)


def test_one_sentence_per_item(conn, ready) -> None:
    chk = edit(conn, ready, lambda d: d["need"]["sentences"][0].update(
        text="The Quorvane job post names HubSpot. It asks for pipelines."))
    assert rules(chk) == {"one_sentence"}


def test_long_quote_in_the_slice(conn, ready) -> None:
    q = " ".join(["clinic"] * 25)
    chk = edit(conn, ready, lambda d: d["slice"]["rows"][0].update(quote=q))
    assert rules(chk) == {"quote_length"}


def test_long_quote_in_the_evidence(conn, ready, monkeypatch) -> None:
    from engine.models import Claim

    ctx = T.load_context(conn, QID)
    ctx.claims[1] = Claim(text="x", artifact_kind="pricing", found=True, url=PRICING,
                          quote=" ".join(["word"] * 26))
    monkeypatch.setattr(T, "load_context", lambda *a, **k: ctx)
    assert "quote_length" in rules(run_check(conn, ready))


@pytest.mark.parametrize("row", [
    {"cells": ["Brightsmile Dental", "not found"], "quote": "Twelve clinics"},  # no url
    {"cells": ["Brightsmile Dental", "not found"], "url": SLICE_URL},  # no quote
    {"cells": ["Brightsmile Dental", ""], "url": SLICE_URL, "quote": "Twelve clinics"},
    {"cells": ["Brightsmile Dental"], "url": SLICE_URL, "quote": "Twelve clinics"},
    {"cells": ["Brightsmile Dental", "x"], "url": "ftp://x.example", "quote": "Twelve"},
])
def test_slice_row_without_url_quote_or_cell(conn, ready, row) -> None:
    chk = edit(conn, ready, lambda d: d["slice"].update(rows=[row]))
    assert rules(chk) == {"slice_row"}


def test_slice_takes_at_most_ten_rows(conn, ready) -> None:
    def ch(d):
        d["slice"]["rows"] = d["slice"]["rows"] * 11

    assert rules(edit(conn, ready, ch)) == {"slice_row"}


def test_contact_value_in_text_is_refused_without_naming_it(conn, ready) -> None:
    with conn:
        db.insert(conn, Contact(company_id=QID, name="Mirela Dunstan", title="Head of Growth"))
    chk = edit(conn, ready, lambda d: d.update(
        why_now="Quorvane is hiring now, and Mirela will want the CRM ready in week one."))
    assert rules(chk) == {"contact_value"}
    assert all("Mirela" not in f.message and "Dunstan" not in f.message for f in chk.failures)
    r = CliRunner().invoke(main, ["teardown", "check", QID, "--dir", str(ready["td"]),
                                  "--sender", str(ready["sender"]),
                                  "--db", conn.execute("PRAGMA database_list").fetchone()[2]])
    assert r.exit_code == 1 and "contact_value" in r.output and "Mirela" not in r.output


def test_first_name_placeholder_never_on_the_page(conn, ready) -> None:
    chk = edit(conn, ready, lambda d: d.update(
        why_now="{first_name}, Quorvane is hiring its founding SDR now."))
    assert rules(chk) == {"placeholder"}


def test_unconfirmed_competitor(conn, ready) -> None:
    chk = edit(conn, ready, lambda d: d.update(
        why_now="Quorvane is hiring now, and its compare page sets it against Routely."))
    assert rules(chk) == {"competitor"}

    def confirm(d):
        for c in d["competitors"]:
            if c["name"] == "Routely":
                c["confirmed"] = True

    assert edit(conn, ready, confirm).failures == []


def test_stale_diagnosis_and_blocked_page_in_section_2(conn, ready) -> None:
    chk = edit(conn, ready, lambda d: d["diagnosis"].update(key="000000000000"))
    assert rules(chk) == {"diagnosis"}

    T.init(conn, QID, directory=ready["td"], force=True)
    fresh = load_doc(ready)
    doc = fill(fresh)
    doc["looked_at"].append({"kind": "about", "url": f"https://{Q_DOMAIN}/about",
                             "found": False, "captured": doc["looked_at"][0]["captured"]})
    save_doc(ready, doc)
    chk = run_check(conn, ready)
    assert rules(chk) == {"looked_at"} and "blocked or unreachable" in chk.failures[0].message


def test_total_words(conn, ready) -> None:
    def ch(d):
        d["build"] = [" ".join(["Quorvane"] + ["word"] * 140)] * 3

    chk = edit(conn, ready, ch)
    assert "total_words" in rules(chk)


def test_offer_type_must_match_the_signal(conn, ready) -> None:
    chk = edit(conn, ready, lambda d: d["header"].update(offer_type="candidate"))
    assert rules(chk) == {"header"}


# -- render --------------------------------------------------------------------------------


def sections(html: str) -> list[str]:
    return re.findall(r'<section id="s(\d)" data-section="\d"', html)


def test_render_writes_the_page_in_order(conn, ready, monkeypatch) -> None:
    r = render(conn, ready)
    assert r.outcome == "rendered" and r.url is None
    assert re.fullmatch(r"quorvane-[a-z0-9]{8}", r.slug)
    out = ready["site"] / r.slug
    html = (out / "index.html").read_text(encoding="utf-8")
    assert sections(html) == ["1", "2", "3", "4", "5", "6", "7", "8"]
    heads = re.findall(r"<h[12]>(.*?)</h[12]>", html)
    assert heads == ["Quorvane: notes for your Founding Sales Development Representative hire",
                     "What I looked at",
                     "What your Founding Sales Development Representative will need",
                     "Why now", "What I would build", "Already built for you",
                     "What I might have wrong", "About me, and what I am looking for"]
    assert (out / "teardown.css").exists()
    assert (ready["site"] / "_headers").read_text() == "/*\n  X-Robots-Tag: noindex, nofollow\n"
    assert (ready["site"] / "robots.txt").read_text() == "User-agent: *\nDisallow: /\n"
    assert conn.execute("SELECT status FROM companies WHERE company_id = ?",
                        (QID,)).fetchone()[0] == "built"
    # footer
    assert "12 Example Road, Pune 411001" in html
    assert "unlisted and will be deleted on request" in html
    assert "I will not write again" in html


def test_evidence_lines_follow_the_spec(conn, ready) -> None:
    html = (ready["site"] / render(conn, ready).slug / "index.html").read_text(encoding="utf-8")
    s3 = html[html.index('id="s3"'):html.index('id="s4"')]
    # found: the quoted line, then the link
    assert re.search(rf"<q>{re.escape(PRICING_Q)}</q> <a href=\"{re.escape(PRICING)}\">", s3)
    assert re.search(rf"<q>{re.escape(JOB_Q)}</q> <a href=\"{re.escape(JOB)}\">", s3)
    # not found: the sentence, a link to where it was looked for, the capture date
    m = re.search(r'<li class="not-found">(Not found: no product page linked from [^<]+)\. '
                  r'<a href="([^"]+)">[^<]+</a>, captured (\d{1,2} \w+ \d{4})</li>', s3)
    assert m and m.group(2) == PRODUCT
    s2 = html[html.index('id="s2"'):html.index('id="s3"')]
    assert "Not found: no product page linked from" in s2 and "Pricing page: found." in s2
    assert "about" not in s2.lower()  # blocked: left out of section 2
    assert s2.count("captured ") == s2.count("<li")


def test_not_found_where_label_reads_the_homepage(conn, ready) -> None:
    ctx = T.load_context(conn, QID)
    assert R.not_found_line("pricing", HOME, ctx) == \
        "Not found: no pricing page linked from the homepage"


def test_page_has_no_script_no_external_assets_and_a_viewport(conn, ready) -> None:
    html = (ready["site"] / render(conn, ready).slug / "index.html").read_text(encoding="utf-8")
    assert "<script" not in html.lower() and "<img" not in html.lower()
    assert "<iframe" not in html.lower() and " src=" not in html.lower()
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert '<meta name="robots" content="noindex, nofollow">' in html
    hrefs = set(re.findall(r'href="([^"]+)"', html))
    allowed = {"teardown.css", JOB, PRICING, PRODUCT, SLICE_URL,
               *(lk["url"] for lk in GOOD_SENDER["links"])}
    page_urls = {r["url"] for r in load_doc(ready)["looked_at"]
                 if r["url"].startswith("http")}
    assert hrefs <= allowed | page_urls
    css = (ready["site"] / render(conn, ready).slug / "teardown.css").read_text(encoding="utf-8")
    assert "@import" not in css and "url(" not in css and "max-width" in css
    assert "Avery Example" in html and "{first_name}" not in html


def test_slug_is_stable_across_re_renders(conn, ready) -> None:
    first = render(conn, ready)
    again = render(conn, ready)
    assert first.slug == again.slug and again.outcome == "unchanged"
    assert conn.execute("SELECT COUNT(*) FROM teardowns WHERE company_id = ?",
                        (QID,)).fetchone()[0] == 1
    doc = load_doc(ready)
    doc["why_now"] = doc["why_now"].replace("week one", "the first week")
    save_doc(ready, doc)
    third = render(conn, ready)
    assert third.slug == first.slug and third.outcome == "rendered"
    assert [p.name for p in ready["site"].iterdir() if p.is_dir()] == [first.slug]


def test_render_refuses_when_the_check_fails(conn, ready) -> None:
    doc = load_doc(ready)
    doc["why_now"] = "Synergy for Quorvane."
    save_doc(ready, doc)
    r = render(conn, ready)
    assert r.outcome == "refused" and {f.rule for f in r.failures} == {"banned_phrase"}
    assert not ready["site"].exists()
    assert conn.execute("SELECT COUNT(*) FROM teardowns").fetchone()[0] == 0


def test_dry_run_writes_nothing(conn, ready) -> None:
    r = render(conn, ready, dry_run=True)
    assert r.outcome == "dry_run" and r.slug is None
    assert not ready["site"].exists()
    assert conn.execute("SELECT COUNT(*) FROM teardowns").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM companies WHERE company_id = ?",
                        (QID,)).fetchone()[0] == "diagnosed"


def test_cli_render_prints_path_and_url(conn, ready, monkeypatch) -> None:
    dbf = conn.execute("PRAGMA database_list").fetchone()[2]
    args = ["teardown", "render", QID, "--dir", str(ready["td"]), "--sender",
            str(ready["sender"]), "--site-dir", str(ready["site"]), "--db", dbf]
    r = CliRunner().invoke(main, args)
    assert r.exit_code == 0, r.output
    slug = R.stored_slug(conn, QID)
    assert str(ready["site"] / slug / "index.html") in r.output
    assert "TEARDOWN_BASE_URL" in r.output
    monkeypatch.setenv("TEARDOWN_BASE_URL", "https://notes.example/")
    r = CliRunner().invoke(main, args)
    assert r.exit_code == 0 and f"url https://notes.example/{slug}/" in r.output
    r = CliRunner().invoke(main, ["teardown", "render", "--pilot", "--dry-run", "--dir",
                                  str(ready["td"]), "--sender", str(ready["sender"]),
                                  "--site-dir", str(ready["site"]), "--db", dbf])
    assert r.exit_code == 0 and f"{QID}: dry_run" in r.output


def test_no_network(conn, ready, monkeypatch) -> None:
    def refuse(*a, **kw):
        raise AssertionError("the renderer touched the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    assert render(conn, ready).outcome == "rendered"

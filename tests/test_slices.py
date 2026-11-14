"""Steps 60-63: the proof-slice builders, the evidence rule and `te slice`.

Invented companies only: Quorvane and Brindlecart from tests/test_extract_facts.py
(Quorvane's DNS is modelled on protocol/HAND_NOTES.md, on an invented domain),
competitor and candidate sites on .example hosts served by a MockTransport
(tests/capture_site.py FakeSites), and a fake search provider. Sockets are
blocked, the model gateway refuses every call, and the llm_calls ledger is
checked: no builder calls a model. Nothing under data/ is read.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
from datetime import date
from pathlib import Path

import httpx
import pytest
import yaml
from click.testing import CliRunner

from capture_site import FakeSites, fast_limiter, page
from engine import db
from engine.build import teardown as T
from engine.build.slices import common as C
from engine.build.slices import dns_inventory, funnel_compare, icp_accounts, role_funnel
from engine.build.slices import run as SR
from engine.build.slices import store as S
from engine.cli import main
from engine.extract import cta, forms, pricing
from engine.extract.base import Art, Bundle
from engine.extract.run import load_bundle
from engine.guard import search_api
from engine.guard.budget import Budget
from engine.guard.http import Guard
from engine.judge import judge as J
from engine.llm import gateway
from engine.models import Claim, Diagnosis
from tests.test_extract_facts import (  # noqa: F401 (fixtures)
    B_DOMAIN,
    BID,
    Q_DOMAIN,
    QID,
    bundles,
    cache,
)

TODAY = date(2026, 11, 14)


# -- fixtures and fakes ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a, **kw):
        raise AssertionError("a slice builder called a model")

    monkeypatch.setattr(gateway, "call", refuse)


@pytest.fixture(autouse=True)
def no_internet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only loopback (the event loop's own socketpair) may connect; every page and
    search answer comes from a MockTransport or a fake."""
    real = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"a slice test touched the network: {address!r}")
        return real(self, address)

    def refuse(*a, **kw):
        raise AssertionError("a slice test touched the network")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def extracted(conn, cache, bundles) -> None:  # noqa: F811
    """Quorvane and Brindlecart captured and extracted (facts written), offline."""
    from engine.extract import run as R

    rep = R.run(conn, cache, [QID, BID])
    assert rep.rejected == [] and rep.advanced == 2


def llm_calls(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0])


@pytest.fixture
def diagnose(conn, extracted):  # noqa: F811
    """diagnose(gap, cid): an attempted diagnosis at the judge's version; status diagnosed."""

    def make(gap: str, cid: str = QID) -> str:
        domain = Q_DOMAIN if cid == QID else B_DOMAIN
        with conn:
            conn.execute("DELETE FROM diagnoses WHERE company_id = ?", (cid,))
            db.insert(conn, Diagnosis(
                company_id=cid, prompt_version=J.PROMPT_VERSION, attempted=True, gap_type=gap,
                confidence=0.9, need="A defined target.", cannot_see="Their CRM.",
                claims=[Claim(text="The page says so.", artifact_kind="homepage", found=True,
                              url=f"https://{domain}/", quote="Book a demo")]))
            conn.execute("UPDATE companies SET status = 'diagnosed' WHERE company_id = ?", (cid,))
        return cid

    return make


@pytest.fixture
def qbundle(conn, cache, extracted) -> Bundle:  # noqa: F811
    b = load_bundle(conn, cache, QID)
    assert isinstance(b, Bundle)
    return b


class FakeSearch:
    """Stands in for search_api: answers from a table, charges the budget it was given."""

    def __init__(self, query: list[str] | None = None,
                 similar: dict[str, list[str]] | None = None, unsupported: bool = False):
        self.answers = query or []
        self.similar_answers = similar or {}
        self.unsupported = unsupported
        self.calls: list[str] = []
        self.budget: Budget | None = None

    async def query(self, q: str, *, num_results: int) -> list[str]:
        assert self.budget is not None
        self.budget.charge_fetch()
        self.calls.append(f"query:{q}")
        return self.answers[:num_results]

    async def similar(self, url: str, *, num_results: int) -> list[str]:
        if self.unsupported:
            raise search_api.Unsupported("tavily: no similar-page search")
        assert self.budget is not None
        self.budget.charge_fetch()
        self.calls.append(f"similar:{url}")
        return self.similar_answers.get(url, [])[:num_results]


def runtime(cache, sites: FakeSites | None = None, search: FakeSearch | None = None,
            budgets: list[Budget] | None = None) -> C.Runtime:
    sites = sites or FakeSites()

    def make_guard(budget: Budget) -> Guard:
        if budgets is not None:
            budgets.append(budget)
        return Guard(cache=cache, budget=budget, transport=sites.transport(),
                     limiter=fast_limiter())

    def make_searcher(budget: Budget) -> FakeSearch:
        if search is None:
            raise search_api.NotConfigured("no web search provider")
        search.budget = budget
        return search

    return C.Runtime(cache, make_guard, make_searcher, TODAY)


def build(conn, rt, cid=QID, **kw) -> SR.Result:
    return asyncio.run(SR.build_company(conn, rt, cid, **kw))


def src(art_: Art, quote: str, scope: str = "company", subject: str = Q_DOMAIN) -> C.Src:
    i = (art_.text or "").find(quote)
    return C.Src(scope, subject, art_.kind, art_.url, quote, "text", max(i, 0), True)


def slice_arts(conn, cache, cid: str, kind: str) -> dict[tuple[str, str], Art]:
    out: dict[tuple[str, str], Art] = {}
    for r in conn.execute("SELECT DISTINCT subject_domain FROM slice_artifacts WHERE "
                          "company_id = ? AND slice_kind = ?", (cid, kind)):
        for k, a in C.stored_artifacts(conn, cid, kind, r[0]).items():
            out[(r[0], k)] = C.to_art(cache, a)
    return out


def assert_rows_quoted(rows: list[dict], arts: dict[tuple[str, str], Art]) -> None:
    """Independently of verify: every row has a url and a quote found verbatim in the
    text of the artifact its source names, at that artifact's url."""
    for r in rows:
        assert r["url"] and r["quote"].strip()
        s = r["source"]
        a = arts[(s["subject"], s["kind"])]
        assert a.url == r["url"]
        assert r["quote"] in (a.text or "")
        assert len(r["quote"].split()) < C.QUOTE_LIMIT


# -- Step 63: the evidence rule ----------------------------------------------------------


def test_finalize_drops_rows_without_a_checkable_source(qbundle) -> None:
    pr = qbundle.arts["pricing"]
    good = src(pr, "Simple pricing for every clinic")

    def resolve(s: C.Src) -> Art | None:
        return qbundle.arts.get(s.kind)

    drafts = [
        C.Draft([C.cell("a"), C.cell("b")], good),
        C.Draft([C.cell("no source"), C.cell("b")], None),
        C.Draft([C.cell("blank quote"), C.cell("b")],
                C.Src("company", Q_DOMAIN, "pricing", pr.url, "  ", "text", 0)),
        C.Draft([C.cell("invented"), C.cell("b")], src(pr, "Free for ever, no card")),
        C.Draft([C.cell("wrong url"), C.cell("b")],
                C.Src("company", Q_DOMAIN, "pricing", "https://elsewhere.example/", good.quote,
                      "text", good.char_start)),
        C.Draft([C.cell("guessed absence"), C.Cell(C.NOT_FOUND, None, True)], good),
        C.Draft([C.cell("unseen"), C.UNSEEN], good),
    ]
    sl = C.finalize("role_funnel", "v", "t", ["x", "y"], drafts, resolve, min_rows=1)
    assert sl.ok and [r["cells"] for r in sl.rows] == [["a", "b"]]
    whys = [d["why"] for d in sl.dropped]
    assert len(whys) == 6
    assert "no source url and quote" in whys
    assert any("not found in pricing" in w for w in whys)
    assert any("is not the pricing artifact's" in w for w in whys)
    assert any("'not found' without a looked-at source" in w for w in whys)
    assert any("source not seen" in w for w in whys)


def test_not_found_cell_needs_an_absence_on_a_looked_at_page(qbundle) -> None:
    prod = qbundle.arts["product"]  # not_found in the bundle
    about = qbundle.arts["about"]  # blocked: unseen
    row_src = src(qbundle.arts["pricing"], "Simple pricing for every clinic")
    seen = C.Src("company", Q_DOMAIN, "product", prod.url, "not_found: product", "not_found",
                 0, False)
    unseen = C.Src("company", Q_DOMAIN, "about", about.url, "not_found: about", "not_found",
                   0, False)
    sl = C.finalize("role_funnel", "v", "t", ["x"],
                    [C.Draft([C.not_found(seen)], row_src),
                     C.Draft([C.not_found(unseen)], row_src)],
                    lambda s: qbundle.arts.get(s.kind), min_rows=1)
    assert [r["cells"] for r in sl.rows] == [[C.NOT_FOUND]]
    assert len(sl.dropped) == 1 and "about" in sl.dropped[0]["why"]


def test_long_quotes_are_cut_verbatim_and_rows_capped_at_ten() -> None:
    text = "intro\n" + " ".join(f"w{i}" for i in range(40)) + "\nend"
    a = Art("homepage", "https://x.example/", "ok", text, {})
    s = C.Src("slice", "x.example", "homepage", a.url, text.split("\n")[1], "text", 6)
    sl = C.finalize("icp_accounts", "v", "t", ["n"],
                    [C.Draft([C.cell(str(i))], s) for i in range(12)],
                    lambda _s: a, min_rows=6)
    assert sl.ok and len(sl.rows) == 10 and len(sl.dropped) == 2
    q = sl.rows[0]["quote"]
    assert len(q.split()) == C.QUOTE_KEEP and q in text
    assert text[sl.rows[0]["source"]["char_start"]:].startswith(q)
    assert all(d["why"] == "over 10 rows" for d in sl.dropped)


def test_a_slice_under_its_minimum_fails() -> None:
    sl = C.finalize("icp_accounts", "v", "t", ["n"], [], lambda s: None, min_rows=6)
    assert not sl.ok and sl.reason == "thin: 0 rows, needs 6"


def test_the_database_refuses_a_row_without_url_or_quote(conn, diagnose) -> None:
    diagnose("G6")
    bad = C.Slice("role_funnel", "v", "t", ["x"],
                  rows=[{"cells": ["a"], "url": "", "quote": "q"}])
    with pytest.raises(sqlite3.IntegrityError):
        S.write_slice(conn, QID, bad)


# -- G6: dns_inventory -------------------------------------------------------------------


def test_dns_inventory_from_a_hand_notes_like_record(conn, qbundle) -> None:
    before = llm_calls(conn)
    sl = dns_inventory.build(qbundle)
    assert sl.ok and sl.kind == "dns_inventory"
    cells = [r["cells"] for r in sl.rows]
    d = Q_DOMAIN
    assert cells[0] == ["SPF", d,
                        "authorises include:_spf.google.com, include:amazonses.com; -all"]
    assert cells[1] == ["DMARC", f"_dmarc.{d}", "p=reject, pct=100, reports to rua and ruf"]
    assert cells[2] == ["MX", d, "mail handled by aspmx.l.google.com, alt1.aspmx.l.google.com"]
    assert cells[3] == ["DKIM selector", "google", "key published"]
    assert cells[4][0] == "DKIM selectors" and cells[4][2] == C.NOT_FOUND
    assert "google" not in cells[4][1].split(", ") and "selector1" in cells[4][1]
    subs = [c for c in cells if c[0] == "Sending subdomains"]
    assert subs and all(c[2] == C.NOT_FOUND for c in subs)
    named = ", ".join(c[1] for c in subs).split(", ")
    assert {"em", "mail", "sg"} <= set(named)
    for r in sl.rows:
        assert r["url"] == f"dns:{d}"
        assert r["quote"] in qbundle.arts["dns"].text
        assert len(r["quote"].split()) < C.QUOTE_LIMIT
    assert all(r["quote"].startswith("NXDOMAIN") for r in sl.rows
               if r["cells"][2] == C.NOT_FOUND)
    assert len(sl.rows) <= C.MAX_ROWS and llm_calls(conn) == before


def test_dns_inventory_leaves_failed_lookups_out(conn, cache, extracted) -> None:  # noqa: F811
    b = load_bundle(conn, cache, BID)
    sl = dns_inventory.build(b)
    by = {r["cells"][0]: r for r in sl.rows}
    assert by["DMARC"]["cells"][2] == C.NOT_FOUND
    assert by["DMARC"]["quote"] == f"NODATA TXT _dmarc.{B_DOMAIN}"
    dkim = by["DKIM selectors"]["cells"][1].split(", ")
    assert "selector1" not in dkim and "selector2" in dkim  # selector1 FAILED: unseen
    subs = ", ".join(r["cells"][1] for r in sl.rows
                     if r["cells"][0] == "Sending subdomains").split(", ")
    assert "sg" not in subs and "mail" in subs
    assert any(r["cells"][:2] == ["Sending subdomain", f"em.{B_DOMAIN}"] for r in sl.rows)


def test_dns_inventory_fails_without_a_seen_dns_artifact(qbundle) -> None:
    arts = dict(qbundle.arts)
    arts["dns"] = Art("dns", f"dns:{Q_DOMAIN}", "dead")
    sl = dns_inventory.build(Bundle(QID, Q_DOMAIN, "Quorvane", arts))
    assert not sl.ok and "dead" in (sl.reason or "")


# -- Step 62: role_funnel ----------------------------------------------------------------


def test_role_funnel_assembles_roles_beside_the_site(conn, qbundle) -> None:
    before = llm_calls(conn)
    sl = role_funnel.build(conn, qbundle, "Quorvane")
    assert sl.ok and len(sl.rows) >= 3 and sl.columns == role_funnel.COLUMNS
    first = sl.rows[0]["cells"]
    assert first[:2] == ["Founding Sales Development Representative", "build prospect lists"]
    assert first[2:] == ["3 linked from the homepage", "found; names india, uae, singapore",
                         "found; lists prices", "found on the demo path"]
    assert any(r["cells"][1] == "post not captured" for r in sl.rows)
    assert_rows_quoted(sl.rows, {(Q_DOMAIN, k): a for k, a in qbundle.arts.items()})
    assert llm_calls(conn) == before


def _blocked(conn, qbundle, facts: tuple[str, ...], kinds: tuple[str, ...]) -> Bundle:
    """Quorvane with these pages blocked: their facts are gone, as the extractor
    writes nothing for an unseen page."""
    with conn:
        conn.execute(f"DELETE FROM facts WHERE company_id = ? AND fact IN "
                     f"({','.join('?' * len(facts))})", (QID, *facts))
    arts = dict(qbundle.arts)
    for k in kinds:
        arts[k] = Art(k, arts[k].url, "blocked")
    return Bundle(QID, Q_DOMAIN, "Quorvane", arts, qbundle.signal_title, qbundle.classify)


def test_role_funnel_drops_the_column_of_an_unseen_page_and_keeps_the_rows(
        conn, qbundle) -> None:
    full = role_funnel.build(conn, qbundle, "Quorvane")
    b = _blocked(conn, qbundle, ("has_booking_step", "demo_form_fields"), ("demo_form",))
    sl = role_funnel.build(conn, b, "Quorvane")
    assert sl.ok and len(sl.rows) == len(full.rows)
    assert "Booking step" not in sl.columns and len(sl.columns) == len(role_funnel.COLUMNS) - 1
    assert all(len(r["cells"]) == len(sl.columns) for r in sl.rows)
    assert not any(C.NOT_FOUND in r["cells"] for r in sl.rows)
    assert sl.meta["dropped_columns"] == [{"column": "Booking step", "kind": "demo_form",
                                           "why": "source not seen: demo_form blocked"}]
    assert [r["quote"] for r in sl.rows] == [r["quote"] for r in full.rows]
    assert_rows_quoted(sl.rows, {(Q_DOMAIN, k): a for k, a in qbundle.arts.items()})


def test_role_funnel_with_three_site_pages_unseen_still_has_two_columns(conn, qbundle) -> None:
    b = _blocked(conn, qbundle, ("has_booking_step", "pricing_model", "customer_regions"),
                 ("demo_form", "pricing", "customers"))
    sl = role_funnel.build(conn, b, "Quorvane")
    assert sl.ok and sl.columns == ["Open role", "What the post asks for", "Segment pages"]
    assert len(sl.meta["dropped_columns"]) == 3


def test_role_funnel_with_every_site_page_unseen_is_thin(conn, qbundle) -> None:
    b = _blocked(conn, qbundle, ("has_booking_step", "pricing_model", "customer_regions",
                                 "segment_pages"),
                 ("demo_form", "pricing", "customers", "homepage"))
    sl = role_funnel.build(conn, b, "Quorvane")
    assert not sl.ok and sl.rows == []
    assert sl.reason == "thin: 1 column(s) besides the role, needs 2"
    assert sl.columns == ["Open role", "What the post asks for"]
    assert {d["column"] for d in sl.meta["dropped_columns"]} == {
        "Segment pages", "Customers page", "Pricing page", "Booking step"}


def test_role_funnel_is_thin_without_open_roles_seen(conn, cache, extracted) -> None:
    b = load_bundle(conn, cache, BID)  # post robots_denied, no job board
    sl = role_funnel.build(conn, b, "Brindlecart")
    assert not sl.ok and sl.rows == [] and sl.reason == "thin: 0 rows, needs 3"


def test_role_funnel_writes_not_found_for_a_page_looked_for(conn, qbundle) -> None:
    facts = [f for f in role_funnel.load_facts(conn, QID) if f.fact != "customer_regions"]
    facts.append(role_funnel.FactRow("customer_regions", "none", "customers",
                                     "not_found: customers", 0, "not_found", False))
    arts = dict(qbundle.arts)
    arts["customers"] = Art("customers", f"https://{Q_DOMAIN}/", "not_found")
    b = Bundle(QID, Q_DOMAIN, "Quorvane", arts, qbundle.signal_title)
    sl = C.finalize("role_funnel", "v", "t", role_funnel.COLUMNS, role_funnel.drafts(facts, b),
                    lambda s: b.arts.get(s.kind), min_rows=3)
    assert sl.ok and all(r["cells"][3] == C.NOT_FOUND for r in sl.rows)


# -- Step 63: fallback chain, hold, idempotency ------------------------------------------


def test_g6_selects_dns_inventory_and_is_idempotent(conn, cache, diagnose) -> None:
    diagnose("G6")
    rt = runtime(cache)
    r = build(conn, rt)
    assert r.outcome == "selected" and r.selected == "dns_inventory"
    assert S.selected(conn, QID).kind == "dns_inventory"
    again = build(conn, rt)
    assert again.changed == 0 and again.selected == "dns_inventory"


def test_search_not_configured_fails_icp_and_falls_back(conn, cache, diagnose) -> None:
    diagnose("G1")
    before = llm_calls(conn)
    r = build(conn, runtime(cache))
    assert r.selected == "role_funnel"
    icp = S.get(conn, QID, "icp_accounts")
    assert icp is not None and not icp.ok and icp.reason == "search not configured"
    assert icp.rows == [] and llm_calls(conn) == before


def test_both_thin_holds_for_review_without_dropping(conn, cache, diagnose) -> None:
    diagnose("G3", BID)
    r = build(conn, runtime(cache), BID)
    assert r.outcome == "held" and r.selected is None
    row = conn.execute("SELECT status, review_reason, drop_reason FROM companies "
                       "WHERE company_id = ?", (BID,)).fetchone()
    assert row["status"] == "diagnosed" and row["drop_reason"] is None
    assert row["review_reason"].startswith("slice_thin: icp_accounts search not configured; "
                                           "role_funnel thin:")
    assert build(conn, runtime(cache), BID).changed == 0


def test_a_hold_never_overwrites_another_review_reason(conn, cache, diagnose) -> None:
    diagnose("G3", BID)
    with conn:
        conn.execute("UPDATE companies SET review_reason = 'qualify_hold: x: y' "
                     "WHERE company_id = ?", (BID,))
    assert build(conn, runtime(cache), BID).outcome == "kept_hold"
    assert conn.execute("SELECT review_reason FROM companies WHERE company_id = ?",
                        (BID,)).fetchone()[0] == "qualify_hold: x: y"


def test_refuses_without_an_attempted_diagnosis(conn, cache, extracted) -> None:  # noqa: F811
    assert build(conn, runtime(cache)).outcome == "refused"


def test_dry_run_sends_and_writes_nothing(conn, cache, diagnose) -> None:
    diagnose("G2")
    sites = FakeSites()
    r = build(conn, runtime(cache, sites), dry_run=True)
    assert r.outcome == "dry_run" and sites.hits == []
    assert S.slices(conn, QID) == [] and S.competitors(conn, QID) == []


# -- Step 61: funnel_compare -------------------------------------------------------------

ROUTELY, FIELDNEST = "routely.example", "fieldnest.example"
SETS = (f"Routely={ROUTELY}", f"Fieldnest={FIELDNEST}")


def competitor_sites() -> FakeSites:
    s = FakeSites()
    s.route(ROUTELY, "/", page(
        '<header><nav><a href="/pricing">Pricing</a></nav></header>'
        '<main><h1>Routely runs hospital rosters</h1>'
        '<a class="btn" href="/demo">Book a demo</a></main>'))
    s.route(ROUTELY, "/pricing", page("<main><h1>Plans</h1><p>$29 per user per month</p>"
                                      "</main>"))
    s.route(ROUTELY, "/demo", page(
        '<main><form action="/x"><label for="a">Work email</label><input id="a" name="email">'
        '<label for="b">Company</label><input id="b" name="company">'
        '<label for="c">Phone</label><input id="c" name="phone"></form></main>'))
    s.route(FIELDNEST, "/", page(
        '<header><nav><a href="/plans">Plans</a></nav></header>'
        '<main><h1>Fieldnest staffs dental groups</h1>'
        '<a class="btn" href="https://calendly.com/fieldnest/demo">Book a demo</a></main>'))
    s.route(FIELDNEST, "/plans", page("<main><h1>Plans</h1><p>Contact sales for a custom "
                                      "quote.</p></main>"))
    s.route("calendly.com", "/fieldnest/demo", page("<main><h1>Pick a time</h1></main>"))
    return s


def test_competitors_wait_for_a_yes_and_teardown_check_refuses_until_then(
        conn, cache, diagnose, tmp_path, sender) -> None:
    diagnose("G2")
    sites = competitor_sites()
    rt = runtime(cache, sites)
    # the company names Routely, Fieldnest and Shiftwise on its compare page, with no links
    ch = asyncio.run(SR.competitors(conn, rt, QID))
    assert [c.name for c in ch.competitors] == ["Routely", "Fieldnest"]
    assert not ch.ready and "no domain for Routely, Fieldnest" in (ch.why or "")
    assert all(c.source == "named_competitors" and c.evidence_quote for c in ch.competitors)
    ch = asyncio.run(SR.competitors(conn, rt, QID, sets=SETS))
    assert ch.ready and not any(c.confirmed for c in ch.competitors)

    r = build(conn, rt)
    assert r.outcome == "pending" and sites.hits == []  # nothing captured before the yes
    assert S.get(conn, QID, "funnel_compare") is None
    assert conn.execute("SELECT COUNT(*) FROM slice_artifacts").fetchone()[0] == 0

    td = tmp_path / "td"
    p = T.init(conn, QID, directory=td, today=TODAY)
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert {"name": "Routely", "confirmed": False} in doc["competitors"]
    doc["why_now"] = "Routely lists prices on its own page."
    doc["competitors"] = [{"name": "Routely", "confirmed": True}]  # the file cannot say yes
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    chk = T.check(conn, QID, directory=td, sender_file=tmp_path / "none.yaml")
    assert any(f.rule == "competitor" and "Routely" in f.message for f in chk.failures)

    assert SR.confirm(conn, QID) == 2
    r = build(conn, rt)
    assert r.selected == "funnel_compare" and sites.hits
    chk = T.check(conn, QID, directory=td, sender_file=tmp_path / "none.yaml")
    assert not any(f.rule == "competitor" for f in chk.failures)


def test_funnel_compare_runs_the_same_extractors_on_all_three(
        conn, cache, diagnose, sender, monkeypatch) -> None:
    diagnose("G2")
    assert [fn for _, fn in funnel_compare.EXTRACTORS] == [
        pricing.pricing_model, cta.primary_cta, forms.demo_form_fields, forms.has_booking_step]
    seen: dict[str, list[str]] = {}

    def spy(name, fn):
        def wrapped(b):
            seen.setdefault(name, []).append(b.company_id)
            return fn(b)
        return wrapped

    monkeypatch.setattr(funnel_compare, "EXTRACTORS",
                        tuple((n, spy(n, f)) for n, f in funnel_compare.EXTRACTORS))
    sites = competitor_sites()
    budgets: list[Budget] = []
    rt = runtime(cache, sites, budgets=budgets)
    asyncio.run(SR.competitors(conn, rt, QID, sets=SETS))
    SR.confirm(conn, QID)
    n_artifacts = conn.execute("SELECT COUNT(*) FROM artifacts WHERE company_id = ?",
                               (QID,)).fetchone()[0]
    before = llm_calls(conn)
    r = build(conn, rt)
    assert r.selected == "funnel_compare"
    three = [QID, f"{QID}/{ROUTELY}", f"{QID}/{FIELDNEST}"]
    assert seen == {n: three for n, _ in funnel_compare.EXTRACTORS}

    st = S.get(conn, QID, "funnel_compare")
    rows = {r["cells"][0]: r["cells"] for r in st.rows}
    assert rows["Routely"] == ["Routely", "lists prices", "Book a demo (demo)", "3 fields",
                               C.NOT_FOUND]
    assert rows["Fieldnest"][1] == "contact sales"
    assert rows["Fieldnest"][4] == "scheduling step found"
    assert rows["Quorvane"][1] == "lists prices" and rows["Quorvane"][4] == "scheduling step found"
    assert not any("convert" in c.lower() for r in st.rows for c in r["cells"])
    arts = {(Q_DOMAIN, k): a for k, a in load_bundle(conn, cache, QID).arts.items()}
    arts.update(slice_arts(conn, cache, QID, "funnel_compare"))
    assert_rows_quoted(st.rows, arts)
    # competitor pages live apart from the company's own bundle
    assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE company_id = ?",
                        (QID,)).fetchone()[0] == n_artifacts
    assert conn.execute("SELECT COUNT(*) FROM slice_artifacts WHERE slice_kind = "
                        "'funnel_compare'").fetchone()[0] == 6
    assert budgets and budgets[0].max_fetches == funnel_compare.FETCH_CEILING
    assert st.fetches == budgets[0].fetches <= funnel_compare.FETCH_CEILING
    assert llm_calls(conn) == before
    # same inputs: not rebuilt, nothing fetched
    hits = len(sites.hits)
    assert build(conn, rt).changed == 0 and len(sites.hits) == hits


def test_funnel_compare_budget_fails_the_slice_and_falls_back(
        conn, cache, diagnose, sender, monkeypatch) -> None:
    diagnose("G2")
    monkeypatch.setattr(funnel_compare, "FETCH_CEILING", 2)
    rt = runtime(cache, competitor_sites())
    asyncio.run(SR.competitors(conn, rt, QID, sets=SETS))
    SR.confirm(conn, QID)
    r = build(conn, rt)
    assert r.selected == "role_funnel"
    fc = S.get(conn, QID, "funnel_compare")
    assert not fc.ok and fc.reason.startswith("budget:") and fc.fetches <= 2


def test_alternatives_search_only_when_none_are_named(conn, cache, diagnose) -> None:
    diagnose("G2", BID)  # Brindlecart's compare page is not_found: no named competitors
    search = FakeSearch(query=["https://g2.com/products/x/competitors",
                               f"https://{B_DOMAIN}/blog", "https://cartwheel.example/",
                               "https://www.checkoutly.example/pricing",
                               "https://third.example/"])
    rt = runtime(cache, search=search)
    r = build(conn, rt, BID)
    assert r.outcome == "pending"
    assert search.calls == ["query:Brindlecart alternatives"]
    comps = S.competitors(conn, BID)
    assert [(c.name, c.domain, c.source) for c in comps] == [
        ("Cartwheel", "cartwheel.example", "alternatives_search"),
        ("Checkoutly", "checkoutly.example", "alternatives_search")]
    assert not any(c.confirmed for c in comps)


def test_named_competitors_never_trigger_a_search(conn, cache, diagnose) -> None:
    diagnose("G2")
    search = FakeSearch(query=["https://other.example/"])
    asyncio.run(SR.competitors(conn, runtime(cache, search=search), QID))
    assert search.calls == []


def test_no_competitors_and_no_search_fails_to_role_funnel(conn, cache, diagnose) -> None:
    diagnose("G5", BID)
    r = build(conn, runtime(cache), BID)
    fc = S.get(conn, BID, "funnel_compare")
    assert not fc.ok and "search not configured" in fc.reason
    assert r.outcome == "held"  # Brindlecart's role_funnel is thin too


# -- Step 60: icp_accounts ---------------------------------------------------------------


def candidate(sites: FakeSites, host: str, body: str) -> str:
    sites.route(host, "/", page(f"<main><h1>{host}</h1>{body}</main>"))
    return f"https://{host}/"


GOOD = ("<p>Clinics across the GCC for working families.</p>"
        "<p>We launched our new diagnostics wing in October 2026.</p>")


def icp_world(n_good: int = 6) -> tuple[FakeSites, FakeSearch]:
    s = FakeSites()
    urls = [candidate(s, f"care{i}.example", GOOD) for i in range(n_good)]
    urls += [
        candidate(s, "noregion.example", "<p>Clinics in Europe.</p><p>We launched a wing in "
                                         "October 2026.</p>"),
        candidate(s, "stale.example", "<p>Clinics across the GCC.</p><p>We launched in "
                                      "March 2025.</p>"),
        candidate(s, "noreason.example", "<p>Clinics across the GCC.</p><p>Visit us.</p>"),
        candidate(s, "routely.example", GOOD),  # a competitor the company names
        f"https://{Q_DOMAIN}/blog", "https://www.g2.com/categories/x",
    ]
    return s, FakeSearch(query=urls)


def test_icp_accounts_lists_only_fit_with_a_dated_reason(conn, cache, diagnose, sender) -> None:
    diagnose("G4")
    sites, search = icp_world()
    budgets: list[Budget] = []
    before = llm_calls(conn)
    r = build(conn, runtime(cache, sites, search, budgets))
    assert r.selected == "icp_accounts"
    st = S.get(conn, QID, "icp_accounts")
    assert st.ok and [r["cells"][0] for r in st.rows] == [f"care{i}.example" for i in range(6)]
    row = st.rows[0]["cells"]
    assert row[1] == "healthcare" and row[2].startswith("launch: ") and row[3] == "2026-10-01"
    assert st.meta["descriptor"]["region"]["id"] == "middle_east"  # G4: the post's region
    assert st.meta["descriptor"]["industry"]["quote"] == "Clinics that run on Quorvane"
    assert set(st.meta["skipped"]) == {"noregion.example", "stale.example", "noreason.example"}
    assert "routely.example" not in sites.paths() and not any(
        h.host == "routely.example" for h in sites.hits)
    assert not any(h.host in (Q_DOMAIN, "www.g2.com") for h in sites.hits)
    assert search.calls == ["query:healthcare companies in middle east"]  # no customer sites
    assert st.searches == 1 and st.fetches <= icp_accounts.FETCH_CEILING
    assert budgets[0].max_fetches == icp_accounts.FETCH_CEILING
    assert_rows_quoted(st.rows, slice_arts(conn, cache, QID, "icp_accounts"))
    assert llm_calls(conn) == before


def test_icp_accounts_under_six_fails_and_falls_back(conn, cache, diagnose, sender) -> None:
    diagnose("G4")
    sites, search = icp_world(n_good=3)
    r = build(conn, runtime(cache, sites, search))
    assert r.selected == "role_funnel"
    st = S.get(conn, QID, "icp_accounts")
    assert not st.ok and st.reason == "thin: 3 rows, needs 6"


def test_icp_region_restriction_applies_to_g4_only(conn, cache, diagnose, sender) -> None:
    diagnose("G1")  # not G4: a candidate outside the region still counts
    sites, search = icp_world(n_good=3)
    build(conn, runtime(cache, sites, search))
    st = S.get(conn, QID, "icp_accounts")
    assert "noregion.example" in [r["cells"][0] for r in st.rows]
    assert st.meta["descriptor"]["region_required"] is False


def test_icp_accounts_budget_stops_the_candidates(conn, cache, diagnose, sender,
                                                  monkeypatch) -> None:
    diagnose("G4")
    monkeypatch.setattr(icp_accounts, "FETCH_CEILING", 5)
    sites, search = icp_world()
    build(conn, runtime(cache, sites, search))
    st = S.get(conn, QID, "icp_accounts")
    assert not st.ok and st.fetches <= 5
    assert any(n.startswith("stopped: fetch ceiling") for n in st.meta["notes"])


def test_icp_similar_search_on_customer_sites(conn, cache, qbundle) -> None:
    d = icp_accounts.Descriptor()
    d.industry = icp_accounts.Field("healthcare", "healthcare",
                                    src(qbundle.arts["customers"], "Clinics"),
                                    icp_accounts.INDUSTRIES[2][2])
    search = FakeSearch(query=["https://k.example/"],
                        similar={"https://saffron.example/": ["https://s1.example/x",
                                                              "https://saffron.example/"]})
    search.budget = Budget(max_fetches=icp_accounts.SEARCHES)
    found = asyncio.run(icp_accounts.find_candidates(search, d, ["saffron.example"],
                                                     {"saffron.example"}, set()))
    assert search.calls == ["similar:https://saffron.example/", "query:healthcare companies"]
    assert found.candidates == ["s1.example", "k.example"] and found.searches == 2
    tav = FakeSearch(query=["https://k.example/"], unsupported=True)
    tav.budget = Budget(max_fetches=3)
    found = asyncio.run(icp_accounts.find_candidates(tav, d, ["saffron.example"], set(), set()))
    assert tav.calls == ["query:healthcare companies"] and found.searches == 1


def test_icp_open_role_from_the_candidates_own_board(conn, cache, qbundle, sender) -> None:
    sites = FakeSites()
    sites.route("hiring.example", "/", page(
        '<main><p>Clinics</p><a href="https://boards.greenhouse.io/hiringco">Jobs</a></main>'))
    sites.route("boards-api.greenhouse.io", "/v1/boards/hiringco/jobs?content=true", json.dumps(
        {"jobs": [
            {"id": 1, "title": "Backend Engineer", "updated_at": "2026-11-02T10:00:00Z"},
            {"id": 2, "title": "Head of Sales", "updated_at": "2026-11-01T10:00:00Z",
             "location": {"name": "Dubai"}},
            {"id": 3, "title": "Sales Manager", "updated_at": "2025-01-01T10:00:00Z"}]}),
        headers={"content-type": "application/json"})
    rt = runtime(cache, sites)
    sales = next(f for f in icp_accounts.FUNCTIONS if f[0] == "sales")
    d = icp_accounts.Descriptor(function=icp_accounts.Field(
        "sales", "sales", src(qbundle.arts["homepage"], "Book a demo"), sales[2]),
        function_title=sales[3])

    async def go():
        g = rt.make_guard(Budget(max_fetches=10))
        try:
            home, body = await C.fetch_page(g, cache, company_id=QID, slice_kind="icp_accounts",
                                            subject="hiring.example", kind="homepage",
                                            url="https://hiring.example/")
            return await icp_accounts.open_role(rt, g, QID, "hiring.example", body, home, d,
                                                conn)
        finally:
            await g.aclose()

    reason, when, arts = asyncio.run(go())
    assert reason.text == "open role: Head of Sales" and when.text == "2026-11-01"
    assert reason.src.quote == "Head of Sales" and reason.src.quote in arts["ats_board"].text


def test_icp_descriptor_needs_an_industry_or_size(conn, qbundle) -> None:
    arts = {k: a for k, a in qbundle.arts.items() if k not in ("customers", "homepage")}
    b = Bundle(QID, Q_DOMAIN, "Quorvane", arts)
    d, why = icp_accounts.descriptor(conn, b, "G1")
    assert why and why.startswith("no ICP descriptor")


# -- teardown init pre-fills the stored slice --------------------------------------------


def test_teardown_init_prefills_the_selected_slice(conn, cache, diagnose, tmp_path) -> None:
    diagnose("G6")
    build(conn, runtime(cache))
    p = T.init(conn, QID, directory=tmp_path / "td", today=TODAY)
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    st = S.selected(conn, QID)
    assert doc["slice"]["title"] == st.title and doc["slice"]["columns"] == st.columns
    assert [r["cells"] for r in doc["slice"]["rows"]] == [r["cells"] for r in st.rows]
    assert all(r["url"] == f"dns:{Q_DOMAIN}" and r["quote"] for r in doc["slice"]["rows"])
    chk = T.check(conn, QID, directory=tmp_path / "td", sender_file=tmp_path / "none.yaml")
    assert not any(f.rule in ("slice_row", "quote_length") for f in chk.failures)


# -- search_api: the two slice searches --------------------------------------------------


def test_search_api_query_and_similar_through_a_mock_transport(monkeypatch, tmp_path) -> None:
    from engine.guard.cache import Cache

    seen: list[tuple[str, dict]] = []

    def exa(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"results": [{"url": "https://a.example/"}]})

    monkeypatch.setattr(search_api, "_last_call", {})
    monkeypatch.setenv(search_api.EXA_ENV, "test-key")
    monkeypatch.delenv(search_api.TAVILY_ENV, raising=False)
    monkeypatch.setattr(search_api, "transport", httpx.MockTransport(exa))
    c, b = Cache(tmp_path / "cache"), Budget(max_fetches=2)
    assert asyncio.run(search_api.search_query("clinics in the gulf", num_results=10, cache=c,
                                               budget=b)) == ["https://a.example/"]
    assert asyncio.run(search_api.search_similar("https://c.example/", cache=c,
                                                 budget=b)) == ["https://a.example/"]
    assert [u for u, _ in seen] == [search_api.EXA_URL, search_api.EXA_SIMILAR_URL]
    assert seen[0][1]["numResults"] == 10 and seen[1][1]["url"] == "https://c.example/"
    assert asyncio.run(search_api.search_query("clinics in the gulf", num_results=10, cache=c,
                                               budget=b)) == ["https://a.example/"]
    assert len(seen) == 2 and b.fetches == 2  # same day: from the cache

    monkeypatch.delenv(search_api.EXA_ENV)
    monkeypatch.setenv(search_api.TAVILY_ENV, "test-key")
    with pytest.raises(search_api.Unsupported):
        asyncio.run(search_api.search_similar("https://c.example/", cache=c))
    assert len(seen) == 2
    monkeypatch.delenv(search_api.TAVILY_ENV)
    with pytest.raises(search_api.NotConfigured):
        C.GuardSearcher(c, Budget())


# -- the CLI -----------------------------------------------------------------------------


def test_cli_build_show_and_competitors(conn, db_file, cache, diagnose,
                                        monkeypatch) -> None:
    diagnose("G2")
    monkeypatch.delenv(search_api.EXA_ENV, raising=False)
    monkeypatch.delenv(search_api.TAVILY_ENV, raising=False)
    runner = CliRunner()
    args = ["--db", str(db_file), "--cache-dir", str(cache.root)]

    dry = runner.invoke(main, ["slice", "build", QID, "--dry-run", *args])
    assert dry.exit_code == 0, dry.output
    assert "dry run, nothing written" in dry.output and S.slices(conn, QID) == []

    out = runner.invoke(main, ["slice", "build", QID, *args])
    assert out.exit_code == 0, out.output
    assert "selected role_funnel" in out.output and "model calls 0" in out.output

    no = runner.invoke(main, ["slice", "competitors", QID, "--set", SETS[0], "--set", SETS[1],
                              *args], input="n\n")
    assert no.exit_code == 1 and "not confirmed; nothing captured" in no.output
    assert not any(c.confirmed for c in S.competitors(conn, QID))
    yes = runner.invoke(main, ["slice", "competitors", QID, "--yes", *args])
    assert yes.exit_code == 0, yes.output
    assert all(c.confirmed for c in S.competitors(conn, QID))

    show = runner.invoke(main, ["slice", "show", QID, "--db", str(db_file)])
    assert show.exit_code == 0
    assert f"role_funnel {role_funnel.VERSION} [selected]: ok" in show.output
    assert "funnel_compare" in show.output and "url: https://" in show.output
    assert "1. Routely routely.example [named_competitors] confirmed" in show.output


def test_cli_build_pilot_selects_diagnosed_pilot_companies(conn, db_file, cache,
                                                           diagnose) -> None:
    diagnose("G6")
    out = CliRunner().invoke(main, ["slice", "build", "--pilot", "--db", str(db_file),
                                    "--cache-dir", str(cache.root)])
    assert out.exit_code == 0, out.output
    assert f"{QID}: selected" in out.output and BID not in out.output  # BID not diagnosed


def test_builders_import_no_model_gateway() -> None:
    root = Path(__file__).parents[1] / "engine" / "build" / "slices"
    for f in root.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "gateway" not in text and "engine.llm" not in text, f.name

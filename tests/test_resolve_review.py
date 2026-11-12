"""Step 30: the human review (engine/resolve/review.py, `te resolve review`).

Companies are invented. Nothing here touches the network or a model.
"""

from __future__ import annotations

import socket
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine import db
from engine.cli import main
from engine.guard.cache import Cache, CacheEntry
from engine.models import CompanyFlag, Signal
from engine.resolve import review
from engine.resolve.review import Decision
from tests.conftest import add_company


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a: object, **k: object) -> None:
        raise AssertionError("review must not open a socket")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


EVIDENCE = [{"marker": "entity:pvt_ltd", "side": "india", "kind": "entity",
             "quote": "(c) 2026 Zentrico Software Private Limited",
             "url": "https://zentrico.example/", "char_start": 10}]


def co(conn: sqlite3.Connection, cid: str, *, segment: str | None = "in_saas",
       status: str = "resolved", name: str = "Zentrico", **kw: object) -> str:
    hq = {"in_saas": "india", "us_india_team": "abroad"}.get(segment or "")
    return add_company(conn, cid, name=name, segment=segment, status=status, hq=hq, **kw)


_n = 0


def sig(conn: sqlite3.Connection, cid: str, *, title: str = "Founding GTM Engineer",
        family: str = "gtm_engineer", offer: str = "candidate", fresh: bool = True,
        india: bool = True, description: str | None = None) -> str:
    global _n
    _n += 1
    sid = f"greenhouse:{cid}:{_n:03d}"
    with conn:
        db.insert(conn, Signal(signal_id=sid, source="greenhouse", board_slug=cid,
                               company_name_raw="Zentrico", title=title, role_family=family,
                               offer_type=offer, location="Bengaluru, India",
                               posted_at=date(2026, 11, 1), first_seen=date(2026, 11, 2),
                               url=f"https://job-boards.greenhouse.io/{cid}/jobs/{_n}",
                               company_id=cid, description_text=description, fresh=fresh,
                               india_located=india))
    return sid


def row(conn: sqlite3.Connection, cid: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM companies WHERE company_id = ?", (cid,)).fetchone()


def cached_homepage(tmp_path: Path, url: str, html: str) -> Cache:
    cache = Cache(tmp_path / "cache")
    sha = cache.put_body(html.encode())
    cache.record(CacheEntry(url=url, final_url=url, kind="homepage", status=200, outcome="ok",
                            sha256=sha, fetched_at=datetime(2026, 11, 10, tzinfo=UTC)))
    return cache


# -- render -------------------------------------------------------------------------------


def test_render_shows_everything(conn: sqlite3.Connection, tmp_path: Path) -> None:
    co(conn, "zentrico-example", domain="zentrico.example", hq_basis="entity",
       hq_evidence=EVIDENCE, review_reason="name_mismatch: homepage title 'Zen' scored 62 < 85")
    sig(conn, "zentrico-example")
    cache = cached_homepage(tmp_path, "https://zentrico.example/",
                            "<html><head><title>Zentrico | Billing for SMBs</title></head></html>")
    out = review.render_company(conn, "zentrico-example", cache=cache)
    assert "Zentrico  [zentrico-example]" in out
    assert "domain     zentrico.example" in out
    assert "segment    in_saas" in out
    assert "hq         india  basis entity" in out
    assert '"(c) 2026 Zentrico Software Private Limited"' in out
    assert "name_mismatch: homepage title 'Zen' scored 62 < 85" in out
    assert "Founding GTM Engineer | gtm_engineer | candidate | Bengaluru, India | fresh yes" in out
    assert "https://job-boards.greenhouse.io/zentrico-example/jobs/" in out
    assert '"Zentrico | Billing for SMBs"' in out and "cached 2026-11-10" in out
    assert "duplicates (none flagged)" in out


def test_render_homepage_not_cached_never_fetches(conn: sqlite3.Connection,
                                                  tmp_path: Path) -> None:
    co(conn, "zentrico-example", domain="zentrico.example")
    out = review.render_company(conn, "zentrico-example", cache=Cache(tmp_path / "empty"))
    assert "(not in Guard's cache)" in out


def test_render_shows_duplicate_flags(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", domain="zentrico.example")
    co(conn, "zentrico-in", domain="zentrico.in", status="sourced", name="Zentrico India")
    with conn:
        db.insert(conn, CompanyFlag(company_id="zentrico-example", other_company_id="zentrico-in",
                                    kind="same_stem", score=95, detail="same stem zentrico"))
    for cid, other in (("zentrico-example", "zentrico-in"), ("zentrico-in", "zentrico-example")):
        out = review.render_company(conn, cid)
        assert "duplicates 1" in out
        assert f"same_stem with {other}" in out and "same stem zentrico" in out


def test_render_marks_best_signal(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", domain="zentrico.example", status="qualified")
    s = sig(conn, "zentrico-example")
    with conn:
        conn.execute("UPDATE companies SET best_signal_id = ?", (s,))
    assert " *Founding GTM Engineer" in review.render_company(conn, "zentrico-example")


# -- queue --------------------------------------------------------------------------------


def test_select_default_and_only_review(conn: sqlite3.Connection) -> None:
    co(conn, "a-example", domain="a.example", review_reason="segment_unclear: x")
    co(conn, "b-example", domain="b.example", status="qualified")
    co(conn, "c-example", domain="c.example", status="qualified", reviewed_at="2026-11-12")
    co(conn, "d-example", domain="d.example", status="dropped", drop_reason="consumer",
       drop_stage="review", review_reason="qualify_hold: consumer_markers: x")
    co(conn, "e-example", domain="e.example", status="sent", hand=True)
    co(conn, "f-example", domain="f.example", status="resolved")
    assert review.select(conn) == ["a-example", "b-example"]
    assert review.select(conn, only_review=True) == ["a-example"]
    assert review.select(conn, limit=1) == ["a-example"]
    assert review.select(conn, company_id="f-example") == ["f-example"]
    with pytest.raises(review.ReviewError):
        review.select(conn, company_id="nope")


# -- decisions ------------------------------------------------------------------------------


def test_accept_qualify_hold_overrides_staffing_name(conn: sqlite3.Connection) -> None:
    co(conn, "hirewise-example", name="Hirewise Recruitment Cloud", domain="hirewise.example",
       review_reason="qualify_hold: staffing_or_agency: staffing_name: ...")
    s = sig(conn, "hirewise-example")
    res = review.apply_decision(conn, "hirewise-example", Decision("accept", headcount=40))
    assert res.ok and res.status == "qualified", res.message
    r = row(conn, "hirewise-example")
    assert r["status"] == "qualified" and r["best_signal_id"] == s
    assert r["review_reason"] is None and r["reviewed_at"] is not None
    assert r["headcount"] == 40
    assert "staffing_name" in r["review_note"]


def test_accept_still_held_stays_resolved(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", domain="zentrico.example",
       review_reason="segment_unclear: no_evidence: nothing found")
    sig(conn, "zentrico-example", fresh=False)
    res = review.apply_decision(conn, "zentrico-example", Decision("accept"))
    assert res.ok and res.status == "resolved"
    r = row(conn, "zentrico-example")
    assert r["status"] == "resolved" and r["reviewed_at"] is None
    assert r["review_reason"].startswith("qualify_hold: no_fresh_gtm_signal")


def test_accept_refused_when_rule_drops(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", domain="zentrico.example",
       review_reason="segment_unclear: x")
    sig(conn, "zentrico-example", description="We are hiring on behalf of our client, a fintech.")
    res = review.apply_decision(conn, "zentrico-example", Decision("accept"))
    assert not res.ok and "staffing_or_agency" in res.message
    r = row(conn, "zentrico-example")
    assert r["status"] == "resolved" and r["review_reason"] == "segment_unclear: x"


def test_accept_needs_segment(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", domain="zentrico.example", segment=None,
       review_reason="segment_unclear: conflicting_entities: x")
    sig(conn, "zentrico-example")
    res = review.apply_decision(conn, "zentrico-example", Decision("accept"))
    assert not res.ok and "press s" in res.message
    assert review.apply_decision(conn, "zentrico-example",
                                 Decision("segment", segment="in_saas")).ok
    assert row(conn, "zentrico-example")["segment"] == "in_saas"
    res = review.apply_decision(conn, "zentrico-example", Decision("accept"))
    assert res.ok and row(conn, "zentrico-example")["status"] == "qualified"


def test_accept_sourced_domain_review(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", domain="zentrico.example", status="sourced", segment=None,
       review_reason="no_linkback: careers page not found")
    res = review.apply_decision(conn, "zentrico-example", Decision("accept"))
    assert res.ok and res.status == "resolved" and "te resolve segment" in res.message
    r = row(conn, "zentrico-example")
    assert r["status"] == "resolved" and r["review_reason"] is None


def test_accept_placeholder_refused(conn: sqlite3.Connection) -> None:
    co(conn, "unresolved-zentrico", domain=None, status="sourced", segment=None,
       review_reason="no_candidate: nothing")
    res = review.apply_decision(conn, "unresolved-zentrico", Decision("accept"))
    assert not res.ok and row(conn, "unresolved-zentrico")["status"] == "sourced"


def test_accept_qualified_marks_reviewed(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", domain="zentrico.example", status="qualified")
    s = sig(conn, "zentrico-example")
    res = review.apply_decision(conn, "zentrico-example", Decision("accept"))
    assert res.ok
    r = row(conn, "zentrico-example")
    assert r["reviewed_at"] is not None and r["best_signal_id"] == s
    assert review.select(conn) == []


@pytest.mark.parametrize("reason", review.DROP_REASONS)
def test_drop_with_each_reason(conn: sqlite3.Connection, reason: str) -> None:
    co(conn, "zentrico-example", domain="zentrico.example", status="qualified")
    res = review.apply_decision(conn, "zentrico-example",
                                Decision("drop", reason=reason, note="checked the site"))
    assert res.ok
    r = row(conn, "zentrico-example")
    assert (r["status"], r["drop_reason"], r["drop_stage"]) == ("dropped", reason, "review")
    assert r["review_note"] == "checked the site" and r["reviewed_at"] is not None
    again = review.apply_decision(conn, "zentrico-example", Decision("accept"))
    assert not again.ok and "already dropped" in again.message


def test_drop_needs_a_known_reason(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", domain="zentrico.example")
    res = review.apply_decision(conn, "zentrico-example", Decision("drop", reason="meh"))
    assert not res.ok and row(conn, "zentrico-example")["status"] == "resolved"


def test_headcount_records_and_warns(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", domain="zentrico.example", status="qualified")
    res = review.apply_decision(conn, "zentrico-example", Decision("headcount", headcount=80))
    assert res.ok and not res.warnings and row(conn, "zentrico-example")["headcount"] == 80
    res = review.apply_decision(conn, "zentrico-example", Decision("headcount", headcount=4000))
    assert res.ok and "too_large" in res.warnings[0]
    r = row(conn, "zentrico-example")
    assert r["headcount"] == 4000 and r["status"] == "qualified"  # never dropped by code


def test_segment_edit_rejects_other_values(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", domain="zentrico.example")
    assert not review.apply_decision(conn, "zentrico-example",
                                     Decision("segment", segment="b2c")).ok
    res = review.apply_decision(conn, "zentrico-example",
                                Decision("segment", segment="us_india_team"))
    assert res.ok and row(conn, "zentrico-example")["segment"] == "us_india_team"


def test_hand_company_refused(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", domain="zentrico.example", status="sent", hand=True)
    assert not review.apply_decision(conn, "zentrico-example", Decision("accept")).ok


# -- the key loop ---------------------------------------------------------------------------


def test_cli_loop(db_file: Path, tmp_path: Path) -> None:
    conn = db.connect(db_file)
    co(conn, "a-example", domain="a.example", segment=None,
       review_reason="segment_unclear: no_evidence: x")
    sig(conn, "a-example")
    co(conn, "b-example", domain="b.example", status="qualified")
    sig(conn, "b-example")
    co(conn, "c-example", domain="c.example", status="qualified")
    conn.close()
    # a: set segment, headcount, accept. b: drop too_large with a note. c: skip.
    keys = "s\nin_saas\nh\n120\na\nd\ntoo_large\nabout 5000 people\nn\n"
    r = CliRunner().invoke(main, ["resolve", "review", "--db", str(db_file),
                                  "--cache-dir", str(tmp_path / "c")], input=keys)
    assert r.exit_code == 0, r.output
    assert "segment_unclear: no_evidence: x" in r.output
    assert "ok: accepted: qualified" in r.output
    assert "ok: dropped: too_large (about 5000 people)" in r.output
    assert "accepted 1, held 0, dropped 1, skipped 1" in r.output
    conn = db.connect(db_file)
    a, b, c = (row(conn, x) for x in ("a-example", "b-example", "c-example"))
    assert (a["status"], a["segment"], a["headcount"]) == ("qualified", "in_saas", 120)
    assert (b["status"], b["drop_reason"], b["drop_stage"]) == ("dropped", "too_large", "review")
    assert c["status"] == "qualified" and c["reviewed_at"] is None
    conn.close()


def test_cli_loop_quit_and_too_large_confirm(db_file: Path, tmp_path: Path) -> None:
    conn = db.connect(db_file)
    co(conn, "a-example", domain="a.example", status="qualified", headcount=3000)
    sig(conn, "a-example")
    co(conn, "b-example", domain="b.example", status="qualified")
    conn.close()
    r = CliRunner().invoke(main, ["resolve", "review", "--db", str(db_file),
                                  "--cache-dir", str(tmp_path / "c")], input="a\nn\nq\n")
    assert r.exit_code == 0, r.output
    assert "accept anyway?" in r.output and "[2/2]" not in r.output
    conn = db.connect(db_file)
    assert row(conn, "a-example")["reviewed_at"] is None
    conn.close()


def test_cli_dry_run_and_only_review(db_file: Path, tmp_path: Path) -> None:
    conn = db.connect(db_file)
    co(conn, "a-example", domain="a.example", review_reason="segment_unclear: x")
    co(conn, "b-example", domain="b.example", status="qualified")
    conn.close()
    r = CliRunner().invoke(main, ["resolve", "review", "--db", str(db_file), "--dry-run",
                                  "--only-review", "--cache-dir", str(tmp_path / "c")])
    assert r.exit_code == 0, r.output
    assert "[1/1]" in r.output and "a-example" in r.output and "b-example" not in r.output
    r = CliRunner().invoke(main, ["resolve", "review", "--db", str(db_file), "--company",
                                  "zz-example"])
    assert r.exit_code == 1


def test_module_has_no_network_or_model_imports() -> None:
    src = Path(review.__file__).read_text(encoding="utf-8")
    assert "engine.llm" not in src and "guard.http" not in src and "httpx" not in src

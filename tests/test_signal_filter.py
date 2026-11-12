"""Step 25: freshness and India location (engine/signal/filter.py). Invented data only."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine import db
from engine.cli import main
from engine.models import Signal
from engine.signal import careers
from engine.signal import filter as sigfilter
from engine.signal.boards import store

TODAY = date(2026, 11, 12)


def sig(n: int, *, source: str = "greenhouse", posted_days: int | None = None,
        seen_days: int = 0, location: str | None = None, role_family: str | None = None,
        offer_type: str | None = None, url: str | None = "https://zentrico.example/jobs/1",
        ) -> Signal:
    return Signal(
        signal_id=f"t-{n:03d}",
        source=source,
        company_name_raw="Zentrico",
        title="Account Executive",
        role_family=role_family,
        offer_type=offer_type,
        location=location,
        posted_at=None if posted_days is None else TODAY - timedelta(days=posted_days),
        url=url,
        first_seen=TODAY - timedelta(days=seen_days),
    )


def flags(conn: sqlite3.Connection) -> dict[str, tuple[int | None, int | None]]:
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT signal_id, fresh, india_located FROM signals ORDER BY signal_id")}


# -- freshness ----------------------------------------------------------------------


@pytest.mark.parametrize(("days", "fresh"), [(0, True), (59, True), (60, True), (61, False)])
def test_window_edges_on_posted_at(days: int, fresh: bool) -> None:
    assert sigfilter.is_fresh(TODAY - timedelta(days=days), TODAY, TODAY) is fresh


@pytest.mark.parametrize(("days", "fresh"), [(59, True), (60, True), (61, False)])
def test_null_posted_at_uses_first_seen(days: int, fresh: bool) -> None:
    assert sigfilter.is_fresh(None, TODAY - timedelta(days=days), TODAY) is fresh


def test_posted_at_wins_over_a_recent_first_seen() -> None:
    assert not sigfilter.is_fresh(TODAY - timedelta(days=90), TODAY, TODAY)


def test_future_date_counts_as_fresh() -> None:
    assert sigfilter.is_fresh(TODAY + timedelta(days=1), TODAY, TODAY)


def test_window_is_injectable() -> None:
    posted = TODAY - timedelta(days=10)
    assert sigfilter.is_fresh(posted, TODAY, TODAY, window_days=10)
    assert not sigfilter.is_fresh(posted, TODAY, TODAY, window_days=9)


# -- India location -----------------------------------------------------------------


@pytest.mark.parametrize("location", [
    "Bengaluru", "Bangalore, Karnataka", "bengaluru", "HYDERABAD", "Pune, MH, IN",
    "Gurugram, Haryana", "Gurgaon", "Mumbai", "Chennai, Tamil Nadu", "Noida",
    "New Delhi", "Delhi NCR", "NCR", "Delhi/NCR", "Remote India", "Remote - India",
    "India", "Remote (India)", "India; Remote", "San Francisco, CA; Bengaluru",
    "London | Pune", "IN", "Remote - IN", "Remote (IN)", "remote, IN", "IN - Remote",
    "New York; IN",
])
def test_india_located(location: str) -> None:
    assert sigfilter.is_india_located(location) is True


@pytest.mark.parametrize("location", [
    None, "", "   ", "Remote", "Remote - US", "Remote, Worldwide", "Indiana",
    "Indianapolis, IN", "Carmel, IN", "Fort Wayne, Indiana", "Punel", "Mumbaikar Street",
    "in", "Remote in EMEA", "Indian Wells, CA", "Chennaix", "Singapore", "Dubai, UAE",
    "INDIANAPOLIS", "Delhiwala Road",
])
def test_not_india_located(location: str | None) -> None:
    assert sigfilter.is_india_located(location) is False


# -- run over the table -------------------------------------------------------------


def seed_rows(conn: sqlite3.Connection) -> None:
    store(conn, [
        # fresh GTM, ATS, India
        sig(1, posted_days=5, location="Bengaluru", role_family="ae", offer_type="adjacent"),
        # stale by posted_at although first seen today
        sig(2, posted_days=61, location="Remote", role_family="sdr", offer_type="adjacent"),
        # careers: no post date, first_seen 60 days ago -> fresh, seed-list GTM
        sig(3, source="careers", seen_days=60, location="Pune, MH, IN",
            role_family="revops", offer_type="candidate"),
        # careers, first seen 61 days ago -> stale
        sig(4, source="careers", seen_days=61, location=None, role_family="growth",
            offer_type="candidate"),
        # fresh but not GTM
        sig(5, posted_days=1, location="Carmel, IN", role_family="not_gtm"),
        # fresh, not yet classified
        sig(6, source="manual", posted_days=59, location="Remote - IN"),
        # fresh GTM with no url
        sig(7, source="manual", posted_days=0, role_family="growth", offer_type="candidate",
            url=None),
    ])


def test_run_marks_counts_and_keeps_every_row(conn: sqlite3.Connection) -> None:
    seed_rows(conn)
    rep = sigfilter.run(conn, today=TODAY)
    assert (rep.total, rep.fresh, rep.stale, rep.india_located) == (7, 5, 2, 3)
    assert (rep.fresh_gtm, rep.fresh_gtm_seed, rep.fresh_gtm_missing) == (3, 1, 1)
    assert rep.by_source == {"careers": 1, "greenhouse": 1, "manual": 1}
    assert rep.changed == 7
    assert flags(conn) == {
        "t-001": (1, 1), "t-002": (0, 0), "t-003": (1, 1), "t-004": (0, 0),
        "t-005": (1, 0), "t-006": (1, 1), "t-007": (1, 0),
    }


def test_run_is_idempotent(conn: sqlite3.Connection) -> None:
    seed_rows(conn)
    sigfilter.run(conn, today=TODAY)
    before = conn.execute("SELECT * FROM signals ORDER BY signal_id").fetchall()
    again = sigfilter.run(conn, today=TODAY)
    assert again.changed == 0
    after = conn.execute("SELECT * FROM signals ORDER BY signal_id").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_later_run_date_turns_fresh_to_stale_never_back(conn: sqlite3.Connection) -> None:
    seed_rows(conn)
    sigfilter.run(conn, today=TODAY)
    rep = sigfilter.run(conn, today=TODAY + timedelta(days=2))
    # t-003 (60 days) and t-006 (59 days) age past the window
    assert rep.changed == 2
    assert flags(conn)["t-003"] == (0, 1) and flags(conn)["t-006"] == (0, 1)
    # a re-collect of the same post inserts nothing and leaves its dates alone
    assert store(conn, [sig(3, source="careers", seen_days=0, location="Pune")]) == 0
    assert sigfilter.run(conn, today=TODAY + timedelta(days=2)).changed == 0


def test_dry_run_writes_nothing(conn: sqlite3.Connection) -> None:
    seed_rows(conn)
    rep = sigfilter.run(conn, today=TODAY, dry_run=True)
    assert rep.changed == 7 and rep.fresh == 5
    assert set(flags(conn).values()) == {(None, None)}


def test_limit_and_window(conn: sqlite3.Connection) -> None:
    seed_rows(conn)
    rep = sigfilter.run(conn, today=TODAY, limit=2, window_days=61)
    assert (rep.total, rep.fresh, rep.changed) == (2, 2, 2)
    assert flags(conn)["t-003"] == (None, None)


# -- careers JSON-LD datePosted ------------------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [
    ('"2026-10-30"', date(2026, 10, 30)),
    ('"2026-10-30T09:00:00Z"', date(2026, 10, 30)),
    ('"30 October 2026"', None),
    ("null", None),
])
def test_jsonld_date_posted(value: str, expected: date | None) -> None:
    markup = ('<script type="application/ld+json">{"@type": "JobPosting", '
              f'"title": "Founding SDR", "url": "/jobs/sdr", "datePosted": {value}}}</script>')
    found = careers.read_page(markup, "https://kelvori.example/careers")
    assert [(f.title, f.posted_at) for f in found] == [("Founding SDR", expected)]


# -- CLI ----------------------------------------------------------------------------


def test_cli_filter_counts_dry_run_and_rerun(db_file: Path) -> None:
    conn = db.connect(db_file)
    seed_rows(conn)
    conn.close()
    runner = CliRunner()
    args = ["signal", "filter", "--db", str(db_file), "--today", TODAY.isoformat()]

    dry = runner.invoke(main, args + ["--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "signals 7: fresh 5, stale 2, india_located 3; would change 7" in dry.output

    first = runner.invoke(main, args)
    assert first.exit_code == 0, first.output
    assert "signals 7: fresh 5, stale 2, india_located 3; changed 7" in first.output
    assert "fresh GTM signals 3, from seed-list companies 1 (source careers)" in first.output
    assert "fresh GTM by source: careers 1, greenhouse 1, manual 1" in first.output
    assert "missing a url, offer type or company name 1" in first.output
    assert "hand-off is not recorded" in first.output

    second = runner.invoke(main, args)
    assert second.exit_code == 0, second.output
    assert "changed 0" in second.output

    limited = runner.invoke(main, args + ["--limit", "1"])
    assert "signals 1: fresh 1, stale 0, india_located 1; changed 0" in limited.output


def test_cli_filter_without_db_fails(tmp_path: Path) -> None:
    r = CliRunner().invoke(main, ["signal", "filter", "--db", str(tmp_path / "none.db")])
    assert r.exit_code == 1

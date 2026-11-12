"""Step 27: dedupe (engine/resolve/dedupe.py).

Companies are invented. Nothing here touches the network (no Guard) or a model.
"""

from __future__ import annotations

import socket
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine import db
from engine.cli import main
from engine.models import CompanyFlag, Signal
from engine.resolve import dedupe
from tests.conftest import add_company


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a: object, **k: object) -> None:
        raise AssertionError("dedupe must not open a socket")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def co(conn: sqlite3.Connection, cid: str, name: str, domain: str | None) -> str:
    return add_company(conn, cid, name=name, domain=domain)


_n = 0


def sig(conn: sqlite3.Connection, company_id: str | None, *, source: str = "greenhouse",
        slug: str = "zentrico", name: str = "Zentrico") -> str:
    global _n
    _n += 1
    sid = f"{source}:{slug}:{_n}"
    with conn:
        db.insert(conn, Signal(signal_id=sid, source=source, board_slug=slug,
                               company_name_raw=name, title="Account Executive",
                               first_seen=date(2026, 11, 1), company_id=company_id))
    return sid


def flags(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [tuple(r) for r in conn.execute(
        "SELECT company_id, other_company_id, kind FROM company_flags ORDER BY 1, 2, 3")]


# -- possible duplicates -----------------------------------------------------------


def test_same_stem_different_suffix_is_flagged(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-com", "Zentrico", "zentrico.com")
    co(conn, "zentrico-in", "Zentrico India", "zentrico.in")
    co(conn, "zentrico-co-in", "Zentrico Technologies", "zentrico.co.in")
    rep = dedupe.run(conn)
    assert rep.ok
    assert flags(conn) == [
        ("zentrico-co-in", "zentrico-com", "same_stem"),
        ("zentrico-co-in", "zentrico-in", "same_stem"),
        ("zentrico-com", "zentrico-in", "same_stem"),
    ]
    assert all("zentrico" in f.detail for f in rep.flags)


def test_same_stem_flagged_even_when_names_differ(conn: sqlite3.Connection) -> None:
    co(conn, "kelvori-io", "Kelvori", "kelvori.io")
    co(conn, "kelvori-example", "Northwind Ledger", "kelvori.example")
    rep = dedupe.run(conn)
    assert [(f.kind, f.score < 90) for f in rep.flags] == [("same_stem", True)]


def test_near_identical_names_on_different_domains_flagged(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", "Zentrico Pvt Ltd", "zentrico.example")
    co(conn, "getzentrico-example", "Zentrico, Inc.", "getzentrico.example")
    rep = dedupe.run(conn)
    assert flags(conn) == [("getzentrico-example", "zentrico-example", "similar_name")]
    assert rep.flags[0].score == 100


def test_different_names_not_flagged(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", "Zentrico", "zentrico.example")
    co(conn, "kelvori-example", "Kelvori", "kelvori.example")
    co(conn, "orbanta-example", "Orbanta Health", "orbanta.example")
    rep = dedupe.run(conn)
    assert rep.flags == [] and flags(conn) == []


def test_threshold_edges() -> None:
    def pair(x: str, y: str) -> dedupe.Flag | None:
        a = dedupe._Co("a-example", "a.example", x, dedupe.normalise_name(x))
        b = dedupe._Co("b-example", "b.example", y, dedupe.normalise_name(y))
        return dedupe.compare(a, b)

    # 2 * 9 / (10 + 10) = 0.90 exactly: flagged at the threshold.
    assert dedupe.name_similarity("abcdefghij", "abcdefghik") == 90
    assert pair("abcdefghij", "abcdefghik") is not None
    # 2 * 8 / (9 + 9) = 0.889: just under, not flagged.
    assert dedupe.name_similarity("abcdefghi", "abcdefghk") < 90
    assert pair("abcdefghi", "abcdefghk") is None
    # Legal words and punctuation never count.
    assert pair("Zentrico Pvt. Ltd.", "ZENTRICO") is not None
    # Same suffix, different label: names decide.
    assert pair("Zentrico", "Kelvori") is None


def test_placeholder_matching_resolved_company_flagged_not_merged(
    conn: sqlite3.Connection,
) -> None:
    co(conn, "zentrico-example", "Zentrico", "zentrico.example")
    co(conn, "unresolved-manual-zentrico", "Zentrico Pvt Ltd", None)
    s_ph = sig(conn, "unresolved-manual-zentrico", source="manual", slug="")
    s_res = sig(conn, "zentrico-example")
    rep = dedupe.run(conn)
    assert rep.ok
    assert flags(conn) == [("unresolved-manual-zentrico", "zentrico-example",
                            "placeholder_name")]
    assert "not merged" in rep.flags[0].detail
    # Nothing moved, nothing deleted.
    owner = dict(conn.execute("SELECT signal_id, company_id FROM signals").fetchall())
    assert owner == {s_ph: "unresolved-manual-zentrico", s_res: "zentrico-example"}
    assert conn.execute("SELECT count(*) FROM companies").fetchone()[0] == 2


def test_flag_drops_with_its_placeholder(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", "Zentrico", "zentrico.example")
    co(conn, "unresolved-manual-zentrico", "Zentrico", None)
    dedupe.run(conn)
    with conn:  # what Step 26 does once a placeholder's signals have moved
        conn.execute("DELETE FROM companies WHERE company_id = 'unresolved-manual-zentrico'")
    assert flags(conn) == []


def test_rerun_is_idempotent(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-com", "Zentrico", "zentrico.com")
    co(conn, "zentrico-in", "Zentrico", "zentrico.in")
    co(conn, "unresolved-manual-kelvori", "Kelvori", None)
    co(conn, "kelvori-example", "Kelvori", "kelvori.example")
    first = dedupe.run(conn)
    before = conn.execute("SELECT * FROM company_flags ORDER BY flag_id").fetchall()
    second = dedupe.run(conn)
    after = conn.execute("SELECT * FROM company_flags ORDER BY flag_id").fetchall()
    assert first.new_flags == 2 and second.new_flags == 0
    assert len(second.flags) == 2
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_dry_run_writes_nothing(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-com", "Zentrico", "zentrico.com")
    co(conn, "zentrico-in", "Zentrico", "zentrico.in")
    rep = dedupe.run(conn, dry_run=True)
    assert rep.new_flags == 1 and flags(conn) == []


def test_company_option_limits_flags(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-com", "Zentrico", "zentrico.com")
    co(conn, "zentrico-in", "Zentrico", "zentrico.in")
    co(conn, "kelvori-com", "Kelvori", "kelvori.com")
    co(conn, "kelvori-in", "Kelvori", "kelvori.in")
    rep = dedupe.run(conn, company_id="kelvori-in")
    assert flags(conn) == [("kelvori-com", "kelvori-in", "same_stem")]
    assert len(rep.flags) == 1


def test_flag_model_orders_pair() -> None:
    with pytest.raises(ValueError):
        CompanyFlag(company_id="b", other_company_id="a", kind="same_stem", score=90, detail="x")


# -- the invariant -----------------------------------------------------------------


def test_clean_database_passes(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", "Zentrico", "zentrico.example")
    sig(conn, "zentrico-example")
    sig(conn, "zentrico-example")
    sig(conn, None, slug="kelvori", name="Kelvori")  # not yet resolved: fine
    assert dedupe.check_invariant(conn) == []


def test_unnormalised_and_duplicate_domain_reported(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", "Zentrico", "zentrico.example")
    co(conn, "www-zentrico-example", "Zentrico", "www.zentrico.example")
    codes = [v.code for v in dedupe.check_invariant(conn)]
    assert codes == ["unnormalised_domain", "duplicate_domain"]
    assert conn.execute("SELECT count(*) FROM companies").fetchone()[0] == 2  # not merged


def test_split_board_reported(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", "Zentrico", "zentrico.example")
    co(conn, "unresolved-greenhouse-zentrico", "Zentrico", None)
    sig(conn, "zentrico-example", slug="Zentrico")
    sig(conn, "unresolved-greenhouse-zentrico", slug="zentrico")
    vs = dedupe.check_invariant(conn)
    assert [v.code for v in vs] == ["split_board"]
    assert "greenhouse:zentrico" in vs[0].detail


def test_careers_signal_on_wrong_row_reported(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-example", "Zentrico", "zentrico.example")
    co(conn, "unresolved-careers-zentrico-example", "Zentrico", None)
    sig(conn, "unresolved-careers-zentrico-example", source="careers",
        slug="www.zentrico.example")
    vs = dedupe.check_invariant(conn)
    assert [v.code for v in vs] == ["signal_elsewhere"]
    assert "zentrico.example" in vs[0].detail


def test_missing_company_reported(conn: sqlite3.Connection) -> None:
    sig(conn, None)
    conn.execute("PRAGMA foreign_keys = OFF")
    with conn:
        conn.execute("UPDATE signals SET company_id = 'ghost-example'")
    vs = dedupe.check_invariant(conn)
    assert [v.code for v in vs] == ["missing_company"]


def test_no_model_calls(conn: sqlite3.Connection) -> None:
    co(conn, "zentrico-com", "Zentrico", "zentrico.com")
    co(conn, "zentrico-in", "Zentrico", "zentrico.in")
    dedupe.run(conn)
    assert conn.execute("SELECT count(*) FROM llm_calls").fetchone()[0] == 0


def test_module_does_not_use_guard_or_llm() -> None:
    src = Path(dedupe.__file__).read_text(encoding="utf-8")
    for banned in ("engine.guard", "engine.llm", "httpx", "Guard("):
        assert banned not in src


# -- CLI ---------------------------------------------------------------------------


def test_cli_dedupe(db_file: Path) -> None:
    conn = db.connect(db_file)
    co(conn, "zentrico-com", "Zentrico", "zentrico.com")
    co(conn, "zentrico-in", "Zentrico", "zentrico.in")
    conn.close()
    runner = CliRunner()
    dry = runner.invoke(main, ["resolve", "dedupe", "--dry-run", "--db", str(db_file)])
    assert dry.exit_code == 0, dry.output
    assert "invariant ok" in dry.output
    assert "same_stem" in dry.output and "zentrico-com <-> zentrico-in" in dry.output
    assert "would add 1 new flag" in dry.output
    real = runner.invoke(main, ["resolve", "dedupe", "--db", str(db_file)])
    assert real.exit_code == 0 and "added 1 new flag" in real.output
    again = runner.invoke(main, ["resolve", "dedupe", "--db", str(db_file)])
    assert "added 0 new flag" in again.output


def test_cli_dedupe_exits_1_on_violation(db_file: Path) -> None:
    conn = db.connect(db_file)
    co(conn, "zentrico-example", "Zentrico", "zentrico.example")
    co(conn, "www-zentrico-example", "Zentrico", "www.zentrico.example")
    conn.close()
    res = CliRunner().invoke(main, ["resolve", "dedupe", "--db", str(db_file)])
    assert res.exit_code == 1
    assert "invariant broken" in res.output and "duplicate_domain" in res.output

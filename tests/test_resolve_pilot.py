"""Step 31: the pilot twelve (engine/resolve/pilot.py, `te pilot`).

Companies are invented. No network, no model.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine import db
from engine.cli import main
from engine.models import Signal
from engine.resolve import pilot
from tests.conftest import add_company


def co(conn: sqlite3.Connection, cid: str, segment: str = "in_saas", offer: str = "adjacent",
       status: str = "qualified", **kw: object) -> str:
    add_company(conn, cid, name=cid.title(), domain=f"{cid}.example", segment=segment,
                status=status, **kw)
    sid = f"greenhouse:{cid}:1"
    with conn:
        db.insert(conn, Signal(signal_id=sid, source="greenhouse", company_name_raw=cid,
                               title="Founding SDR" if offer == "candidate" else "SDR",
                               role_family="sdr", offer_type=offer, first_seen=date(2026, 11, 1),
                               company_id=cid))
        conn.execute("UPDATE companies SET best_signal_id = ? WHERE company_id = ?", (sid, cid))
    return cid


def twelve(conn: sqlite3.Connection) -> list[str]:
    ids = []
    for seg in ("in_saas", "us_india_team"):
        for i in range(6):
            ids.append(co(conn, f"{seg[:2]}{i}", seg, "candidate" if i < 2 else "adjacent"))
    return ids


def test_pick_twelve_passes_checks(conn: sqlite3.Connection) -> None:
    ids = twelve(conn)
    new, warnings = pilot.pick(conn, ids)
    assert len(new) == 12 and warnings == []
    assert pilot.check(pilot.pilots(conn)).ok
    assert pilot.pick(conn, ids[:1]) == ([], [])  # idempotent


def test_hand_company_refused(conn: sqlite3.Connection) -> None:
    co(conn, "phase0", status="sent", hand=True)
    co(conn, "fine")
    with pytest.raises(pilot.PilotError, match="Phase 0"):
        pilot.pick(conn, ["fine", "phase0"])
    assert pilot.pilots(conn) == []  # all or nothing


def test_unqualified_and_dropped_refused(conn: sqlite3.Connection) -> None:
    co(conn, "early", status="resolved")
    co(conn, "gone", status="dropped", drop_reason="too_large", drop_stage="review")
    with pytest.raises(pilot.PilotError) as e:
        pilot.pick(conn, ["early", "gone", "missing"])
    msg = str(e.value)
    assert "early: status resolved" in msg and "gone: dropped" in msg and "missing" in msg


def test_counts_warn_then_refuse_over(conn: sqlite3.Connection) -> None:
    for i in range(7):
        co(conn, f"in{i}")
    new, warnings = pilot.pick(conn, ["in0", "in1"])
    assert len(new) == 2
    assert any("2 picked; the pilot is 12" in w for w in warnings)
    assert any("0 with a candidate" in w for w in warnings)
    assert any("0 in us_india_team" in w for w in warnings)
    pilot.pick(conn, [f"in{i}" for i in range(2, 6)])
    with pytest.raises(pilot.PilotError, match="at most 6"):
        pilot.pick(conn, ["in6"])


def test_unpick(conn: sqlite3.Connection) -> None:
    co(conn, "in0")
    pilot.pick(conn, ["in0"])
    assert pilot.unpick(conn, "in0") is True
    assert pilot.unpick(conn, "in0") is False
    with pytest.raises(pilot.PilotError):
        pilot.unpick(conn, "nope")


def test_cli_pick_list_unpick(db_file: Path) -> None:
    conn = db.connect(db_file)
    ids = twelve(conn)
    co(conn, "phase0", status="sent", hand=True)
    conn.close()
    run = CliRunner().invoke
    r = run(main, ["pilot", "pick", "phase0", "--db", str(db_file)])
    assert r.exit_code == 1 and "Phase 0" in r.output
    r = run(main, ["pilot", "pick", *ids[:3], "--db", str(db_file)])
    assert r.exit_code == 0 and "picked 3" in r.output and "warning:" in r.output
    r = run(main, ["pilot", "list", "--db", str(db_file)])
    assert r.exit_code == 1 and "check: 3 picked" in r.output
    r = run(main, ["pilot", "pick", *ids, "--db", str(db_file)])
    assert r.exit_code == 0 and "picked 9" in r.output
    r = run(main, ["pilot", "list", "--db", str(db_file)])
    assert r.exit_code == 0, r.output
    assert "in_saas 6, us_india_team 6" in r.output and "candidate 4" in r.output
    assert "checks ok" in r.output
    r = run(main, ["pilot", "unpick", ids[0], "--db", str(db_file)])
    assert r.exit_code == 0 and "unpicked" in r.output

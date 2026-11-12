import sqlite3

import pytest

from engine.states import (
    PIPELINE,
    Status,
    TransitionError,
    UnknownCompany,
    advance,
    can_advance,
    drop,
)
from tests.conftest import add_company


def _status(conn: sqlite3.Connection, cid: str) -> str:
    return conn.execute("SELECT status FROM companies WHERE company_id = ?", (cid,)).fetchone()[0]


def test_pipeline_order() -> None:
    assert [s.value for s in PIPELINE] == [
        "sourced", "resolved", "qualified", "captured", "extracted", "diagnosed",
        "built", "approved", "sent", "replied", "meeting", "closed",
    ]


def test_advance_forward(conn: sqlite3.Connection) -> None:
    cid = add_company(conn)
    assert advance(cid, Status.RESOLVED, conn=conn) is True
    assert _status(conn, cid) == "resolved"


def test_advance_may_skip_forward(conn: sqlite3.Connection) -> None:
    cid = add_company(conn, status="replied")
    assert advance(cid, "closed", conn=conn) is True


def test_advance_same_status_is_noop(conn: sqlite3.Connection) -> None:
    cid = add_company(conn, status="captured")
    assert advance(cid, "captured", conn=conn) is False
    assert _status(conn, cid) == "captured"


def test_backward_transition_raises(conn: sqlite3.Connection) -> None:
    cid = add_company(conn, status="diagnosed")
    with pytest.raises(TransitionError):
        advance(cid, Status.CAPTURED, conn=conn)
    assert _status(conn, cid) == "diagnosed"


def test_advance_to_dropped_must_use_drop(conn: sqlite3.Connection) -> None:
    cid = add_company(conn)
    with pytest.raises(TransitionError):
        advance(cid, Status.DROPPED, conn=conn)


def test_drop_then_no_way_back(conn: sqlite3.Connection) -> None:
    cid = add_company(conn, status="qualified")
    assert drop(cid, "no india team", "resolve", conn=conn) is True
    row = conn.execute(
        "SELECT status, drop_reason, drop_stage FROM companies WHERE company_id = ?", (cid,)
    ).fetchone()
    assert tuple(row) == ("dropped", "no india team", "resolve")
    with pytest.raises(TransitionError):
        advance(cid, Status.CLOSED, conn=conn)
    # Same drop again is idempotent; a different one is refused.
    assert drop(cid, "no india team", "resolve", conn=conn) is False
    with pytest.raises(TransitionError):
        drop(cid, "other", "judge", conn=conn)


def test_drop_needs_reason_and_stage(conn: sqlite3.Connection) -> None:
    cid = add_company(conn)
    with pytest.raises(ValueError):
        drop(cid, " ", "resolve", conn=conn)
    with pytest.raises(ValueError):
        drop(cid, "reason", "", conn=conn)


def test_unknown_company(conn: sqlite3.Connection) -> None:
    with pytest.raises(UnknownCompany):
        advance("nobody-example", Status.RESOLVED, conn=conn)


def test_database_refuses_backward_update(conn: sqlite3.Connection) -> None:
    cid = add_company(conn, status="sent")
    with pytest.raises(sqlite3.IntegrityError, match="forward"), conn:
        conn.execute("UPDATE companies SET status = 'built' WHERE company_id = ?", (cid,))
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(
            "UPDATE companies SET status = 'dropped' WHERE company_id = ?", (cid,)
        )  # dropped without a reason


def test_database_refuses_leaving_dropped(conn: sqlite3.Connection) -> None:
    cid = add_company(conn)
    drop(cid, "duplicate", "resolve", conn=conn)
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(
            "UPDATE companies SET status = 'closed', drop_reason = NULL, drop_stage = NULL "
            "WHERE company_id = ?",
            (cid,),
        )


@pytest.mark.parametrize("cur", list(Status))
def test_can_advance_matches_rank(cur: Status) -> None:
    for to in Status:
        expected = (
            cur is not Status.DROPPED
            and to is not Status.DROPPED
            and PIPELINE.index(to) > PIPELINE.index(cur)
        )
        assert can_advance(cur, to) is expected

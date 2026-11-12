"""The company state machine.

sourced -> resolved -> qualified -> captured -> extracted -> diagnosed -> built
-> approved -> sent -> replied -> meeting -> closed; any state -> dropped.

A status only moves forward (skipping ahead is allowed), or to dropped.
dropped is terminal. advance() and drop() are the only transitions; the
database enforces the same rule with a trigger.
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum


class Status(StrEnum):
    SOURCED = "sourced"
    RESOLVED = "resolved"
    QUALIFIED = "qualified"
    CAPTURED = "captured"
    EXTRACTED = "extracted"
    DIAGNOSED = "diagnosed"
    BUILT = "built"
    APPROVED = "approved"
    SENT = "sent"
    REPLIED = "replied"
    MEETING = "meeting"
    CLOSED = "closed"
    DROPPED = "dropped"


# The forward pipeline, in order. dropped sits outside it.
PIPELINE: tuple[Status, ...] = tuple(s for s in Status if s is not Status.DROPPED)
_RANK = {s: i for i, s in enumerate(PIPELINE)}


class TransitionError(ValueError):
    """A status change the state machine does not allow."""


class UnknownCompany(KeyError):
    pass


def can_advance(current: Status | str, to_status: Status | str) -> bool:
    """True if moving current -> to_status is a strictly forward pipeline move."""
    cur, to = Status(current), Status(to_status)
    if cur is Status.DROPPED or to is Status.DROPPED:
        return False
    return _RANK[to] > _RANK[cur]


def _conn(conn: sqlite3.Connection | None) -> tuple[sqlite3.Connection, bool]:
    if conn is not None:
        return conn, False
    from engine.db import connect

    return connect(), True


def _current(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT status, drop_reason, drop_stage FROM companies WHERE company_id = ?",
        (company_id,),
    ).fetchone()
    if row is None:
        raise UnknownCompany(company_id)
    return row


def advance(
    company_id: str, to_status: Status | str, conn: sqlite3.Connection | None = None
) -> bool:
    """Move a company forward to to_status.

    Returns True if the status changed, False if it was already there (a re-run
    changes nothing). Raises TransitionError on a backward move, on a move out
    of dropped, or if to_status is dropped (use drop()).
    """
    to = Status(to_status)
    if to is Status.DROPPED:
        raise TransitionError("use drop() to move a company to dropped")
    c, owned = _conn(conn)
    try:
        with c:
            cur = Status(_current(c, company_id)["status"])
            if cur is to:
                return False
            if not can_advance(cur, to):
                raise TransitionError(f"{company_id}: cannot move {cur} -> {to}")
            c.execute(
                "UPDATE companies SET status = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE company_id = ?",
                (to.value, company_id),
            )
        return True
    finally:
        if owned:
            c.close()


def drop(
    company_id: str, reason: str, stage: str, conn: sqlite3.Connection | None = None
) -> bool:
    """Move a company to dropped with a reason and the stage that dropped it.

    Returns True if it changed. Dropping again with the same reason and stage is
    a no-op (False); with a different reason or stage it raises TransitionError.
    """
    if not reason or not reason.strip():
        raise ValueError("drop() needs a non-empty reason")
    if not stage or not stage.strip():
        raise ValueError("drop() needs a non-empty stage")
    c, owned = _conn(conn)
    try:
        with c:
            row = _current(c, company_id)
            if row["status"] == Status.DROPPED:
                if (row["drop_reason"], row["drop_stage"]) == (reason, stage):
                    return False
                raise TransitionError(f"{company_id}: already dropped")
            c.execute(
                "UPDATE companies SET status = 'dropped', drop_reason = ?, drop_stage = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE company_id = ?",
                (reason, stage, company_id),
            )
        return True
    finally:
        if owned:
            c.close()


def status_counts(conn: sqlite3.Connection) -> dict[Status, int]:
    """Company count for every status, in pipeline order, dropped last."""
    counts = {s: 0 for s in Status}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM companies GROUP BY status"):
        counts[Status(row["status"])] = int(row["n"])
    return counts

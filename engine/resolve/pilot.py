"""Mark the pilot twelve (docs/PLAN.md Step 31, `te pilot pick|list|unpick`).

The person chooses; this only records pilot = 1 and checks the choice
against Step 31:

  refused  a hand-made Phase 0 company (hand = 1); a company that is dropped
           or not yet qualified; a segment other than in_saas or
           us_india_team; a pick that would make more than TOTAL in all or
           more than PER_SEGMENT in one segment.
  warned   fewer than TOTAL, fewer than PER_SEGMENT in a segment, fewer than
           MIN_CANDIDATE companies whose best signal is a candidate role, a
           company with no best signal.

A pick is all or nothing: one refusal and nothing is written. The split
within each six (two clear findings, two none, two unpredictable) is the
person's judgment and is not checked. No network, no model calls.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from engine.resolve.segment import IN_SAAS, US_INDIA_TEAM
from engine.states import PIPELINE, Status

TOTAL = 12
PER_SEGMENT = 6
MIN_CANDIDATE = 3
SEGMENTS = (IN_SAAS, US_INDIA_TEAM)
_PICKABLE = frozenset(s.value for s in PIPELINE[PIPELINE.index(Status.QUALIFIED):])


class PilotError(ValueError):
    pass


@dataclass
class PilotRow:
    company_id: str
    name: str
    segment: str | None
    status: str
    hand: int
    best_signal_id: str | None
    offer_type: str | None
    title: str | None


@dataclass
class Check:
    ok: bool
    problems: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


_SQL = ("SELECT c.company_id, c.name, c.segment, c.status, c.hand, c.best_signal_id, "
        "s.offer_type, s.title FROM companies c "
        "LEFT JOIN signals s ON s.signal_id = c.best_signal_id ")


def _row(r: sqlite3.Row) -> PilotRow:
    return PilotRow(r["company_id"], r["name"], r["segment"], r["status"], r["hand"],
                    r["best_signal_id"], r["offer_type"], r["title"])


def pilots(conn: sqlite3.Connection) -> list[PilotRow]:
    return [_row(r) for r in conn.execute(_SQL + "WHERE c.pilot = 1 ORDER BY c.segment, "
                                                 "c.company_id")]


def check(rows: list[PilotRow]) -> Check:
    """Step 31 constraints for a full pilot set."""
    probs: list[str] = []
    counts = {seg: sum(1 for r in rows if r.segment == seg) for seg in SEGMENTS}
    cand = sum(1 for r in rows if r.offer_type == "candidate")
    counts["total"], counts["candidate"] = len(rows), cand
    if len(rows) != TOTAL:
        probs.append(f"{len(rows)} picked; the pilot is {TOTAL}")
    for seg in SEGMENTS:
        if counts[seg] != PER_SEGMENT:
            probs.append(f"{counts[seg]} in {seg}; want {PER_SEGMENT}")
    if cand < MIN_CANDIDATE:
        probs.append(f"{cand} with a candidate best signal; want at least {MIN_CANDIDATE}")
    for r in rows:
        if r.hand:
            probs.append(f"{r.company_id} is a Phase 0 (hand) company")
        if r.segment not in SEGMENTS:
            probs.append(f"{r.company_id} has segment {r.segment!r}")
        if r.best_signal_id is None:
            probs.append(f"{r.company_id} has no best signal")
        if r.status == Status.DROPPED:
            probs.append(f"{r.company_id} is dropped")
    return Check(not probs, probs, counts)


def pick(conn: sqlite3.Connection, ids: list[str], *, dry_run: bool = False
         ) -> tuple[list[str], list[str]]:
    """Mark ids pilot = 1. Returns (newly picked, warnings). Raises PilotError
    listing every refusal; then nothing is written."""
    ids = list(dict.fromkeys(ids))
    refusals: list[str] = []
    new: list[PilotRow] = []
    current = pilots(conn)
    have = {r.company_id for r in current}
    for cid in ids:
        r = conn.execute(_SQL + "WHERE c.company_id = ?", (cid,)).fetchone()
        if r is None:
            refusals.append(f"{cid}: no such company")
            continue
        row = _row(r)
        if row.hand:
            refusals.append(f"{cid}: a hand-made Phase 0 company; none of the twelve may be one")
        elif row.status == Status.DROPPED:
            refusals.append(f"{cid}: dropped")
        elif row.status not in _PICKABLE:
            refusals.append(f"{cid}: status {row.status}; only qualified companies (after "
                            "`te resolve review`) may be picked")
        elif row.segment not in SEGMENTS:
            refusals.append(f"{cid}: segment {row.segment!r}; set it in `te resolve review`")
        elif cid not in have:
            new.append(row)
    after = current + new
    if len(after) > TOTAL:
        refusals.append(f"{len(after)} would be picked; the pilot is {TOTAL} (unpick first)")
    for seg in SEGMENTS:
        n = sum(1 for r in after if r.segment == seg)
        if n > PER_SEGMENT:
            refusals.append(f"{n} would be in {seg}; at most {PER_SEGMENT}")
    if refusals:
        raise PilotError("\n".join(refusals))
    if not dry_run and new:
        with conn:
            conn.executemany("UPDATE companies SET pilot = 1, updated_at = "
                             "strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE company_id = ?",
                             [(r.company_id,) for r in new])
    return [r.company_id for r in new], check(after).problems


def unpick(conn: sqlite3.Connection, company_id: str) -> bool:
    r = conn.execute("SELECT pilot FROM companies WHERE company_id = ?",
                     (company_id,)).fetchone()
    if r is None:
        raise PilotError(f"{company_id}: no such company")
    if not r["pilot"]:
        return False
    with conn:
        conn.execute("UPDATE companies SET pilot = 0, updated_at = "
                     "strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE company_id = ?",
                     (company_id,))
    return True

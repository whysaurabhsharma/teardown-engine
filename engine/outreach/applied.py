"""Apply before you write, for candidate roles (docs/PLAN.md Step 75; protocol/
CONDUCT.md Part 3, "A human gate at send").

    te queue applied <company_id> [--on YYYY-MM-DD] [--note TEXT] [--dry-run]

A person submits the application through the company's own form first; this
records that they did. It sets touches.applied = 1 and applied_on on touch 1,
which is the flag `te queue draft` reads before it will make the candidate
email variant (engine/outreach/draft.py). When there is no touch 1 yet, a row
is written holding only the flag: no draft_id, no drafted_at, status NULL;
`te queue draft` fills it later.

Refused: a dropped company; a company whose best signal is not a `candidate`
role (an adjacent signal needs no application: the warning says so); a date in
the future. The note is free text for the person's own record and must never
hold a person's name or contact; it is stored, never printed.

Idempotent: the same date again changes nothing; a different date replaces the
recorded one and says what it was.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from engine import db
from engine.models import Touch
from engine.outreach.draft import Result, company_row, touch_one
from engine.states import Status


def applied(conn: sqlite3.Connection, company_id: str, *, on: date | None = None,
            note: str | None = None, dry_run: bool = False,
            today: date | None = None) -> Result:
    today = today or datetime.now(UTC).date()
    c = company_row(conn, company_id)
    if Status(c["status"]) is Status.DROPPED:
        return Result(company_id, "refused", f"dropped ({c['drop_reason']})")
    sig = None
    if c["best_signal_id"]:
        sig = conn.execute("SELECT offer_type FROM signals WHERE signal_id = ?",
                           (c["best_signal_id"],)).fetchone()
    if sig is None:
        return Result(company_id, "refused", "the company has no best signal; nothing to apply "
                                             "for")
    if sig["offer_type"] != "candidate":
        return Result(company_id, "refused",
                      f"not a candidate role (the best signal's offer_type is "
                      f"{sig['offer_type']}); the adjacent variant needs no application, and "
                      "nothing was recorded",
                      warnings=["not a candidate role: te queue applied is for candidate "
                                "signals only"])
    when = on or today
    if when > today:
        return Result(company_id, "refused", f"{when} is in the future")
    note = (note or "").strip() or None
    t1 = touch_one(conn, company_id)
    if t1 is not None and t1["applied"] == 1 and t1["applied_on"] == when.isoformat():
        return Result(company_id, "unchanged", f"the application is already recorded on {when}")
    was = (f" (was {t1['applied_on'] or 'recorded with no date'})"
           if t1 is not None and t1["applied"] == 1 else "")
    if dry_run:
        return Result(company_id, "dry_run", f"would record the application on {when}{was}")
    with conn:
        if t1 is None:
            contact = conn.execute("SELECT channel FROM contacts WHERE company_id = ? "
                                   "ORDER BY contact_id LIMIT 1", (company_id,)).fetchone()
            channel = (contact["channel"] if contact is not None
                       and contact["channel"] in ("email", "linkedin") else "email")
            db.insert(conn, Touch(company_id=company_id, touch_no=1, channel=channel,
                                  applied=True, applied_on=when, applied_note=note))
        else:
            conn.execute("UPDATE touches SET applied = 1, applied_on = ?, applied_note = "
                         "coalesce(?, applied_note) WHERE touch_id = ?",
                         (when.isoformat(), note, t1["touch_id"]))
    return Result(company_id, "applied", f"application recorded on {when}{was}; the candidate "
                                         f"email may now be drafted (te queue draft --company "
                                         f"{company_id})")

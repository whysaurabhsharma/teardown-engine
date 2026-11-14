"""Bounces and the stop on sending (docs/PLAN.md Step 74; protocol/CONDUCT.md
Part 3, "Bounces": two bounces in one day stops sending).

    te queue bounce <company_id> [--on YYYY-MM-DD] [--dry-run]
    te queue resume --reason "addresses re-verified"

bounce. A person saw a bounce for the company's latest sent email touch (the
engine reads no mail). It marks that touch outcome 'bounced' with bounced_on,
and puts the recipient's address in suppressions (reason 'bounce'), so nothing
is drafted to it again. The address is never printed. When two touches have
bounced on the same UTC day, a send_stops row is written (one open stop at a
time): `te queue draft` refuses every company until `te queue resume`.

resume. After the remaining addresses are re-verified and the confidence rule
that let the bounces through is tightened, close the open stop with a reason.

A bounced company is skipped by `te queue followups`; it is not dropped (a
person may find another address and record it with te people).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from engine.models import SendStop
from engine.outreach.draft import QueueError, Result, company_row, now_utc, stamp

BOUNCES_PER_DAY = 2  # CONDUCT Part 3
EMAIL_CHANNELS = ("email", "email-reply")
BOUNCE_REASON = "bounce"


def active_stop(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM send_stops WHERE resumed_at IS NULL "
                        "ORDER BY stop_id LIMIT 1").fetchone()


def stop_problem(conn: sqlite3.Connection) -> str | None:
    """Why nothing may be drafted now, or None (read by `te queue draft`)."""
    s = active_stop(conn)
    if s is None:
        return None
    return (f"sending is stopped since {s['stopped_on']} ({s['reason']}); re-verify the "
            "remaining addresses, tighten the confidence rule, then te queue resume --reason "
            "\"addresses re-verified\"")


def bounces_on(conn: sqlite3.Connection, day: date) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM touches WHERE bounced_on = ?",
                            (day.isoformat(),)).fetchone()[0])


def _latest_sent(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM touches WHERE company_id = ? AND status = 'sent' "
                        "ORDER BY touch_no DESC LIMIT 1", (company_id,)).fetchone()


def bounce(conn: sqlite3.Connection, company_id: str, *, on: date | None = None,
           dry_run: bool = False, today: date | None = None) -> Result:
    today = today or datetime.now(UTC).date()
    company_row(conn, company_id)
    t = _latest_sent(conn, company_id)
    if t is None:
        return Result(company_id, "refused", "no sent touch to bounce (te queue sent records "
                                             "touch 1)")
    if t["channel"] not in EMAIL_CHANNELS:
        return Result(company_id, "refused", f"the latest sent touch ({t['touch_no']}) went by "
                                             f"{t['channel']}; only an email bounces")
    if t["bounced_on"] is not None:
        return Result(company_id, "unchanged", f"touch {t['touch_no']} already bounced on "
                                               f"{t['bounced_on']}")
    when = on or today
    if when > today:
        return Result(company_id, "refused", f"{when} is in the future")
    if t["sent_at"] and when < date.fromisoformat(t["sent_at"][:10]):
        return Result(company_id, "refused", f"{when} is before touch {t['touch_no']} was sent")
    contact = conn.execute("SELECT email FROM contacts WHERE company_id = ? ORDER BY contact_id "
                           "LIMIT 1", (company_id,)).fetchone()
    address = (contact["email"] or "").strip().lower() if contact is not None else ""
    n = bounces_on(conn, when) + 1
    stops = n >= BOUNCES_PER_DAY and active_stop(conn) is None
    if dry_run:
        return Result(company_id, "dry_run",
                      f"would mark touch {t['touch_no']} bounced on {when}, suppress the "
                      f"address{'; and stop sending' if stops else ''}")
    with conn:
        conn.execute("UPDATE touches SET outcome = 'bounced', bounced_on = ? WHERE touch_id = ?",
                     (when.isoformat(), t["touch_id"]))
        if address:
            conn.execute("INSERT OR IGNORE INTO suppressions (email_or_domain, reason) "
                         "VALUES (?, ?)", (address, BOUNCE_REASON))
        if stops:
            from engine import db

            db.insert(conn, SendStop(reason=f"{n} bounces on {when}", stopped_on=when))
    lines = []
    if not address:
        lines.append("the recipient has no address on file to suppress")
    else:
        lines.append("the recipient's address is suppressed (Step 72/74)")
    if n >= BOUNCES_PER_DAY:
        lines += [f"{n} bounces on {when}: sending is stopped (CONDUCT Part 3).",
                  "Re-verify the remaining addresses and tighten the confidence rule that let "
                  "these through, then: te queue resume --reason \"addresses re-verified\""]
    return Result(company_id, "bounced", f"touch {t['touch_no']} bounced on {when}", lines)


def resume(conn: sqlite3.Connection, reason: str, *, dry_run: bool = False) -> Result:
    if not reason or not reason.strip():
        raise QueueError("resume needs a --reason (what was re-verified)")
    s = active_stop(conn)
    if s is None:
        return Result("queue", "unchanged", "sending is not stopped")
    if dry_run:
        return Result("queue", "dry_run", f"would resume the stop of {s['stopped_on']}")
    with conn:
        conn.execute("UPDATE send_stops SET resumed_at = ?, resume_reason = ? "
                     "WHERE stop_id = ?", (stamp(now_utc()), reason.strip(), s["stop_id"]))
    return Result("queue", "resumed", f"the stop of {s['stopped_on']} is lifted: "
                                      f"{reason.strip()}")

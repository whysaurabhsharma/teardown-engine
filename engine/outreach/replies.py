"""Replies, meetings and conversation closes, logged by hand (docs/PLAN.md Steps
72, 77 and 82; protocol/CONDUCT.md Part 3, "Opt-outs").

    te reply <company_id> --type positive|question|referral|not_now|negative|optout
             --note "..." [--on YYYY-MM-DD] [--optout] [--dry-run]
    te meeting <company_id> --held [--on YYYY-MM-DD] [--minutes N] [--note "..."] [--dry-run]
    te close <company_id> --outcome hiring_process|paid_project|not_now|no_fit|other
             --next "..." [--on YYYY-MM-DD] [--dry-run]

The engine reads no mail: a person reads the inbox and logs what came back.

reply. Recorded on the company's latest sent touch: a replies row (the type,
the date, the note), touches.reply_at (the first reply on that touch) and
touches.outcome 'replied_<type>' (the latest). The company moves sent ->
replied; a company already further on (meeting, closed) stays where it is.
An `optout` reply, or a `negative` one logged with --optout (an annoyed "no"),
puts the recipient's address and the domain into suppressions the same day,
reason 'optout' (CONDUCT Part 3): the company's registrable domain, and the
address's own domain when it differs and is not a free-mail provider. The
queue (engine/outreach/draft.py) then refuses to draft anything for it, and
`te queue followups` skips it. The rendered page is not deleted here (site/ is
the person's to touch): the path to delete is printed.

meeting. A call actually held, with its date (default today) and, when typed,
its length. The company moves to meeting (from sent or replied). The same
date again changes nothing.

close. The outcome of the conversation and the next step (Step 82); the
company moves to closed. One row per company: logging it again with other
values replaces them and says what they were.

Refused: an unknown or dropped company; no sent touch; a date in the future
or before the touch was sent; a note or next step holding a value from the
contacts table (the message never names it). Nothing here prints an address
or a name. No network, no model call.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from engine import db, states
from engine.build import render as R
from engine.models import Close, Meeting, Reply, Suppression
from engine.outreach.draft import Result, company_row, teardown_row
from engine.resolve.domain import registrable_domain
from engine.states import Status

REPLY_TYPES = ("positive", "question", "referral", "not_now", "negative", "optout")
POSITIVE_TYPES = ("positive", "question", "referral")  # Step 78: positive reply rate
OPTOUT = "optout"
OPTOUT_REASON = "optout"
CLOSE_OUTCOMES = ("hiring_process", "paid_project", "not_now", "no_fit", "other")
# an address at one of these says nothing about the company; never suppress the domain
FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com",
    "live.com", "icloud.com", "me.com", "proton.me", "protonmail.com", "rediffmail.com",
    "aol.com", "zoho.com", "zohomail.in", "gmx.com",
})


def _today(today: date | None) -> date:
    return today or datetime.now(UTC).date()


def latest_sent(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row | None:
    """The company's highest-numbered touch a person sent (status sent, or a sent_at:
    the Phase 0 hand rows carry sent_at with no status)."""
    return conn.execute("SELECT * FROM touches WHERE company_id = ? AND (status = 'sent' OR "
                        "sent_at IS NOT NULL) ORDER BY touch_no DESC LIMIT 1",
                        (company_id,)).fetchone()


def _sent_day(t: sqlite3.Row) -> date | None:
    return date.fromisoformat(t["sent_at"][:10]) if t["sent_at"] else None


def contact_problem(conn: sqlite3.Connection, company_id: str, text: str | None,
                    what: str) -> str | None:
    """Why `text` may not be stored: it holds a contacts value (never named)."""
    if not text:
        return None
    from engine.llm.gateway import contact_pattern, contact_values

    for v in contact_values(conn, company_id):
        if contact_pattern(v).search(text):
            return (f"the {what} holds a value from the contacts table (a name, title, address "
                    "or URL); write it without the person's details")
    return None


def _check_day(when: date, today: date, t: sqlite3.Row | None) -> str | None:
    if when > today:
        return f"{when} is in the future"
    sd = _sent_day(t) if t is not None else None
    if sd is not None and when < sd:
        return f"{when} is before touch {t['touch_no']} was sent ({sd})"
    return None


def _live(c: sqlite3.Row) -> str | None:
    if Status(c["status"]) is Status.DROPPED:
        return f"dropped ({c['drop_reason']})"
    return None


def suppress_targets(conn: sqlite3.Connection, company: sqlite3.Row) -> list[tuple[str, str]]:
    """(what, value) to suppress for an opt-out: the address, the company's domain, the
    address's domain when it differs and is not free mail. Values are never printed."""
    from engine.people.contacts import recipient

    out: list[tuple[str, str]] = []
    contact = recipient(conn, company["company_id"])
    email = (contact["email"] or "").strip().lower() if contact is not None else ""
    if email:
        out.append(("address", email))
    dom = (company["domain"] or "").strip().lower()
    reg = registrable_domain(dom) if dom else None
    if reg or dom:
        out.append(("domain", (reg or dom).lower()))
    if email and "@" in email:
        ed = email.rsplit("@", 1)[1]
        ereg = (registrable_domain(ed) or ed).lower()
        if ereg not in FREE_MAIL and all(ereg != v for _, v in out):
            out.append(("address domain", ereg))
    return out


def page_to_delete(conn: sqlite3.Connection, company_id: str) -> str | None:
    td = teardown_row(conn, company_id)
    if td is None or not td["page_slug"]:
        return None
    return str(R.site_dir() / td["page_slug"]) + "/"


def reply(conn: sqlite3.Connection, company_id: str, reply_type: str, *, note: str | None = None,
          on: date | None = None, optout: bool = False, dry_run: bool = False,
          today: date | None = None) -> Result:
    if reply_type not in REPLY_TYPES:
        return Result(company_id, "refused", f"--type must be one of {', '.join(REPLY_TYPES)}")
    is_optout = reply_type == OPTOUT or optout
    if optout and reply_type not in (OPTOUT, "negative"):
        return Result(company_id, "refused", "--optout goes with --type negative (an annoyed "
                                             "no); use --type optout for a plain stop")
    c = company_row(conn, company_id)
    if (why := _live(c)) is not None:
        return Result(company_id, "refused", why)
    t = latest_sent(conn, company_id)
    if t is None:
        return Result(company_id, "refused", "no sent touch to reply to (te queue sent records "
                                             "touch 1)")
    today = _today(today)
    when = on or today
    if (why := _check_day(when, today, t)) is not None:
        return Result(company_id, "refused", why)
    note = (note or "").strip() or None
    if (why := contact_problem(conn, company_id, note, "note")) is not None:
        return Result(company_id, "refused", why)
    outcome = f"replied_{reply_type}"
    exists = conn.execute("SELECT 1 FROM replies WHERE company_id = ? AND touch_no = ? AND "
                          "type = ? AND replied_on = ?",
                          (company_id, t["touch_no"], reply_type, when.isoformat())).fetchone()
    targets = suppress_targets(conn, c) if is_optout else []
    missing = [(w, v) for w, v in targets if conn.execute(
        "SELECT 1 FROM suppressions WHERE email_or_domain = ?", (v,)).fetchone() is None]
    status = Status(c["status"])
    moves = states.can_advance(status, Status.REPLIED)
    if exists and not missing and not moves:
        return Result(company_id, "unchanged", f"a {reply_type} reply on touch {t['touch_no']} "
                                               f"on {when} is already logged")
    lines: list[str] = []
    if is_optout:
        lines += [f"suppressed ({OPTOUT_REASON}): " + (", ".join(w for w, _ in targets) or
                                                     "nothing on file to suppress"),
                  "nothing more is drafted or followed up for this company (CONDUCT Part 3)"]
        page = page_to_delete(conn, company_id)
        lines.append(f"delete the teardown page today: {page}" if page else
                     "no rendered page on record; delete any copy of the page today")
    if dry_run:
        return Result(company_id, "dry_run",
                      f"would log a {reply_type} reply on touch {t['touch_no']} on {when}"
                      f"{'; and move sent -> replied' if moves else ''}", lines)
    reply_at = f"{when.isoformat()}T00:00:00Z" if on is not None else \
        datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        if not exists:
            db.insert(conn, Reply(company_id=company_id, touch_no=t["touch_no"],
                                  type=reply_type, replied_on=when, optout=is_optout,
                                  note=note))
        latest = conn.execute("SELECT type FROM replies WHERE company_id = ? AND touch_no = ? "
                              "ORDER BY replied_on DESC, reply_id DESC LIMIT 1",
                              (company_id, t["touch_no"])).fetchone()
        outcome = f"replied_{latest['type']}" if latest is not None else outcome
        conn.execute("UPDATE touches SET outcome = ?, reply_at = coalesce(reply_at, ?) "
                     "WHERE touch_id = ?", (outcome, reply_at, t["touch_id"]))
        for _, v in missing:
            db.insert(conn, Suppression(email_or_domain=v, reason=OPTOUT_REASON))
    if moves:
        states.advance(company_id, Status.REPLIED, conn)
    return Result(company_id, "replied",
                  f"{reply_type} reply logged on touch {t['touch_no']} on {when}"
                  f"{'; status sent -> replied' if moves else f'; status stays {status}'}",
                  lines)


def meeting(conn: sqlite3.Connection, company_id: str, *, held: bool, on: date | None = None,
            minutes: float | None = None, note: str | None = None, dry_run: bool = False,
            today: date | None = None) -> Result:
    if not held:
        return Result(company_id, "refused", "only a call actually held is logged: pass --held")
    if minutes is not None and not minutes > 0:
        return Result(company_id, "refused", "--minutes must be more than zero")
    c = company_row(conn, company_id)
    if (why := _live(c)) is not None:
        return Result(company_id, "refused", why)
    t = latest_sent(conn, company_id)
    if t is None:
        return Result(company_id, "refused", "no sent touch; a meeting follows a send")
    today = _today(today)
    when = on or today
    if (why := _check_day(when, today, t)) is not None:
        return Result(company_id, "refused", why)
    note = (note or "").strip() or None
    if (why := contact_problem(conn, company_id, note, "note")) is not None:
        return Result(company_id, "refused", why)
    status = Status(c["status"])
    moves = states.can_advance(status, Status.MEETING)
    if status is Status.CLOSED:
        return Result(company_id, "refused", "the conversation is closed; a new call after a "
                                             "close is not logged here")
    exists = conn.execute("SELECT minutes FROM meetings WHERE company_id = ? AND held_on = ?",
                          (company_id, when.isoformat())).fetchone()
    if exists is not None and not moves:
        return Result(company_id, "unchanged", f"a meeting on {when} is already logged")
    if dry_run:
        return Result(company_id, "dry_run", f"would log a meeting held on {when}"
                                             f"{f'; and move {status} -> meeting' if moves else ''}")
    with conn:
        if exists is None:
            db.insert(conn, Meeting(company_id=company_id, held_on=when, minutes=minutes,
                                    note=note))
    if moves:
        states.advance(company_id, Status.MEETING, conn)
    return Result(company_id, "meeting", f"meeting held on {when} logged"
                                         f"{f'; status {status} -> meeting' if moves else ''}",
                  [f"prep sheet: te prep {company_id}"])


def close(conn: sqlite3.Connection, company_id: str, outcome: str, next_step: str, *,
          on: date | None = None, dry_run: bool = False, today: date | None = None) -> Result:
    if outcome not in CLOSE_OUTCOMES:
        return Result(company_id, "refused",
                      f"--outcome must be one of {', '.join(CLOSE_OUTCOMES)}")
    next_step = (next_step or "").strip()
    if not next_step:
        return Result(company_id, "refused", "--next needs the next step (or 'none')")
    c = company_row(conn, company_id)
    if (why := _live(c)) is not None:
        return Result(company_id, "refused", why)
    t = latest_sent(conn, company_id)
    if t is None:
        return Result(company_id, "refused", "no sent touch; there is no conversation to close")
    today = _today(today)
    when = on or today
    if (why := _check_day(when, today, t)) is not None:
        return Result(company_id, "refused", why)
    if (why := contact_problem(conn, company_id, next_step, "next step")) is not None:
        return Result(company_id, "refused", why)
    status = Status(c["status"])
    moves = states.can_advance(status, Status.CLOSED)
    old = conn.execute("SELECT * FROM closes WHERE company_id = ?", (company_id,)).fetchone()
    same = old is not None and (old["outcome"], old["next_step"], old["closed_on"]) == (
        outcome, next_step, when.isoformat())
    if same and not moves:
        return Result(company_id, "unchanged", f"closed as {outcome} on {when} already")
    was = f" (was {old['outcome']} on {old['closed_on']})" if old is not None and not same else ""
    if dry_run:
        return Result(company_id, "dry_run", f"would close as {outcome} on {when}{was}"
                                             f"{f'; and move {status} -> closed' if moves else ''}")
    with conn:
        if old is None:
            db.insert(conn, Close(company_id=company_id, outcome=outcome, next_step=next_step,
                                  closed_on=when))
        elif not same:
            conn.execute("UPDATE closes SET outcome = ?, next_step = ?, closed_on = ?, "
                         "logged_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE company_id = ?",
                         (outcome, next_step, when.isoformat(), company_id))
    if moves:
        states.advance(company_id, Status.CLOSED, conn)
    return Result(company_id, "closed", f"closed as {outcome} on {when}{was}"
                                        f"{f'; status {status} -> closed' if moves else ''}")

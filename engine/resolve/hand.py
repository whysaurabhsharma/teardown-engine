"""Bring in the Phase 0 companies (docs/PLAN.md Step 30, `te company add --hand`).

Each of the six teardowns sent by hand in Phase 0 becomes a companies row with
hand = 1 at status `sent`, one manual signal for the role it was written
against (best_signal_id), and a touches row for touch 1 carrying any reply
(outcome "replied_<type>", reply_at; the types of Step 77). The status stays
`sent` even with a reply: the reply log (E7) owns the move to `replied`. An
optout reply also suppresses the company's domain (suppressions row, reason
"optout"); domain level only, no person data.

No recipient details are taken or stored: the contacts table is filled by the
People step, never here. --note goes to companies.review_note and must not
name a person.

Role family comes from the title rules in engine/signal/classify.py (no model
call); when they cannot place the title, --family is required. The typed
offer type is kept even when it differs from what the rules would give (a
warning says so).

Idempotent by domain: a second run with the same inputs writes nothing. A
hand row that exists gets only what is missing (the signal, touch 1, a reply
not yet logged); differing values are reported, never overwritten. A domain
already in the pipeline (hand = 0) is refused unless adopt is set, which
marks it hand = 1 and moves it forward to `sent` (never backward, never a
dropped or pilot row).

Minutes by hand (--minutes-research, --minutes-writing, --minutes-slice) go to
hand_minutes (migration 0020), one row per company: what the teardown cost
before the engine existed, the baseline for minutes per teardown (Step 78).
A part already stored with a different value is reported, never overwritten.

No network, no model calls.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from engine import db, states
from engine.models import Company, Signal, Suppression, Touch
from engine.resolve.domain import company_id_for, registrable_domain
from engine.resolve.segment import IN_SAAS, US_INDIA_TEAM
from engine.signal import classify
from engine.signal.manual import make_signal
from engine.states import Status

SEGMENTS = (IN_SAAS, US_INDIA_TEAM)
OFFERS = ("candidate", "adjacent")
FAMILIES = tuple(sorted(classify.CANDIDATE_FAMILIES | classify.ADJACENT_FAMILIES))
# docs/PLAN.md Step 77, the reply types `te reply` logs
REPLIES = ("positive", "question", "referral", "not_now", "negative", "optout")
OPTOUT = "optout"
CHANNELS = ("email", "linkedin")


class HandError(ValueError):
    pass


@dataclass(frozen=True)
class HandEntry:
    company: str
    domain: str
    segment: str
    role: str
    role_url: str
    offer: str
    family: str | None = None
    sent_on: date | None = None
    reply: str | None = None
    reply_on: date | None = None
    channel: str = "email"
    note: str | None = None
    adopt: bool = False
    minutes_research: float | None = None
    minutes_writing: float | None = None
    minutes_slice: float | None = None

    def minutes(self) -> dict[str, float]:
        """The minutes typed, by hand_minutes column."""
        parts = {"research": self.minutes_research, "writing": self.minutes_writing,
                 "slice": self.minutes_slice}
        return {k: float(v) for k, v in parts.items() if v is not None}


@dataclass
class HandResult:
    company_id: str
    domain: str
    role_family: str
    signal_id: str
    created: bool = False
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _dt(d: date | None) -> datetime | None:
    return datetime(d.year, d.month, d.day, tzinfo=UTC) if d else None


def prepare(e: HandEntry, today: date) -> tuple[str, str, str, Signal]:
    """Validate; returns (domain, company_id, role_family, signal). Raises HandError."""
    if e.segment not in SEGMENTS:
        raise HandError(f"segment must be one of {', '.join(SEGMENTS)}")
    if e.offer not in OFFERS:
        raise HandError(f"offer must be one of {', '.join(OFFERS)}")
    if e.reply is not None and e.reply not in REPLIES:
        raise HandError(f"reply must be one of {', '.join(REPLIES)}")
    if e.reply_on is not None and e.reply is None:
        raise HandError("--reply-on needs --reply")
    if e.channel not in CHANNELS:
        raise HandError(f"channel must be one of {', '.join(CHANNELS)}")
    for part, v in e.minutes().items():
        if not 0 <= v <= 6000:
            raise HandError(f"--minutes-{part} must be between 0 and 6000")
    dom = registrable_domain(e.domain)
    if dom is None:
        raise HandError(f"not a registrable domain: {e.domain!r}")
    family = e.family or classify.rule_family(e.role)
    if family is None:
        raise HandError(f"the title rules cannot place {e.role!r}; pass --family")
    if family not in FAMILIES:
        raise HandError(f"role family {family!r} is not a GTM family ({', '.join(FAMILIES)})")
    try:
        sig = make_signal(company=e.company, title=e.role, url=e.role_url, today=today)
    except ValueError as err:
        raise HandError(str(err)) from err
    sig = sig.model_copy(update={"company_id": company_id_for(dom), "role_family": family,
                                 "offer_type": e.offer})
    return dom, company_id_for(dom), family, sig


def add(conn: sqlite3.Connection, e: HandEntry, *, today: date) -> HandResult:
    dom, cid, family, sig = prepare(e, today)
    res = HandResult(cid, dom, family, sig.signal_id)
    rule_offer = classify.offer_type(family, e.role)
    if rule_offer != e.offer:
        res.warnings.append(f"the title rules give offer {rule_offer} for {e.role!r}; "
                            f"keeping {e.offer}")

    row = conn.execute("SELECT * FROM companies WHERE domain = ?", (dom,)).fetchone()
    if row is not None and row["company_id"] != cid:
        cid = res.company_id = row["company_id"]
        sig = sig.model_copy(update={"company_id": cid})
    if row is None and conn.execute("SELECT 1 FROM companies WHERE company_id = ?",
                                     (cid,)).fetchone():
        raise HandError(f"company_id {cid} exists with another domain; fix by hand")
    if row is not None and not row["hand"]:
        if not e.adopt:
            raise HandError(f"{dom} is already in the pipeline as {row['company_id']} "
                            f"(status {row['status']}); pass --adopt to mark it hand-made")
        if row["status"] == Status.DROPPED:
            raise HandError(f"{row['company_id']} is dropped ({row['drop_reason']}); "
                            "a dropped company cannot be adopted")
        if row["pilot"]:
            raise HandError(f"{row['company_id']} is in the pilot; unpick it first "
                            "(no pilot company may be a Phase 0 company)")

    with conn:
        if row is None:
            db.insert(conn, Company(company_id=cid, domain=dom, name=" ".join(e.company.split()),
                                    segment=e.segment, status=Status.SENT, hand=True,
                                    review_note=(e.note or "").strip() or None))
            res.created = True
            res.changes.append(f"company {cid} created at sent, hand = 1")
        else:
            if not row["hand"]:
                conn.execute("UPDATE companies SET hand = 1, updated_at = "
                             "strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE company_id = ?",
                             (row["company_id"],))
                res.changes.append(f"company {cid} adopted: hand = 1")
            if row["segment"] is None:
                conn.execute("UPDATE companies SET segment = ? WHERE company_id = ?",
                             (e.segment, cid))
                res.changes.append(f"segment {e.segment}")
            elif row["segment"] != e.segment:
                res.warnings.append(f"segment is {row['segment']}, not {e.segment}; unchanged")
            if row["name"] != " ".join(e.company.split()):
                res.warnings.append(f"name is {row['name']!r}; unchanged")
            if e.note and e.note.strip() and row["review_note"] != e.note.strip():
                if row["review_note"] is None:
                    conn.execute("UPDATE companies SET review_note = ? WHERE company_id = ?",
                                 (e.note.strip(), cid))
                    res.changes.append("note")
                else:
                    res.warnings.append("a different note is stored; unchanged")

        s = conn.execute("SELECT company_id, role_family, offer_type FROM signals "
                         "WHERE signal_id = ?", (res.signal_id,)).fetchone()
        if s is None:
            db.insert(conn, sig)
            res.changes.append(f"signal {res.signal_id}")
        elif s["company_id"] is None:
            conn.execute("UPDATE signals SET company_id = ?, role_family = coalesce("
                         "role_family, ?), offer_type = coalesce(offer_type, ?) "
                         "WHERE signal_id = ?", (cid, family, e.offer, res.signal_id))
            res.changes.append(f"signal {res.signal_id} attached")
        elif s["company_id"] != cid:
            raise HandError(f"signal {res.signal_id} belongs to {s['company_id']}")
        best = conn.execute("SELECT best_signal_id FROM companies WHERE company_id = ?",
                            (cid,)).fetchone()["best_signal_id"]
        if best is None:
            conn.execute("UPDATE companies SET best_signal_id = ? WHERE company_id = ?",
                         (res.signal_id, cid))
            res.changes.append("best_signal_id")
        elif best != res.signal_id:
            res.warnings.append(f"best_signal_id is {best}; unchanged")

        outcome = f"replied_{e.reply}" if e.reply else None
        t = conn.execute("SELECT * FROM touches WHERE company_id = ? AND touch_no = 1",
                         (cid,)).fetchone()
        if t is None:
            db.insert(conn, Touch(company_id=cid, touch_no=1, channel=e.channel,
                                  sent_at=_dt(e.sent_on), outcome=outcome,
                                  reply_at=_dt(e.reply_on)))
            res.changes.append("touch 1" + (f" ({outcome})" if outcome else ""))
        else:
            fill: dict[str, object] = {}
            for col, val in (("sent_at", _dt(e.sent_on)), ("outcome", outcome),
                             ("reply_at", _dt(e.reply_on))):
                if val is None:
                    continue
                text = val.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(val, datetime) else val
                if t[col] is None:
                    fill[col] = text
                elif t[col] != text:
                    res.warnings.append(f"touch 1 {col} is {t[col]}; unchanged")
            if t["channel"] != e.channel:
                res.warnings.append(f"touch 1 channel is {t['channel']}; unchanged")
            if fill:
                sets = ", ".join(f"{k} = ?" for k in fill)
                conn.execute(f"UPDATE touches SET {sets} WHERE touch_id = ?",
                             (*fill.values(), t["touch_id"]))
                res.changes.append("touch 1 " + ", ".join(fill))

        typed = e.minutes()
        if typed:
            hm = conn.execute("SELECT research, writing, slice FROM hand_minutes "
                              "WHERE company_id = ?", (cid,)).fetchone()
            if hm is None:
                cols = ", ".join(typed)
                marks = ", ".join("?" for _ in typed)
                conn.execute(f"INSERT INTO hand_minutes (company_id, {cols}) "
                             f"VALUES (?, {marks})", (cid, *typed.values()))
                res.changes.append("minutes " + ", ".join(f"{k} {v:g}"
                                                          for k, v in typed.items()))
            else:
                fill_m = {k: v for k, v in typed.items() if hm[k] is None}
                for k, v in typed.items():
                    if hm[k] is not None and float(hm[k]) != v:
                        res.warnings.append(f"minutes {k} is {hm[k]:g}; unchanged")
                if fill_m:
                    sets = ", ".join(f"{k} = ?" for k in fill_m)
                    conn.execute(f"UPDATE hand_minutes SET {sets}, updated_at = "
                                 "strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE company_id = ?",
                                 (*fill_m.values(), cid))
                    res.changes.append("minutes " + ", ".join(f"{k} {v:g}"
                                                              for k, v in fill_m.items()))

        if e.reply == OPTOUT and conn.execute(
                "SELECT 1 FROM suppressions WHERE email_or_domain = ?", (dom,)).fetchone() is None:
            db.insert(conn, Suppression(email_or_domain=dom, reason=OPTOUT))
            res.changes.append(f"suppression {dom} (optout)")

    if row is not None and not row["hand"]:
        if states.can_advance(row["status"], Status.SENT):
            states.advance(cid, Status.SENT, conn)
            res.changes.append(f"status {row['status']} -> sent")
        elif row["status"] != Status.SENT:
            res.warnings.append(f"status {row['status']} is past sent; unchanged")
    return res
